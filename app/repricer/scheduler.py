import asyncio
import heapq
import os
import random
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

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
    choose_dynamic_delay,
    display_interval_range,
    timing_for_group,
    update_change_score,
    update_error_score,
)
from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    RedisFixedWindowRateLimiter,
    RedisAdaptiveTokenBucketRateLimiter,
    RedisTokenBucketRateLimiter,
    adaptive_backoff_seconds,
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
                    await asyncio.sleep(self.settings.scheduler_idle_sleep_seconds)

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
                await asyncio.sleep(self.settings.scheduler_idle_sleep_seconds)
                return

            await self._sync_schedule(positions, session)
            positions_by_amount = {position.robux_amount: position for position in positions}
            item = self._next_due_position(positions_by_amount)
            if item is None:
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
            engine = RepricerEngine(
                session=session,
                settings=self.settings,
                starvell_client=starvell_client,
                dry_run=dry_run,
            )

            if not await self.position_lock.acquire(item.robux_amount):
                self.logger.info(
                    "repricer_position_lock_busy",
                    worker_group=self.settings.worker_group,
                    position_amount=item.robux_amount,
                )
                self._postpone_locked_position(item.robux_amount)
                await self._write_heartbeat(session, status="lock_busy", dry_run=dry_run)
                await session.commit()
                await asyncio.sleep(0.2)
                return

            try:
                result = await engine.process_position(item.robux_amount)
            finally:
                await self.position_lock.release(item.robux_amount)

            schedule_state = self._update_schedule_after_result(item, result)
            await PositionScheduleStateRepository(session).upsert(
                position=item,
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
            return

    async def _sync_schedule(self, positions: list[Position], session: AsyncSession) -> None:
        repository = PositionScheduleStateRepository(session)
        persisted_by_position_id = {
            item.position_id: item
            for item in await repository.list_all()
        }
        now = time.monotonic()
        active_amounts = {position.robux_amount for position in positions}

        for position in positions:
            timing = timing_for_group(self.settings.worker_group)
            existing = self.schedule_runtime.get(position.robux_amount)
            if existing is not None:
                existing.lot_id = position.lot_id
                continue

            persisted = persisted_by_position_id.get(position.id)
            current_interval = (
                persisted.current_interval_seconds
                if persisted is not None
                else timing.base_seconds
            )
            next_run = now
            if persisted is not None and persisted.last_checked_at is not None:
                elapsed = (datetime.now(UTC) - persisted.last_checked_at).total_seconds()
                next_run = now + max(current_interval - elapsed, 0.0)
            interval_min, interval_max = display_interval_range(self.settings.worker_group)
            self.schedule_runtime[position.robux_amount] = RuntimeScheduleState(
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

        for amount in set(self.schedule_runtime) - active_amounts:
            self.schedule_runtime.pop(amount, None)
        self._rebuild_schedule_heap()

    def _rebuild_schedule_heap(self) -> None:
        self.schedule_heap = [
            (state.next_run_monotonic, -state.change_score, amount)
            for amount, state in self.schedule_runtime.items()
        ]
        heapq.heapify(self.schedule_heap)

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

    def _next_schedule_wait_seconds(self) -> float:
        while self.schedule_heap:
            next_run, _, amount = self.schedule_heap[0]
            state = self.schedule_runtime.get(amount)
            if state is None or state.next_run_monotonic != next_run:
                heapq.heappop(self.schedule_heap)
                continue
            return max(min(next_run - time.monotonic(), self.settings.scheduler_idle_sleep_seconds), 0.1)
        return self.settings.scheduler_idle_sleep_seconds

    def _postpone_locked_position(self, amount: int) -> None:
        state = self.schedule_runtime.get(amount)
        if state is None:
            return
        state.next_run_monotonic = time.monotonic() + 0.5
        heapq.heappush(self.schedule_heap, (state.next_run_monotonic, -state.change_score, amount))

    def _update_schedule_after_result(self, position: Position, result) -> RuntimeScheduleState:
        state = self.schedule_runtime[position.robux_amount]
        error_kind = self._error_kind(result.status, result.reason)
        change_score = update_change_score(
            state.change_score,
            state.last_competitor_price,
            result.competitor_price,
        )
        error_score = update_error_score(state.error_score, failed=result.status == "failed")
        backoff_active = self._backoff_active() or error_kind == "429"
        decision = choose_dynamic_delay(
            worker_group=self.settings.worker_group,
            change_score=change_score,
            error_score=error_score,
            backoff_active=backoff_active,
            previous_delay_seconds=state.current_interval_seconds,
        )
        timing = timing_for_group(self.settings.worker_group)
        if backoff_active:
            interval_min, interval_max = display_interval_range(
                self.settings.worker_group,
                backoff_active=True,
            )
        else:
            interval_min, interval_max = timing.min_seconds, timing.max_seconds
        checked_at = datetime.now(UTC)
        state.current_interval_seconds = decision.delay_seconds
        state.interval_min_seconds = interval_min
        state.interval_max_seconds = interval_max
        state.delay_reason = decision.reason
        state.next_run_monotonic = time.monotonic() + decision.delay_seconds
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
        )
        return state

    def _filter_assigned_positions(self, positions):
        if self.settings.worker_group == WORKER_GROUP_ALL:
            return positions
        assigned = set(self.settings.assigned_positions)
        return [position for position in positions if position.robux_amount in assigned]

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
            self.last_safe_mode_delay_seconds = adaptive_backoff_seconds(
                self.consecutive_errors
            )
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
                ramp_step_per_minute=self.settings.account_limit_ramp_step_per_minute,
                ramp_idle_seconds=self.settings.account_limit_ramp_idle_seconds,
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
            key_prefix=f"repricer:burst:{self.settings.worker_group}",
        )
        return CompositeRateLimiter(
            profile_limiter=profile,
            global_limiter=global_limiter,
            burst_limiter=burst,
            min_delay_ms=self.settings.request_min_delay_ms,
            max_delay_ms=self.settings.request_max_delay_ms,
            jitter_ms=self.settings.request_jitter_ms,
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
        return self.consecutive_errors >= self.settings.worker_safe_mode_error_threshold

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
