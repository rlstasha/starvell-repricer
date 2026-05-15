from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.models import Position, UpdateStatus
from app.db.repositories import PositionRepository
from app.market.client import StarvellClient
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
            await self.session.commit()
            return result
        except Exception as exc:
            await self.session.rollback()
            await self._persist_failure(position_amount, exc)
            self.logger.exception(
                "repricer_position_failed",
                position_amount=position_amount,
                error=str(exc),
            )
            return ProcessResult(position_amount, "failed", str(exc), None, None, None)

    async def _process_loaded_position(self, position: Position) -> ProcessResult:
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

        market_result = await self.starvell_client.get_market_offers_result(
            position.robux_amount,
            position.lot_id,
        )
        offers = market_result.offers
        own_lot = await self.starvell_client.get_my_lot(position.robux_amount, position.lot_id)
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
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.SKIPPED.value,
                decision.reason,
                current_price,
                None,
                decision.competitor_price,
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
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.SKIPPED.value,
                decision.reason,
                current_price,
                decision.target_price,
                decision.competitor_price,
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
                position_amount=position.robux_amount,
                old_price=str(current_price),
                new_price=str(decision.target_price),
                competitor_price=str(decision.competitor_price),
            )
            return ProcessResult(
                position.robux_amount,
                UpdateStatus.DRY_RUN.value,
                decision.reason,
                current_price,
                decision.target_price,
                decision.competitor_price,
            )

        await self.starvell_client.update_my_lot_price(
            position.robux_amount,
            position.lot_id,
            decision.target_price,
        )
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
            position_amount=position.robux_amount,
            old_price=str(current_price),
            new_price=str(decision.target_price),
            competitor_price=str(decision.competitor_price),
        )
        return ProcessResult(
            position.robux_amount,
            UpdateStatus.SUCCESS.value,
            decision.reason,
            current_price,
            decision.target_price,
            decision.competitor_price,
        )

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

    async def _persist_failure(self, position_amount: int, exc: Exception) -> None:
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
            error_message=str(exc),
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
            reason=str(exc),
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
