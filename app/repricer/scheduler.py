import asyncio
import heapq
import json
import os
import random
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import (
    AppSettingsRepository,
    PositionScheduleStateRepository,
    PositionRepository,
    WorkerHeartbeatRepository,
    WorkerStateRepository,
)
from app.db.models import Position
from app.market.client import StarvellClient, safe_starvell_error_reason
from app.repricer.engine import RepricerEngine
from app.repricer.locks import RedisPositionLock
from app.core.network import mask_proxy_url
from app.repricer.adaptive_scheduler import (
    apply_strategy_activity_floor,
    choose_dynamic_delay,
    display_interval_range,
    is_active_strategy_reason,
    timing_for_group,
    timing_for_position,
    update_change_score,
    update_error_score,
)
from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    NoopRateLimiter,
    RedisAdaptiveTokenBucketRateLimiter,
    RedisFixedWindowRateLimiter,
    RedisTokenBucketRateLimiter,
    adaptive_backoff_seconds,
    transport_backoff_seconds,
)
from app.repricer.worker_groups import (
    WORKER_GROUP_ALL,
    WORKER_GROUP_FAST_1,
    WORKER_GROUP_FAST_2,
    WORKER_GROUP_SLOW,
)


RAMP_UP_IDLE_SECONDS = 10 * 60
RAMP_UP_STEP_PER_MINUTE = 10
MIN_EFFECTIVE_REQUEST_LIMIT_PER_MINUTE = 10


@dataclass
class RuntimeScheduleState:
    position_amount: int
    lot_id: str | None
    proxy_profile: str
    base_interval_seconds: float
    current_interval_seconds: float
    next_run_monotonic: float
    last_checked_at: datetime | None
    last_competitor_price: Decimal | None
    last_own_price: Decimal | None
    change_score: float = 0.5
    error_score: float = 0.0
    last_429_at: datetime | None = None
    interval_min_seconds: float | None = None
    interval_max_seconds: float | None = None
    delay_reason: str = "normal"
    hot_last_update_monotonic: float | None = None
    hot_consecutive_target_skips: int = 0


class RepricerScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        public_ip: str | None = None,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis
        self.public_ip = public_ip
        self.hostname = socket.gethostname()
        self.worker_id = f"{self.hostname}:{os.getpid()}"
        self.position_lock = RedisPositionLock(
            redis,
            ttl_seconds=settings.position_lock_ttl_seconds,
            owner=self.worker_id,
        )
        self.schedule_runtime: dict[int, RuntimeScheduleState] = {}
        self.schedule_heap: list[tuple[float, float, int]] = []
        self.current_delay_seconds: float | None = None
        min_interval, max_interval = display_interval_range(settings.worker_group)
        self.interval_min_seconds = min_interval
        self.interval_max_seconds = max_interval
        self.errors_429 = 0
        self.errors_403 = 0
        self.errors_timeout = 0
        self.consecutive_errors = 0
        self.safe_mode_until = 0.0
        self.last_error_kind: str | None = None
        self.last_safe_mode_delay_seconds = 0.0
        self.configured_request_limit_per_minute = settings.worker_request_limit_per_minute
        self.effective_request_limit_per_minute = self.configured_request_limit_per_minute
        self.last_429_at: datetime | None = None
        self.last_limit_ramp_monotonic = time.monotonic()
        self.rate_limiter = self._build_rate_limiter()
        self._last_limiter_snapshot_logged_at = 0.0
        self.logger = get_logger(__name__)

    async def run_forever(self) -> None:
        proxy_url = self.settings.proxy_url_for_group()
        self.logger.info(
            "repricer_scheduler_started",
            worker_group=self.settings.worker_group,
            proxy_profile=self.settings.worker_group,
            proxy=mask_proxy_url(proxy_url),
            request_limit_per_minute=self.settings.worker_request_limit_per_minute,
            effective_request_limit_per_minute=self.effective_request_limit_per_minute,
        )
        async with StarvellClient(
            self.settings,
            self.rate_limiter,
            proxy_profile=self.settings.worker_group,
            proxy_url=proxy_url,
        ) as starvell_client:
            dedicated_tasks = self._start_dedicated_position_tasks(starvell_client)
            try:
                while True:
                    try:
                        await self.run_once(starvell_client)
                    except Exception as exc:
                        error = safe_starvell_error_reason(exc)
                        await self._mark_error(exc)
                        self.logger.exception(
                            "repricer_scheduler_cycle_failed",
                            worker_group=self.settings.worker_group,
                            error=error,
                        )
                        async with self.session_factory() as session:
                            await WorkerStateRepository(session).mark_cycle(
                                name=self._worker_state_name(),
                                position_amount=None,
                                status="failed",
                                error=error,
                            )
                            await self._write_heartbeat(
                                session,
                                status="failed",
                                dry_run=self.settings.dry_run,
                            )
                            await session.commit()
                        await asyncio.sleep(self._idle_sleep_seconds())
            finally:
                for task in dedicated_tasks:
                    task.cancel()
                if dedicated_tasks:
                    await asyncio.gather(*dedicated_tasks, return_exceptions=True)

    def _dedicated_position_amounts(self) -> set[int]:
        if not self.settings.dedicated_position_tasks_enabled:
            return set()
        assigned = set(self.settings.assigned_positions)
        return {
            amount
            for amount in self.settings.dedicated_position_task_amounts
            if amount in assigned
        }

    def _start_dedicated_position_tasks(self, starvell_client: StarvellClient) -> list[asyncio.Task]:
        amounts = sorted(self._dedicated_position_amounts())
        if not amounts:
            return []
        self.logger.info(
            "repricer_dedicated_position_tasks_started",
            worker_group=self.settings.worker_group,
            positions=amounts,
            idle_sleep_seconds=self.settings.dedicated_position_idle_sleep_seconds,
        )
        return [
            asyncio.create_task(
                self._run_dedicated_position_loop(amount, starvell_client),
                name=f"repricer-dedicated-{self.settings.worker_group}-{amount}",
            )
            for amount in amounts
        ]

    async def _run_dedicated_position_loop(
        self,
        amount: int,
        starvell_client: StarvellClient,
    ) -> None:
        while True:
            try:
                if self._safe_mode_active():
                    await asyncio.sleep(
                        max(
                            min(
                                self._safe_mode_remaining_seconds(),
                                self.settings.dedicated_position_idle_sleep_seconds,
                            ),
                            self.settings.dedicated_position_idle_sleep_seconds,
                        )
                    )
                    continue

                async with self.session_factory() as session:
                    position = await PositionRepository(session).get_by_amount(amount)
                    if (
                        position is None
                        or not position.enabled
                        or amount not in set(self.settings.assigned_positions)
                    ):
                        await session.commit()
                        await asyncio.sleep(self._idle_sleep_seconds())
                        continue
                    await self._ensure_runtime_state(position, session)
                    state = self.schedule_runtime.get(amount)
                    if state is None:
                        await session.commit()
                        await asyncio.sleep(self.settings.dedicated_position_idle_sleep_seconds)
                        continue
                    price_change_event = await self._consume_price_change_event(amount)
                    if price_change_event:
                        state.next_run_monotonic = min(state.next_run_monotonic, time.monotonic())
                        self.logger.info(
                            "repricer_price_change_event_consumed",
                            worker_group=self.settings.worker_group,
                            positions=[amount],
                            source="price_watcher",
                            dedicated_task=True,
                            event_age_ms=price_change_event.get("event_age_ms"),
                            offer_id=price_change_event.get("offer_id"),
                            new_price=price_change_event.get("new_price"),
                        )
                    wait_seconds = max(state.next_run_monotonic - time.monotonic(), 0.0)
                    if wait_seconds > 0:
                        await session.commit()
                        await asyncio.sleep(
                            min(wait_seconds, self.settings.dedicated_position_idle_sleep_seconds)
                        )
                        continue
                    dry_run = await AppSettingsRepository(session).get_bool(
                        "dry_run",
                        default=self.settings.dry_run,
                    )
                    await session.commit()

                self.logger.info(
                    "repricer_dedicated_position_due",
                    worker_group=self.settings.worker_group,
                    position_amount=amount,
                )
                await self._process_due_position(position, starvell_client, dry_run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = safe_starvell_error_reason(exc)
                await self._mark_error(exc)
                self.logger.exception(
                    "repricer_dedicated_position_failed",
                    worker_group=self.settings.worker_group,
                    position_amount=amount,
                    error=error,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(self._idle_sleep_seconds())

    async def run_once(self, starvell_client: StarvellClient) -> None:
        if self._safe_mode_active():
            remaining = self._safe_mode_remaining_seconds()
            async with self.session_factory() as session:
                await self._write_heartbeat(
                    session,
                    status=self._safe_mode_status(),
                    dry_run=self.settings.dry_run,
                )
                await session.commit()
            await asyncio.sleep(max(remaining, 0.1))
            return

        async with self.session_factory() as session:
            repository = PositionRepository(session)
            positions = await repository.list_enabled_positions()
            positions = self._filter_assigned_positions(positions)

            if not positions:
                await self._mark_idle(
                    session,
                    "Нет включенных позиций этой группы",
                )
                await asyncio.sleep(self._idle_sleep_seconds())
                return

            await self._sync_schedule(positions, session)
            positions_by_amount = {position.robux_amount: position for position in positions}
            await self._apply_price_change_events(positions_by_amount)
            due_positions = self._due_positions(
                positions_by_amount,
                limit=self.settings.scheduler_max_concurrent_positions,
            )
            if not due_positions:
                await self._write_heartbeat(
                    session,
                    status="waiting",
                    dry_run=self.settings.dry_run,
                )
                await session.commit()
                await asyncio.sleep(self._next_schedule_wait_seconds())
                return

            dry_run = await AppSettingsRepository(session).get_bool(
                "dry_run",
                default=self.settings.dry_run,
            )
            await session.commit()

        self.logger.info(
            "repricer_due_positions_selected",
            worker_group=self.settings.worker_group,
            count=len(due_positions),
            positions=[position.robux_amount for position in due_positions],
            max_concurrent_positions=self.settings.scheduler_max_concurrent_positions,
        )
        semaphore = asyncio.Semaphore(self.settings.scheduler_max_concurrent_positions)

        async def run_position(position: Position):
            async with semaphore:
                return await self._process_due_position(position, starvell_client, dry_run)

        results = await asyncio.gather(
            *(run_position(position) for position in due_positions),
            return_exceptions=True,
        )
        processed_count = 0
        for result in results:
            if isinstance(result, Exception):
                await self._mark_error(result)
                self.logger.error(
                    "repricer_due_position_task_failed",
                    worker_group=self.settings.worker_group,
                    error=safe_starvell_error_reason(result),
                    error_type=type(result).__name__,
                )
                continue
            if result is not None:
                processed_count += 1
        if processed_count == 0:
            await asyncio.sleep(0.2)
        return

    async def _sync_schedule(self, positions: list[Position], session: AsyncSession) -> None:
        repository = PositionScheduleStateRepository(session)
        persisted_by_position_id = {
            item.position_id: item
            for item in await repository.list_all()
        }
        now = time.monotonic()
        active_amounts = {position.robux_amount for position in positions} | self._dedicated_position_amounts()

        for position in positions:
            existing = self.schedule_runtime.get(position.robux_amount)
            if existing is not None:
                existing.lot_id = position.lot_id
                continue

            persisted = persisted_by_position_id.get(position.id)
            self.schedule_runtime[position.robux_amount] = self._build_runtime_state(
                position=position,
                persisted=persisted,
                now_monotonic=now,
            )

        for amount in set(self.schedule_runtime) - active_amounts:
            self.schedule_runtime.pop(amount, None)
        self._rebuild_schedule_heap()

    async def _ensure_runtime_state(
        self,
        position: Position,
        session: AsyncSession,
    ) -> RuntimeScheduleState:
        existing = self.schedule_runtime.get(position.robux_amount)
        if existing is not None:
            existing.lot_id = position.lot_id
            return existing
        persisted = await PositionScheduleStateRepository(session).get_by_position_id(position.id)
        state = self._build_runtime_state(
            position=position,
            persisted=persisted,
            now_monotonic=time.monotonic(),
        )
        self.schedule_runtime[position.robux_amount] = state
        return state

    def _build_runtime_state(
        self,
        *,
        position: Position,
        persisted,
        now_monotonic: float,
    ) -> RuntimeScheduleState:
        timing = timing_for_position(self.settings.worker_group, position.robux_amount)
        current_interval = (
            persisted.current_interval_seconds
            if persisted is not None
            else timing.base_seconds
        )
        next_run = now_monotonic
        if persisted is not None and persisted.last_checked_at is not None:
            elapsed = (datetime.now(UTC) - persisted.last_checked_at).total_seconds()
            next_run = now_monotonic + max(current_interval - elapsed, 0.0)
        interval_min, interval_max = display_interval_range(
            self.settings.worker_group,
            position_amount=position.robux_amount,
        )
        return RuntimeScheduleState(
            position_amount=position.robux_amount,
            lot_id=position.lot_id,
            proxy_profile=self.settings.worker_group,
            base_interval_seconds=timing.base_seconds,
            current_interval_seconds=current_interval,
            next_run_monotonic=next_run,
            last_checked_at=persisted.last_checked_at if persisted else None,
            last_competitor_price=(
                persisted.last_competitor_price
                if persisted
                else position.state.last_seen_competitor_price if position.state else None
            ),
            last_own_price=(
                persisted.last_own_price
                if persisted
                else position.state.current_own_price if position.state else None
            ),
            change_score=persisted.change_score if persisted else 0.5,
            error_score=persisted.error_score if persisted else 0.0,
            last_429_at=persisted.last_429_at if persisted else None,
            interval_min_seconds=interval_min,
            interval_max_seconds=interval_max,
        )

    def _rebuild_schedule_heap(self) -> None:
        self.schedule_heap = [
            (state.next_run_monotonic, -state.change_score, amount)
            for amount, state in self.schedule_runtime.items()
        ]
        heapq.heapify(self.schedule_heap)

    async def _apply_price_change_events(self, positions_by_amount: dict[int, Position]) -> None:
        triggered_amounts: list[int] = []
        event_details: list[dict[str, Any]] = []
        now = time.monotonic()
        for amount in positions_by_amount:
            price_change_event = await self._consume_price_change_event(amount)
            if not price_change_event:
                continue
            state = self.schedule_runtime.get(amount)
            if state is None:
                continue
            state.next_run_monotonic = min(state.next_run_monotonic, now)
            triggered_amounts.append(amount)
            event_details.append(
                {
                    "position_amount": amount,
                    "event_age_ms": price_change_event.get("event_age_ms"),
                    "offer_id": price_change_event.get("offer_id"),
                    "new_price": price_change_event.get("new_price"),
                }
            )

        if not triggered_amounts:
            return
        self._rebuild_schedule_heap()
        self.logger.info(
            "repricer_price_change_event_consumed",
            worker_group=self.settings.worker_group,
            positions=triggered_amounts,
            source="price_watcher",
            events=event_details,
        )

    async def _consume_price_change_event(self, amount: int) -> dict[str, Any] | None:
        key = f"repricer:price_change_event:{amount}"
        try:
            raw_payload = None
            if hasattr(self.redis, "get"):
                raw_payload = await self.redis.get(key)
            deleted = await self.redis.delete(key)
            if not deleted:
                return None
            return self._parse_price_change_event(raw_payload)
        except Exception as exc:
            self.logger.warning(
                "repricer_price_change_event_check_failed",
                worker_group=self.settings.worker_group,
                position_amount=amount,
                error=safe_starvell_error_reason(exc),
                error_type=type(exc).__name__,
            )
            return None

    @staticmethod
    def _parse_price_change_event(raw_payload) -> dict[str, Any]:
        event: dict[str, Any] = {"source": "price_watcher"}
        if raw_payload:
            if isinstance(raw_payload, bytes):
                raw_payload = raw_payload.decode("utf-8", errors="replace")
            if isinstance(raw_payload, str) and raw_payload.strip() and raw_payload != "1":
                try:
                    parsed = json.loads(raw_payload)
                except ValueError:
                    parsed = {}
                if isinstance(parsed, dict):
                    event.update(parsed)
        detected_at_ms = event.get("detected_at_ms")
        if isinstance(detected_at_ms, (int, float)):
            event["event_age_ms"] = max(int(time.time() * 1000 - detected_at_ms), 0)
        return event

    def _next_due_position(self, positions_by_amount: dict[int, Position]) -> Position | None:
        now = time.monotonic()
        while self.schedule_heap:
            next_run, _, amount = self.schedule_heap[0]
            state = self.schedule_runtime.get(amount)
            if state is None or state.next_run_monotonic != next_run:
                heapq.heappop(self.schedule_heap)
                continue
            if next_run > now:
                return None
            heapq.heappop(self.schedule_heap)
            return positions_by_amount.get(amount)
        return None

    def _due_positions(self, positions_by_amount: dict[int, Position], *, limit: int) -> list[Position]:
        # A future per-position task model would remove the heap bottleneck entirely,
        # but it needs separate lifecycle/backoff supervision. This keeps the heap
        # scheduler and only processes currently due positions with bounded fan-out.
        now = time.monotonic()
        due: list[Position] = []
        normalized_limit = max(int(limit), 1)
        while self.schedule_heap and len(due) < normalized_limit:
            next_run, _, amount = self.schedule_heap[0]
            state = self.schedule_runtime.get(amount)
            if state is None or state.next_run_monotonic != next_run:
                heapq.heappop(self.schedule_heap)
                continue
            if next_run > now:
                break
            heapq.heappop(self.schedule_heap)
            position = positions_by_amount.get(amount)
            if position is not None:
                due.append(position)
        return due

    async def _process_due_position(
        self,
        position: Position,
        starvell_client: StarvellClient,
        dry_run: bool,
    ):
        async with self.session_factory() as session:
            if not await self.position_lock.acquire(position.robux_amount):
                self.logger.info(
                    "repricer_position_lock_busy",
                    worker_group=self.settings.worker_group,
                    position_amount=position.robux_amount,
                )
                self._postpone_locked_position(position.robux_amount)
                await self._write_heartbeat(session, status="lock_busy", dry_run=dry_run)
                await session.commit()
                return None

            engine = RepricerEngine(
                session=session,
                settings=self.settings,
                starvell_client=starvell_client,
                dry_run=dry_run,
            )
            try:
                result = await engine.process_position(position.robux_amount)
            finally:
                await self.position_lock.release(position.robux_amount)

            schedule_state = self._update_schedule_after_result(position, result)
            await PositionScheduleStateRepository(session).upsert(
                position=position,
                proxy_profile=self.settings.worker_group,
                base_interval_seconds=schedule_state.base_interval_seconds,
                current_interval_seconds=schedule_state.current_interval_seconds,
                last_checked_at=schedule_state.last_checked_at,
                last_competitor_price=schedule_state.last_competitor_price,
                last_own_price=schedule_state.last_own_price,
                change_score=schedule_state.change_score,
                error_score=schedule_state.error_score,
                last_429_at=schedule_state.last_429_at,
            )
            await WorkerStateRepository(session).mark_cycle(
                name=self._worker_state_name(),
                position_amount=result.position_amount,
                status=result.status,
                error=result.reason if result.status == "failed" else None,
            )
            await self._update_error_state(result.status, result.reason)
            await self._write_heartbeat(
                session,
                status=self._heartbeat_status(result.status),
                dry_run=dry_run,
            )
            await session.commit()
            self.logger.info(
                "repricer_position_processed",
                worker_group=self.settings.worker_group,
                proxy_profile=self.settings.worker_group,
                position_amount=result.position_amount,
                status=result.status,
                reason=result.reason,
                old_price=str(result.old_price),
                new_price=str(result.new_price),
                competitor_price=str(result.competitor_price),
            )
            return result

    def _next_schedule_wait_seconds(self) -> float:
        while self.schedule_heap:
            next_run, _, amount = self.schedule_heap[0]
            state = self.schedule_runtime.get(amount)
            if state is None or state.next_run_monotonic != next_run:
                heapq.heappop(self.schedule_heap)
                continue
            return max(min(next_run - time.monotonic(), self._idle_sleep_seconds()), 0.1)
        return self._idle_sleep_seconds()

    def _idle_sleep_seconds(self) -> float:
        return self.settings.scheduler_idle_sleep_for_group(self.settings.worker_group)

    def _postpone_locked_position(self, amount: int) -> None:
        state = self.schedule_runtime.get(amount)
        if state is None:
            return
        state.next_run_monotonic = time.monotonic() + 0.5
        heapq.heappush(self.schedule_heap, (state.next_run_monotonic, -state.change_score, amount))

    def _update_schedule_after_result(self, position: Position, result) -> RuntimeScheduleState:
        state = self.schedule_runtime[position.robux_amount]
        error_kind = self._error_kind(result.status, result.reason)
        competitor_changed = (
            state.last_competitor_price is not None
            and result.competitor_price is not None
            and state.last_competitor_price != result.competitor_price
        )
        target_equals_current = (
            result.status == "skipped"
            and result.old_price is not None
            and result.new_price is not None
            and result.old_price == result.new_price
        )
        change_score = update_change_score(
            state.change_score,
            state.last_competitor_price,
            result.competitor_price,
        )
        strategy_activity_override = is_active_strategy_reason(result.reason)
        change_score = apply_strategy_activity_floor(change_score, result.reason)
        error_score = update_error_score(state.error_score, failed=result.status == "failed")
        backoff_active = self._backoff_active() or error_kind == "429"
        backoff_reason = (
            "backoff_after_429"
            if error_kind == "429" or self.last_error_kind == "429"
            else "backoff_after_error"
        )
        decision = choose_dynamic_delay(
            worker_group=self.settings.worker_group,
            position_amount=position.robux_amount,
            change_score=change_score,
            error_score=error_score,
            backoff_active=backoff_active,
            backoff_reason=backoff_reason,
            previous_delay_seconds=state.current_interval_seconds,
        )
        now_monotonic = time.monotonic()
        hot_mode_reason = None
        if result.status == "success":
            state.hot_last_update_monotonic = now_monotonic
            state.hot_consecutive_target_skips = 0
        elif competitor_changed:
            state.hot_consecutive_target_skips = 0
        elif target_equals_current:
            state.hot_consecutive_target_skips += 1
        elif result.status == "failed":
            state.hot_consecutive_target_skips = 0

        decision, hot_mode_reason = self._maybe_override_dynamic_delay_for_hot_mode(
            position=position,
            state=state,
            decision=decision,
            backoff_active=backoff_active,
            competitor_changed=competitor_changed,
            now_monotonic=now_monotonic,
        )
        timing = timing_for_position(self.settings.worker_group, position.robux_amount)
        post_update_reason = None
        decision, post_update_reason = self._maybe_override_post_update_delay(
            position=position,
            decision=decision,
            backoff_active=backoff_active,
            timing=timing,
            result_status=result.status,
        )
        if backoff_active:
            interval_min, interval_max = display_interval_range(
                self.settings.worker_group,
                position_amount=position.robux_amount,
                backoff_active=True,
            )
        elif hot_mode_reason or post_update_reason:
            interval_min, interval_max = decision.range_min_seconds, decision.range_max_seconds
        else:
            interval_min, interval_max = timing.min_seconds, timing.max_seconds
        checked_at = datetime.now(UTC)
        state.current_interval_seconds = decision.delay_seconds
        state.interval_min_seconds = interval_min
        state.interval_max_seconds = interval_max
        state.delay_reason = decision.reason
        state.next_run_monotonic = now_monotonic + decision.delay_seconds
        state.last_checked_at = checked_at
        state.last_competitor_price = result.competitor_price
        state.last_own_price = result.old_price
        state.change_score = change_score
        state.error_score = error_score
        if error_kind == "429":
            state.last_429_at = checked_at
        self.current_delay_seconds = decision.delay_seconds
        self.interval_min_seconds = interval_min
        self.interval_max_seconds = interval_max
        heapq.heappush(self.schedule_heap, (state.next_run_monotonic, -state.change_score, position.robux_amount))
        self.logger.info(
            "repricer_dynamic_delay_selected",
            profile=self.settings.worker_group,
            delay=decision.delay_seconds,
            reason=decision.reason,
            position_amount=position.robux_amount,
            change_score=round(change_score, 3),
            error_score=round(error_score, 3),
            strategy_reason=result.reason,
            activity_override="min_price_bounce" if strategy_activity_override else None,
            hot_mode_reason=hot_mode_reason,
            post_update_reason=post_update_reason,
            post_update_interval_multiplier=self.settings.post_update_interval_multiplier,
            hot_consecutive_target_skips=state.hot_consecutive_target_skips,
            competitor_changed=competitor_changed,
        )
        if position.robux_amount == 500 and self.settings.worker_group == WORKER_GROUP_FAST_1 and backoff_active:
            self.logger.warning(
                "ultra_fast_backoff",
                profile=self.settings.worker_group,
                position_amount=position.robux_amount,
                effective_delay=decision.delay_seconds,
                reason=decision.reason,
                last_429_at=self.last_429_at.isoformat() if self.last_429_at else None,
            )
        return state

    def _maybe_override_post_update_delay(
        self,
        *,
        position: Position,
        decision,
        backoff_active: bool,
        timing,
        result_status: str,
    ):
        multiplier = self.settings.post_update_interval_multiplier
        enabled_positions = self.settings.post_update_interval_position_amounts
        if (
            backoff_active
            or result_status != "success"
            or multiplier == 1.0
            or (
                enabled_positions
                and position.robux_amount not in enabled_positions
            )
        ):
            return decision, None
        delay = round(max(timing.min_seconds * multiplier, 0.01), 2)
        return (
            self._replace_delay_decision(
                decision,
                delay,
                delay,
                delay,
                "post_update_followup",
            ),
            "post_update_followup",
        )

    def _maybe_override_dynamic_delay_for_hot_mode(
        self,
        *,
        position: Position,
        state: RuntimeScheduleState,
        decision,
        backoff_active: bool,
        competitor_changed: bool,
        now_monotonic: float,
    ):
        if backoff_active:
            return decision, None

        if self.settings.hot_mode_enabled and self._is_ultra_hot_position(position):
            return self._hot_mode_decision(
                state=state,
                decision=decision,
                competitor_changed=competitor_changed,
                now_monotonic=now_monotonic,
            )

        if self.settings.fast_mode_enabled and self._is_fast_mode_position(position):
            return self._fast_mode_decision(
                state=state,
                decision=decision,
                competitor_changed=competitor_changed,
                now_monotonic=now_monotonic,
            )

        return decision, None

    def _is_ultra_hot_position(self, position: Position) -> bool:
        return self.settings.worker_group == WORKER_GROUP_FAST_1 and position.robux_amount == 500

    def _is_fast_mode_position(self, position: Position) -> bool:
        if self.settings.worker_group not in {WORKER_GROUP_FAST_1, WORKER_GROUP_FAST_2}:
            return False
        if self._is_ultra_hot_position(position):
            return False
        return True

    def _hot_mode_decision(
        self,
        *,
        state: RuntimeScheduleState,
        decision,
        competitor_changed: bool,
        now_monotonic: float,
    ):
        if self._hot_mode_should_cool_down(state, competitor_changed):
            low = max(self.settings.hot_mode_cooldown_interval_seconds * 0.8, 0.01)
            high = max(self.settings.hot_mode_cooldown_interval_seconds * 1.2, low)
            delay = self._bounded_random_delay(low, high, state.current_interval_seconds)
            return self._replace_delay_decision(decision, delay, low, high, "hot_mode_cooldown"), "cooldown"

        if competitor_changed or self._recent_hot_update(state, now_monotonic):
            low = self.settings.hot_mode_min_interval_seconds
            high = self.settings.hot_mode_max_interval_seconds
            delay = self._bounded_random_delay(low, high, state.current_interval_seconds)
            return self._replace_delay_decision(decision, delay, low, high, "hot_mode_active"), "active"

        return decision, None

    def _fast_mode_decision(
        self,
        *,
        state: RuntimeScheduleState,
        decision,
        competitor_changed: bool,
        now_monotonic: float,
    ):
        if self._hot_mode_should_cool_down(state, competitor_changed):
            low = self.settings.fast_mode_cooldown_min_interval_seconds
            high = self.settings.fast_mode_cooldown_max_interval_seconds
            delay = self._bounded_random_delay(low, high, state.current_interval_seconds)
            return self._replace_delay_decision(decision, delay, low, high, "fast_mode_cooldown"), "cooldown"

        if competitor_changed or self._recent_hot_update(state, now_monotonic):
            low = self.settings.fast_mode_min_interval_seconds
            high = self.settings.fast_mode_max_interval_seconds
            delay = self._bounded_random_delay(low, high, state.current_interval_seconds)
            return self._replace_delay_decision(decision, delay, low, high, "fast_mode_active"), "active"

        return decision, None

    def _hot_mode_should_cool_down(
        self,
        state: RuntimeScheduleState,
        competitor_changed: bool,
    ) -> bool:
        return (
            not competitor_changed
            and state.hot_consecutive_target_skips >= self.settings.hot_mode_skipped_threshold
        )

    def _recent_hot_update(self, state: RuntimeScheduleState, now_monotonic: float) -> bool:
        if state.hot_last_update_monotonic is None:
            return False
        return now_monotonic - state.hot_last_update_monotonic <= self.settings.hot_mode_window_seconds

    def _bounded_random_delay(
        self,
        low: float,
        high: float,
        previous_delay: float | None,
    ) -> float:
        normalized_low = max(min(low, high), 0.01)
        normalized_high = max(high, normalized_low)
        delay = random.uniform(normalized_low, normalized_high)
        if previous_delay is not None and abs(delay - previous_delay) < 0.05:
            if delay + 0.08 <= normalized_high:
                delay += 0.08
            elif delay - 0.08 >= normalized_low:
                delay -= 0.08
        return round(delay, 2)

    @staticmethod
    def _replace_delay_decision(decision, delay: float, low: float, high: float, reason: str):
        return type(decision)(
            delay_seconds=delay,
            reason=reason,
            range_min_seconds=round(low, 2),
            range_max_seconds=round(high, 2),
        )

    def _filter_assigned_positions(self, positions):
        if self.settings.worker_group == WORKER_GROUP_ALL:
            filtered = positions
        else:
            assigned = set(self.settings.assigned_positions)
            filtered = [position for position in positions if position.robux_amount in assigned]
        dedicated = self._dedicated_position_amounts()
        if dedicated:
            filtered = [
                position
                for position in filtered
                if position.robux_amount not in dedicated
            ]
        return filtered

    async def _mark_idle(self, session: AsyncSession, reason: str) -> None:
        self.logger.warning(
            "repricer_worker_idle",
            worker_group=self.settings.worker_group,
            reason=reason,
        )
        await WorkerStateRepository(session).mark_cycle(
            name=self._worker_state_name(),
            position_amount=None,
            status="idle",
            error=reason,
        )
        await self._write_heartbeat(
            session,
            status="idle",
            dry_run=self.settings.dry_run,
        )
        await session.commit()

    async def _write_heartbeat(
        self,
        session: AsyncSession,
        *,
        status: str,
        dry_run: bool,
    ) -> None:
        self._maybe_ramp_up_limit()
        profile_usage = await self.rate_limiter.current_usage()
        account_snapshot = (
            await self.rate_limiter.account_snapshot()
            if hasattr(self.rate_limiter, "account_snapshot")
            else None
        )
        self._log_limiter_snapshot(
            profile_usage=profile_usage,
            account_snapshot=account_snapshot,
        )
        await WorkerHeartbeatRepository(session).upsert(
            worker_group=self.settings.worker_group,
            hostname=self.hostname,
            public_ip=self.public_ip,
            assigned_positions=list(self.settings.assigned_positions),
            request_limit_per_minute=self.settings.worker_request_limit_per_minute,
            effective_request_limit_per_minute=self.effective_request_limit_per_minute,
            profile_request_usage_per_minute=profile_usage,
            account_effective_limit_per_minute=(
                account_snapshot.effective_limit_per_minute if account_snapshot else None
            ),
            account_request_usage_per_minute=(
                account_snapshot.current_usage if account_snapshot else 0
            ),
            account_backoff_active=(
                account_snapshot.backoff_active if account_snapshot else False
            ),
            account_last_429_at=(
                account_snapshot.last_429_at if account_snapshot else None
            ),
            account_retry_after_until=(
                account_snapshot.retry_after_until if account_snapshot else None
            ),
            current_delay_seconds=self.current_delay_seconds,
            interval_min_seconds=self.interval_min_seconds,
            interval_max_seconds=self.interval_max_seconds,
            most_active_position_amount=self._most_active_position_amount(),
            status=status,
            errors_429=self.errors_429,
            errors_403=self.errors_403,
            errors_timeout=self.errors_timeout,
            consecutive_errors=self.consecutive_errors,
            backoff_active=self._backoff_active(),
            last_429_at=self.last_429_at,
            safe_mode=self._safe_mode_active(),
            dry_run=dry_run,
        )

    def _log_limiter_snapshot(
        self,
        *,
        profile_usage: int,
        account_snapshot,
    ) -> None:
        now = time.time()
        if now - self._last_limiter_snapshot_logged_at < 10:
            return
        self._last_limiter_snapshot_logged_at = now
        account_configured = (
            account_snapshot.configured_limit_per_minute
            if account_snapshot
            else self.settings.global_request_limit_per_minute
        )
        account_effective = (
            account_snapshot.effective_limit_per_minute
            if account_snapshot
            else self.settings.global_request_limit_per_minute
        )
        account_usage = account_snapshot.current_usage if account_snapshot else 0
        self.logger.info(
            "repricer_limiter_snapshot",
            worker_group=self.settings.worker_group,
            soft_cap_enabled=self.settings.rate_limiter_soft_cap_enabled,
            token_limit_mode=self.settings.token_limit_mode,
            profile_configured_limit_per_minute=self.configured_request_limit_per_minute,
            profile_effective_limit_per_minute=self.effective_request_limit_per_minute,
            profile_requests_last_60s=profile_usage,
            profile_requests_in_current_window=profile_usage,
            account_configured_limit_per_minute=account_configured,
            account_effective_limit_per_minute=account_effective,
            account_requests_last_60s=account_usage,
            account_requests_in_current_window=account_usage,
            active_account_ceiling_per_minute=account_effective,
            account_backoff_active=bool(account_snapshot and account_snapshot.backoff_active),
            account_last_429_at=(
                account_snapshot.last_429_at.isoformat()
                if account_snapshot and account_snapshot.last_429_at
                else None
            ),
            account_retry_after_until=(
                account_snapshot.retry_after_until.isoformat()
                if account_snapshot and account_snapshot.retry_after_until
                else None
            ),
            ramp_recovery_eta_seconds=(
                account_snapshot.ramp_recovery_eta_seconds if account_snapshot else None
            ),
            limiter_window_type="sliding",
            usage_window_seconds=60,
            burst_limit_per_second=(
                self.settings.request_burst_limit
                if self.settings.rate_limiter_soft_cap_enabled
                else None
            ),
        )

    async def _update_error_state(self, status: str, reason: str | None) -> None:
        error_kind = self._error_kind(status, reason)
        if error_kind is None:
            self.consecutive_errors = 0
            self.last_error_kind = None
            self.last_safe_mode_delay_seconds = 0.0
            if hasattr(self.rate_limiter, "reset_backoff"):
                self.rate_limiter.reset_backoff()
            self._maybe_ramp_up_limit()
            return

        self.consecutive_errors += 1
        self.last_error_kind = error_kind
        if error_kind == "429":
            self.errors_429 += 1
            self._record_429()
        elif error_kind == "403":
            self.errors_403 += 1
        elif error_kind == "timeout":
            self.errors_timeout += 1

        if self._should_enter_safe_mode(error_kind):
            self.last_safe_mode_delay_seconds = self._safe_mode_delay_seconds(error_kind)
            self.safe_mode_until = time.monotonic() + self.last_safe_mode_delay_seconds

    async def _mark_error(self, exc: Exception) -> None:
        await self._update_error_state("failed", safe_starvell_error_reason(exc))

    def _safe_mode_active(self) -> bool:
        return time.monotonic() < self.safe_mode_until

    def _backoff_active(self) -> bool:
        limiter_backoff = bool(getattr(self.rate_limiter, "backoff_active", False))
        return (
            self._safe_mode_active()
            or limiter_backoff
            or self.effective_request_limit_per_minute
            < self.configured_request_limit_per_minute
        )

    def _safe_mode_remaining_seconds(self) -> float:
        return max(self.safe_mode_until - time.monotonic(), 0.0)

    def _safe_mode_status(self) -> str:
        if not self.last_error_kind:
            return "safe_mode"
        return f"safe_mode_{self.last_error_kind}"

    def _heartbeat_status(self, status: str) -> str:
        if self._safe_mode_active():
            return self._safe_mode_status()
        return status

    def _error_kind(self, status: str, reason: str | None) -> str | None:
        if status != "failed":
            return None
        normalized = (reason or "").lower()
        if normalized in {"rate_limited", "429"} or "429" in normalized:
            return "429"
        if normalized in {"forbidden", "403"} or "403" in normalized:
            return "403"
        if normalized == "timeout" or "timeout" in normalized or "таймаут" in normalized:
            return "timeout"
        if (
            normalized
            in {
                "proxy_error",
                "proxy_malformed_reply",
                "proxy_connect_error",
                "network_error",
            }
            or "proxy" in normalized
            or "socks" in normalized
            or "malformed reply" in normalized
            or "protocolerror" in normalized
            or "connecterror" in normalized
            or "server disconnected" in normalized
        ):
            return "proxy"
        return "failed"

    def _worker_state_name(self) -> str:
        if self.settings.worker_group == WORKER_GROUP_ALL:
            return "repricer"
        return f"repricer:{self.settings.worker_group}"

    def _most_active_position_amount(self) -> int | None:
        if not self.schedule_runtime:
            return None
        return max(
            self.schedule_runtime.values(),
            key=lambda state: state.change_score,
        ).position_amount

    def _build_rate_limiter(self) -> CompositeRateLimiter:
        if self.settings.rate_limiter_soft_cap_enabled:
            profile = RedisTokenBucketRateLimiter(
                self.redis,
                limit=self.effective_request_limit_per_minute,
                key_prefix=f"repricer:token-bucket:{self.settings.worker_group}",
            )
            if self.settings.token_limit_mode:
                global_limiter = RedisAdaptiveTokenBucketRateLimiter(
                    self.redis,
                    configured_limit_per_minute=self.settings.global_request_limit_per_minute,
                    initial_effective_limit_per_minute=self.settings.account_effective_limit_per_minute,
                    min_limit_per_minute=self.settings.account_min_limit_per_minute,
                    decrease_step_per_minute=self.settings.account_limit_decrease_step_per_minute,
                    ramp_step_per_minute=(
                        self.settings.ramp_step_per_minute
                        or self.settings.account_limit_ramp_step_per_minute
                    ),
                    ramp_idle_seconds=(
                        self.settings.ramp_idle_seconds
                        or self.settings.account_limit_ramp_idle_seconds
                    ),
                    key_prefix="repricer:account-token-limit",
                )
            else:
                global_limiter = RedisTokenBucketRateLimiter(
                    self.redis,
                    limit=self.settings.global_request_limit_per_minute,
                    key_prefix="repricer:token-bucket:global",
                )
            burst = RedisFixedWindowRateLimiter(
                self.redis,
                limit=self.settings.request_burst_limit,
                window_seconds=1,
                key_prefix=(
                    "repricer:burst:account"
                    if self.settings.token_limit_mode
                    else f"repricer:burst:{self.settings.worker_group}"
                ),
            )
        else:
            profile = NoopRateLimiter()
            global_limiter = NoopRateLimiter()
            burst = None
        min_delay_ms, jitter_ms = self.settings.request_delay_for_group(self.settings.worker_group)
        return CompositeRateLimiter(
            profile_limiter=profile,
            global_limiter=global_limiter,
            burst_limiter=burst,
            min_delay_ms=min_delay_ms,
            max_delay_ms=self.settings.request_max_delay_ms,
            jitter_ms=jitter_ms,
            backoff_factor=self.settings.request_backoff_factor,
        )

    def _should_enter_safe_mode(self, error_kind: str) -> bool:
        if not self.settings.safe_mode_enabled:
            return False
        if error_kind == "429" and self.settings.safe_mode_on_429:
            return True
        if error_kind == "403" and self.settings.safe_mode_on_403:
            return True
        if error_kind == "timeout":
            return True
        if error_kind == "proxy":
            return True
        return self.consecutive_errors >= self.settings.worker_safe_mode_error_threshold

    def _safe_mode_delay_seconds(self, error_kind: str) -> float:
        if error_kind == "proxy":
            return transport_backoff_seconds(self.consecutive_errors)
        return adaptive_backoff_seconds(self.consecutive_errors)

    def _profile_jitter_sleep_seconds(self) -> float:
        positions_count = max(len(self.settings.assigned_positions), 1)
        base = 60 * positions_count / max(self.effective_request_limit_per_minute, 1)
        if self.settings.worker_group == WORKER_GROUP_FAST_1:
            return random.uniform(max(base - 0.2, 0.1), base + 0.4)
        if self.settings.worker_group == WORKER_GROUP_FAST_2:
            return random.uniform(max(base - 0.2, 0.1), base + 0.4)
        if self.settings.worker_group == WORKER_GROUP_SLOW:
            return random.uniform(max(base - 0.4, 0.1), base + 1.1)
        return random.uniform(max(base * 0.9, 0.1), max(base * 1.2, 0.2))

    def _record_429(self) -> None:
        now = time.monotonic()
        self.last_429_at = datetime.now(UTC)
        self.last_limit_ramp_monotonic = now
        reduced_limit = max(
            min(
                MIN_EFFECTIVE_REQUEST_LIMIT_PER_MINUTE,
                self.configured_request_limit_per_minute,
            ),
            self.effective_request_limit_per_minute - RAMP_UP_STEP_PER_MINUTE,
        )
        self._set_effective_request_limit(reduced_limit)
        self.logger.warning(
            "repricer_effective_limit_reduced_after_429",
            worker_group=self.settings.worker_group,
            configured_request_limit_per_minute=self.configured_request_limit_per_minute,
            effective_request_limit_per_minute=self.effective_request_limit_per_minute,
        )

    def _maybe_ramp_up_limit(self, now: float | None = None) -> bool:
        if self.effective_request_limit_per_minute >= self.configured_request_limit_per_minute:
            return False

        current = time.monotonic() if now is None else now
        elapsed = current - self.last_limit_ramp_monotonic
        if elapsed < RAMP_UP_IDLE_SECONDS:
            return False

        steps = int(elapsed // RAMP_UP_IDLE_SECONDS)
        new_limit = min(
            self.configured_request_limit_per_minute,
            self.effective_request_limit_per_minute + steps * RAMP_UP_STEP_PER_MINUTE,
        )
        if new_limit == self.effective_request_limit_per_minute:
            return False

        self._set_effective_request_limit(new_limit)
        self.last_limit_ramp_monotonic += steps * RAMP_UP_IDLE_SECONDS
        self.logger.info(
            "repricer_effective_limit_ramped_up",
            worker_group=self.settings.worker_group,
            configured_request_limit_per_minute=self.configured_request_limit_per_minute,
            effective_request_limit_per_minute=self.effective_request_limit_per_minute,
        )
        return True

    def _set_effective_request_limit(self, limit: int) -> None:
        normalized = max(
            1,
            min(int(limit), self.configured_request_limit_per_minute),
        )
        self.effective_request_limit_per_minute = normalized
        if hasattr(self, "rate_limiter") and hasattr(self.rate_limiter, "set_profile_limit"):
            self.rate_limiter.set_profile_limit(normalized)
