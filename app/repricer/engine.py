import asyncio
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from time import perf_counter
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Position, UpdateStatus
from app.db.repositories import PositionRepository
from app.market.client import StarvellClient, safe_starvell_error_reason
from app.market.exceptions import (
    StarvellEndpointNotConfiguredError,
    StarvellPayloadStyleError,
    StarvellWriteDisabledError,
)
from app.market.schemas import MarketOffer, OwnLot
from app.repricer.competitor_filter import CompetitorFilter, CompetitorFilterSettings
from app.repricer.price_strategy import PriceCalculationSettings, PriceDecision, UndercutByStepStrategy


@dataclass(frozen=True)
class ProcessResult:
    position_amount: int
    status: str
    reason: str
    old_price: Decimal | None
    new_price: Decimal | None
    competitor_price: Decimal | None
    phase_metrics: dict[str, float | bool | str | None] = field(default_factory=dict, compare=False)


class RepricerEngine:
    def __init__(
        self,
        *,
        session: AsyncSession,
        settings: Settings,
        starvell_client: StarvellClient,
        dry_run: bool | None = None,
    ):
        self.session = session
        self.settings = settings
        self.starvell_client = starvell_client
        self.dry_run = settings.dry_run if dry_run is None else dry_run
        self.positions = PositionRepository(session)
        self.competitor_filter = CompetitorFilter()
        self.strategy = UndercutByStepStrategy()
        self.logger = get_logger(__name__)

    async def process_position(self, position_amount: int) -> ProcessResult:
        position = await self.positions.get_by_amount(position_amount)
        if position is None:
            return ProcessResult(position_amount, "skipped", "position_not_found", None, None, None)
        if not position.enabled:
            return ProcessResult(position_amount, "skipped", "position_disabled", None, None, None)

        try:
            result = await self._process_loaded_position(position)
            db_commit_started_at = perf_counter()
            await self.session.commit()
            db_commit_ms = _elapsed_ms(db_commit_started_at)
            if result.phase_metrics:
                self._log_cycle_phase_profile(
                    position=position,
                    result=result,
                    db_commit_ms=db_commit_ms,
                )
            return result
        except Exception as exc:
            await self.session.rollback()
            reason = safe_starvell_error_reason(exc)
            await self._persist_failure(position_amount, exc, reason)
            log_context = {
                "proxy_profile": self.settings.worker_group,
                "position_amount": position_amount,
                "error": reason,
            }
            if isinstance(
                exc,
                (
                    StarvellEndpointNotConfiguredError,
                    StarvellPayloadStyleError,
                    StarvellWriteDisabledError,
                ),
            ):
                self.logger.warning("repricer_position_failed", **log_context)
            else:
                self.logger.exception("repricer_position_failed", **log_context)
            return ProcessResult(position_amount, "failed", reason, None, None, None)

    async def process_watcher_price_event(
        self,
        position_amount: int,
        event: dict[str, Any],
    ) -> ProcessResult | None:
        if not self.settings.watcher_event_fast_path_enabled:
            return None
        position = await self.positions.get_by_amount(position_amount)
        if position is None or not position.enabled:
            return None
        try:
            result = await self._process_watcher_price_event_loaded_position(position, event)
            if result is None:
                return None
            db_commit_started_at = perf_counter()
            await self.session.commit()
            db_commit_ms = _elapsed_ms(db_commit_started_at)
            if result.phase_metrics:
                self._log_cycle_phase_profile(
                    position=position,
                    result=result,
                    db_commit_ms=db_commit_ms,
                )
            return result
        except Exception as exc:
            await self.session.rollback()
            reason = safe_starvell_error_reason(exc)
            await self._persist_failure(position_amount, exc, reason)
            self.logger.exception(
                "event_fast_path_failed",
                proxy_profile=self.settings.worker_group,
                position_amount=position_amount,
                error=reason,
                error_type=type(exc).__name__,
            )
            return ProcessResult(position_amount, "failed", reason, None, None, None)

    async def _process_loaded_position(self, position: Position) -> ProcessResult:
        cycle_started_at = perf_counter()
        price_write_ms = 0.0
        if not position.lot_id:
            await self._record_missing_lot(position)
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.SKIPPED.value,
                "missing_lot_id",
                position.state.current_own_price if position.state else None,
                None,
                position.state.last_seen_competitor_price if position.state else None,
            )

        own_lot_cache_status = self.starvell_client.own_lot_cache_status(position.lot_id)
        parallel_fetch_enabled = bool(own_lot_cache_status["fresh"])
        parallel_fetch_disabled_reason = (
            None if parallel_fetch_enabled else str(own_lot_cache_status["reason"])
        )
        if position.robux_amount == 500:
            self.logger.info(
                "repricer_500_parallel_fetch_diagnostics",
                lot_id=position.lot_id,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
                own_lot_cache_age_seconds=own_lot_cache_status["cache_age_seconds"],
                own_lot_cache_ttl_seconds=own_lot_cache_status["cache_ttl_seconds"],
            )
        if parallel_fetch_enabled:
            market_task = asyncio.create_task(
                self._timed_market_fetch(position.robux_amount, position.lot_id)
            )
            own_lot_task = asyncio.create_task(
                self._timed_my_lot_fetch(position.robux_amount, position.lot_id)
            )
            try:
                (market_result, market_request_ms), (
                    own_lot,
                    my_lot_request_ms,
                ) = await asyncio.gather(
                    market_task,
                    own_lot_task,
                )
            except Exception:
                for task in (market_task, own_lot_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(market_task, own_lot_task, return_exceptions=True)
                raise
        else:
            market_result, market_request_ms = await self._timed_market_fetch(
                position.robux_amount,
                position.lot_id,
            )
            own_lot, my_lot_request_ms = await self._timed_my_lot_fetch(
                position.robux_amount,
                position.lot_id,
            )
        offers = market_result.offers
        if market_own_lot := self.starvell_client.refresh_own_lot_cache_from_market(
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            offers=offers,
        ):
            own_lot = market_own_lot
        current_price = self._current_price(position, own_lot)

        filter_settings = CompetitorFilterSettings(
            min_rating=position.settings.min_rating,
            ignore_no_rating=position.settings.ignore_no_rating,
            own_seller_id=self.settings.own_seller_id,
            own_seller_username=self.settings.own_seller_username,
        )
        filter_result = self.competitor_filter.filter(
            offers,
            filter_settings,
        )
        ignored_counts = Counter(
            reason
            for offer in offers
            if (reason := self.competitor_filter.ignore_reason(offer, filter_settings))
        )
        self.logger.info(
            "repricer_competitor_diagnostics",
            proxy_profile=self.settings.worker_group,
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            method=market_result.method,
            url=market_result.url,
            subcategory_id=market_result.subcategory_id,
            raw_offer_count=market_result.raw_offer_count,
            offers_before_filter=len(offers),
            offers_after_filter=len(filter_result.accepted),
            parser_rejected_count=market_result.parser_rejected_count,
            ignored_reasons=dict(ignored_counts),
            market_cache_hit=market_result.cache_hit,
            market_inflight_dedupe_hit=market_result.inflight_dedupe_hit,
            market_cache_key=market_result.market_cache_key,
            category_payload_size=market_result.response_size_bytes,
        )
        await self.positions.add_competitor_snapshots(
            position,
            offers,
            filter_result.ignored_reasons,
        )

        decision = self.strategy.calculate(
            competitors=filter_result.accepted,
            current_own_price=current_price,
            settings=PriceCalculationSettings(
                min_price=position.settings.min_price,
                max_price=position.settings.max_price,
                step=position.settings.step,
                fallback_behavior=position.settings.fallback_behavior,
            ),
        )

        if decision.target_price is None:
            await self._record_decision(
                position=position,
                decision=decision,
                old_price=current_price,
                new_price=None,
                status=UpdateStatus.SKIPPED.value,
                reason=decision.reason,
            )
            self._log_cycle_profile(
                position=position,
                status=UpdateStatus.SKIPPED.value,
                reason=decision.reason,
                decision=decision,
                current_price=current_price,
                target_price=None,
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            phase_metrics = self._phase_metrics(
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.SKIPPED.value,
                decision.reason,
                current_price,
                None,
                decision.competitor_price,
                phase_metrics,
            )

        if not decision.should_update:
            await self._record_decision(
                position=position,
                decision=decision,
                old_price=current_price,
                new_price=decision.target_price,
                status=UpdateStatus.SKIPPED.value,
                reason=decision.reason,
            )
            self._log_cycle_profile(
                position=position,
                status=UpdateStatus.SKIPPED.value,
                reason=decision.reason,
                decision=decision,
                current_price=current_price,
                target_price=decision.target_price,
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            phase_metrics = self._phase_metrics(
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.SKIPPED.value,
                decision.reason,
                current_price,
                decision.target_price,
                decision.competitor_price,
                phase_metrics,
            )

        if self.dry_run:
            await self._record_decision(
                position=position,
                decision=decision,
                old_price=current_price,
                new_price=decision.target_price,
                status=UpdateStatus.DRY_RUN.value,
                reason=f"dry_run_would_update:{decision.reason}",
            )
            self.logger.info(
                "repricer_dry_run_price_update",
                proxy_profile=self.settings.worker_group,
                position_amount=position.robux_amount,
                old_price=str(current_price),
                new_price=str(decision.target_price),
                competitor_price=str(decision.competitor_price),
            )
            self._log_cycle_profile(
                position=position,
                status=UpdateStatus.DRY_RUN.value,
                reason=decision.reason,
                decision=decision,
                current_price=current_price,
                target_price=decision.target_price,
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            phase_metrics = self._phase_metrics(
                market_request_ms=market_request_ms,
                my_lot_request_ms=my_lot_request_ms,
                price_write_ms=price_write_ms,
                cycle_started_at=cycle_started_at,
                parallel_fetch_enabled=parallel_fetch_enabled,
                parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            )
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.DRY_RUN.value,
                decision.reason,
                current_price,
                decision.target_price,
                decision.competitor_price,
                phase_metrics,
            )

        price_write_started_at = perf_counter()
        await self.starvell_client.update_my_lot_price(
            position.robux_amount,
            position.lot_id,
            decision.target_price,
            allow_real_write=not self.dry_run,
        )
        price_write_ms = _elapsed_ms(price_write_started_at)
        await self._record_decision(
            position=position,
            decision=decision,
            old_price=current_price,
            new_price=decision.target_price,
            status=UpdateStatus.SUCCESS.value,
            reason=decision.reason,
        )
        self.logger.info(
            "repricer_price_updated",
            proxy_profile=self.settings.worker_group,
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            old_price=str(current_price),
            new_price=str(decision.target_price),
            competitor_price=str(decision.competitor_price),
        )
        self._log_cycle_profile(
            position=position,
            status=UpdateStatus.SUCCESS.value,
            reason=decision.reason,
            decision=decision,
            current_price=current_price,
            target_price=decision.target_price,
            market_request_ms=market_request_ms,
            my_lot_request_ms=my_lot_request_ms,
            price_write_ms=price_write_ms,
            cycle_started_at=cycle_started_at,
            parallel_fetch_enabled=parallel_fetch_enabled,
            parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
        )
        phase_metrics = self._phase_metrics(
            market_request_ms=market_request_ms,
            my_lot_request_ms=my_lot_request_ms,
            price_write_ms=price_write_ms,
            cycle_started_at=cycle_started_at,
            parallel_fetch_enabled=parallel_fetch_enabled,
            parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
        )
        return ProcessResult(
            position.robux_amount,
            UpdateStatus.SUCCESS.value,
            decision.reason,
            current_price,
            decision.target_price,
            decision.competitor_price,
            phase_metrics,
        )

    async def _process_watcher_price_event_loaded_position(
        self,
        position: Position,
        event: dict[str, Any],
    ) -> ProcessResult | None:
        event_started_at = perf_counter()
        event_age_ms = _event_age_ms(event)
        self.logger.info(
            "event_fast_path_attempt",
            proxy_profile=self.settings.worker_group,
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            offer_id=event.get("offer_id"),
            event_age_ms=event_age_ms,
        )
        fallback = self._event_fast_path_fallback
        if not position.lot_id:
            fallback(position=position, event=event, reason="missing_lot_id", event_age_ms=event_age_ms)
            return None

        event_price = _decimal_or_none(event.get("new_price"))
        if event_price is None:
            fallback(position=position, event=event, reason="missing_event_price", event_age_ms=event_age_ms)
            return None

        own_lot_cache_status = self.starvell_client.own_lot_cache_status(position.lot_id)
        cache_age = own_lot_cache_status["cache_age_seconds"]
        if not own_lot_cache_status["fresh"]:
            fallback(
                position=position,
                event=event,
                reason=str(own_lot_cache_status["reason"]),
                event_age_ms=event_age_ms,
                cache_age_seconds=cache_age,
            )
            return None
        if (
            cache_age is not None
            and float(cache_age) > self.settings.watcher_event_own_lot_cache_max_age_seconds
        ):
            fallback(
                position=position,
                event=event,
                reason="own_lot_cache_too_old",
                event_age_ms=event_age_ms,
                cache_age_seconds=cache_age,
            )
            return None

        own_lot = self.starvell_client.cached_own_lot(position.lot_id)
        current_price = self._current_price(position, own_lot)
        if current_price is None:
            fallback(position=position, event=event, reason="missing_current_price", event_age_ms=event_age_ms)
            return None

        event_offer = _market_offer_from_event(position.robux_amount, event, event_price)
        filter_settings = CompetitorFilterSettings(
            min_rating=position.settings.min_rating,
            ignore_no_rating=position.settings.ignore_no_rating,
            own_seller_id=self.settings.own_seller_id,
            own_seller_username=self.settings.own_seller_username,
        )
        filter_result = self.competitor_filter.filter([event_offer], filter_settings)
        if not filter_result.accepted:
            fallback(
                position=position,
                event=event,
                reason="event_competitor_filtered",
                event_age_ms=event_age_ms,
                ignored_reasons=filter_result.ignored_reasons,
            )
            return None

        decision = self.strategy.calculate(
            competitors=filter_result.accepted,
            current_own_price=current_price,
            settings=PriceCalculationSettings(
                min_price=position.settings.min_price,
                max_price=position.settings.max_price,
                step=position.settings.step,
                fallback_behavior=position.settings.fallback_behavior,
            ),
        )
        if decision.reason in {
            "all_competitors_below_min_price",
            "min_price_bounce_to_upper_competitor",
        }:
            fallback(
                position=position,
                event=event,
                reason=f"strategy_requires_full_market:{decision.reason}",
                event_age_ms=event_age_ms,
            )
            return None
        if decision.target_price is None:
            fallback(
                position=position,
                event=event,
                reason=f"strategy_no_target:{decision.reason}",
                event_age_ms=event_age_ms,
            )
            return None

        price_write_ms = 0.0
        status = UpdateStatus.SKIPPED.value
        reason = decision.reason
        if decision.should_update:
            if self.dry_run:
                status = UpdateStatus.DRY_RUN.value
                reason = f"dry_run_would_update:{decision.reason}"
            else:
                price_write_started_at = perf_counter()
                await self.starvell_client.update_my_lot_price(
                    position.robux_amount,
                    position.lot_id,
                    decision.target_price,
                    allow_real_write=not self.dry_run,
                )
                price_write_ms = _elapsed_ms(price_write_started_at)
                status = UpdateStatus.SUCCESS.value
                self.logger.info(
                    "event_fast_path_price_updated",
                    proxy_profile=self.settings.worker_group,
                    position_amount=position.robux_amount,
                    lot_id=position.lot_id,
                    offer_id=event.get("offer_id"),
                    old_price=str(current_price),
                    new_price=str(decision.target_price),
                    competitor_price=str(decision.competitor_price),
                    event_age_ms=event_age_ms,
                    price_write_ms=price_write_ms,
                    total_event_to_write_ms=_total_event_to_now_ms(event),
                )

        await self._record_decision(
            position=position,
            decision=decision,
            old_price=current_price,
            new_price=decision.target_price,
            status=status,
            reason=reason,
        )
        if status != UpdateStatus.SUCCESS.value:
            self.logger.info(
                "event_fast_path_no_update",
                proxy_profile=self.settings.worker_group,
                position_amount=position.robux_amount,
                lot_id=position.lot_id,
                offer_id=event.get("offer_id"),
                status=status,
                reason=reason,
                current_price=str(current_price),
                target_price=str(decision.target_price),
                competitor_price=str(decision.competitor_price),
                event_age_ms=event_age_ms,
            )
        phase_metrics = self._phase_metrics(
            market_request_ms=0.0,
            my_lot_request_ms=0.0,
            price_write_ms=price_write_ms,
            cycle_started_at=event_started_at,
            parallel_fetch_enabled=True,
            parallel_fetch_disabled_reason=None,
        )
        phase_metrics["event_fast_path"] = True
        phase_metrics["event_age_ms"] = event_age_ms
        phase_metrics["total_event_to_write_ms"] = _total_event_to_now_ms(event)
        return ProcessResult(
            position.robux_amount,
            status,
            reason,
            current_price,
            decision.target_price,
            decision.competitor_price,
            phase_metrics,
        )

    def _event_fast_path_fallback(
        self,
        *,
        position: Position,
        event: dict[str, Any],
        reason: str,
        event_age_ms: int | None,
        **extra,
    ) -> None:
        self.logger.info(
            "event_fast_path_fallback",
            proxy_profile=self.settings.worker_group,
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            offer_id=event.get("offer_id"),
            reason=reason,
            event_age_ms=event_age_ms,
            **extra,
        )

    async def _timed_market_fetch(self, position_amount: int, lot_id: str):
        started_at = perf_counter()
        result = await self.starvell_client.get_market_offers_result(position_amount, lot_id)
        return result, _elapsed_ms(started_at)

    async def _timed_my_lot_fetch(self, position_amount: int, lot_id: str | None):
        started_at = perf_counter()
        result = await self.starvell_client.get_my_lot(position_amount, lot_id)
        return result, _elapsed_ms(started_at)

    def _log_cycle_profile(
        self,
        *,
        position: Position,
        status: str,
        reason: str,
        decision: PriceDecision,
        current_price: Decimal | None,
        target_price: Decimal | None,
        market_request_ms: float,
        my_lot_request_ms: float,
        price_write_ms: float,
        cycle_started_at: float,
        parallel_fetch_enabled: bool,
        parallel_fetch_disabled_reason: str | None,
    ) -> None:
        cycle_total_ms = _elapsed_ms(cycle_started_at)
        self.logger.info(
            "repricer_cycle_profile",
            proxy_profile=self.settings.worker_group,
            position=position.robux_amount,
            lot_id=position.lot_id,
            status=status,
            reason=reason,
            parallel_fetch_enabled=parallel_fetch_enabled,
            parallel_fetch_disabled_reason=parallel_fetch_disabled_reason,
            market_request_ms=market_request_ms,
            my_lot_request_ms=my_lot_request_ms,
            market_fetch_ms=market_request_ms,
            own_lot_fetch_ms=my_lot_request_ms,
            price_write_ms=price_write_ms,
            cycle_total_ms=cycle_total_ms,
            current_price=str(current_price) if current_price is not None else None,
            target_price=str(target_price) if target_price is not None else None,
            competitor_price=(
                str(decision.competitor_price)
                if decision.competitor_price is not None
                else None
            ),
        )

    def _phase_metrics(
        self,
        *,
        market_request_ms: float,
        my_lot_request_ms: float,
        price_write_ms: float,
        cycle_started_at: float,
        parallel_fetch_enabled: bool,
        parallel_fetch_disabled_reason: str | None,
    ) -> dict[str, float | bool | str | None]:
        return {
            "market_fetch_ms": market_request_ms,
            "own_lot_fetch_ms": my_lot_request_ms,
            "price_write_ms": price_write_ms,
            "cycle_total_ms": _elapsed_ms(cycle_started_at),
            "parallel_fetch_enabled": parallel_fetch_enabled,
            "parallel_fetch_disabled_reason": parallel_fetch_disabled_reason,
        }

    def _log_cycle_phase_profile(
        self,
        *,
        position: Position,
        result: ProcessResult,
        db_commit_ms: float,
    ) -> None:
        metrics = dict(result.phase_metrics)
        metrics["db_commit_ms"] = db_commit_ms
        cycle_total_ms = _metric_float(metrics.get("cycle_total_ms"))
        cycle_total_with_commit_ms = (
            round(cycle_total_ms + db_commit_ms, 2)
            if cycle_total_ms is not None
            else None
        )
        dominant_phase, dominant_phase_ms = _dominant_phase(metrics, db_commit_ms=db_commit_ms)
        log_context = {
            "proxy_profile": self.settings.worker_group,
            "position": position.robux_amount,
            "lot_id": position.lot_id,
            "status": result.status,
            "reason": result.reason,
            "market_fetch_ms": metrics.get("market_fetch_ms"),
            "own_lot_fetch_ms": metrics.get("own_lot_fetch_ms"),
            "price_write_ms": metrics.get("price_write_ms"),
            "db_commit_ms": db_commit_ms,
            "cycle_total_ms": cycle_total_ms,
            "cycle_total_with_commit_ms": cycle_total_with_commit_ms,
            "dominant_phase": dominant_phase,
            "dominant_phase_ms": dominant_phase_ms,
            "parallel_fetch_enabled": metrics.get("parallel_fetch_enabled"),
            "parallel_fetch_disabled_reason": metrics.get("parallel_fetch_disabled_reason"),
        }
        self.logger.info("repricer_cycle_phase_profile", **log_context)
        if dominant_phase_ms is not None and dominant_phase_ms >= 1000:
            self.logger.warning("repricer_cycle_phase_dominant", **log_context)

    async def _record_decision(
        self,
        *,
        position: Position,
        decision: PriceDecision,
        old_price: Decimal | None,
        new_price: Decimal | None,
        status: str,
        reason: str,
    ) -> None:
        state_current_price = new_price if status == UpdateStatus.SUCCESS.value else old_price
        await self.positions.update_state(
            position,
            last_seen_competitor_price=decision.competitor_price,
            current_own_price=state_current_price,
            calculated_price=new_price,
            error_status=None,
            error_message=None,
            success=status in {UpdateStatus.SUCCESS.value, UpdateStatus.DRY_RUN.value, UpdateStatus.SKIPPED.value},
        )
        await self.positions.add_price_log(
            position,
            old_price=old_price,
            new_price=new_price,
            competitor_price=decision.competitor_price,
            competitor_seller_id=decision.competitor.seller_id if decision.competitor else None,
            competitor_seller_username=decision.competitor.seller_username if decision.competitor else None,
            status=status,
            reason=reason,
        )

    async def _persist_failure(self, position_amount: int, exc: Exception, reason: str) -> None:
        position = await self.positions.get_by_amount(position_amount)
        if position is None:
            return
        await self.positions.update_state(
            position,
            last_seen_competitor_price=(
                position.state.last_seen_competitor_price if position.state else None
            ),
            current_own_price=position.state.current_own_price if position.state else None,
            calculated_price=position.state.calculated_price if position.state else None,
            error_status=type(exc).__name__,
            error_message=reason,
            success=False,
        )
        await self.positions.add_price_log(
            position,
            old_price=position.state.current_own_price if position.state else None,
            new_price=None,
            competitor_price=position.state.last_seen_competitor_price if position.state else None,
            competitor_seller_id=None,
            competitor_seller_username=None,
            status=UpdateStatus.FAILED.value,
            reason=reason,
        )
        await self.session.commit()

    async def _record_missing_lot(self, position: Position) -> None:
        message = "Не найден ID лота. Репрайс невозможен."
        await self.positions.update_state(
            position,
            last_seen_competitor_price=(
                position.state.last_seen_competitor_price if position.state else None
            ),
            current_own_price=position.state.current_own_price if position.state else None,
            calculated_price=None,
            error_status="missing_lot_id",
            error_message=message,
            success=False,
        )
        await self.positions.add_price_log(
            position,
            old_price=position.state.current_own_price if position.state else None,
            new_price=None,
            competitor_price=position.state.last_seen_competitor_price if position.state else None,
            competitor_seller_id=None,
            competitor_seller_username=None,
            status=UpdateStatus.SKIPPED.value,
            reason="missing_lot_id",
        )

    def _current_price(self, position: Position, own_lot: OwnLot | None) -> Decimal | None:
        if own_lot is not None:
            return own_lot.price
        if position.state is not None:
            return position.state.current_own_price
        return None


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 2)


def _metric_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dominant_phase(
    metrics: dict[str, float | bool | str | None],
    *,
    db_commit_ms: float,
) -> tuple[str | None, float | None]:
    phase_values = {
        "market_fetch": _metric_float(metrics.get("market_fetch_ms")),
        "own_lot_fetch": _metric_float(metrics.get("own_lot_fetch_ms")),
        "price_write": _metric_float(metrics.get("price_write_ms")),
        "db_commit": db_commit_ms,
    }
    normalized = {
        name: value
        for name, value in phase_values.items()
        if value is not None
    }
    if not normalized:
        return None, None
    phase, value = max(normalized.items(), key=lambda item: item[1])
    return phase, round(value, 2)


def _decimal_or_none(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except Exception:
        return None


def _bool_or_none(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _event_age_ms(event: dict[str, Any]) -> int | None:
    detected_at_ms = event.get("detected_at_ms")
    if isinstance(detected_at_ms, (int, float)):
        return max(int(_epoch_ms() - detected_at_ms), 0)
    event_age = event.get("event_age_ms")
    if isinstance(event_age, (int, float)):
        return max(int(event_age), 0)
    return None


def _total_event_to_now_ms(event: dict[str, Any]) -> int | None:
    return _event_age_ms(event)


def _epoch_ms() -> int:
    import time

    return int(time.time() * 1000)


def _market_offer_from_event(
    position_amount: int,
    event: dict[str, Any],
    price: Decimal,
) -> MarketOffer:
    return MarketOffer(
        position_amount=position_amount,
        price=price,
        seller_id=str(event["seller_id"]) if event.get("seller_id") is not None else None,
        seller_username=(
            str(event["seller_username"]) if event.get("seller_username") is not None else None
        ),
        rating=_decimal_or_none(event.get("rating")),
        is_active=_bool_or_none(event.get("is_active")),
        raw_payload={
            "id": event.get("offer_id"),
            "source": event.get("source"),
            "price": str(price),
            "event": event,
        },
    )
