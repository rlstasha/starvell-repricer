import asyncio
import os
import random
import socket
import time
from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import (
    AppSettingsRepository,
    PositionRepository,
    WorkerHeartbeatRepository,
    WorkerStateRepository,
)
from app.market.client import StarvellClient, safe_starvell_error_reason
from app.repricer.engine import RepricerEngine
from app.repricer.locks import RedisPositionLock
from app.repricer.priority_queue import PercentagePositionQueue
from app.core.network import mask_proxy_url
from app.repricer.rate_limiter import (
    CompositeRateLimiter,
    RedisFixedWindowRateLimiter,
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
        self.queue = PercentagePositionQueue(
            high_percent=settings.high_priority_percent,
            normal_percent=settings.normal_priority_percent,
        )
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
            self.queue.update(positions)

            if not positions:
                await self._mark_idle(
                    session,
                    "Нет включенных позиций этой группы",
                )
                await asyncio.sleep(self.settings.scheduler_idle_sleep_seconds)
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

            for _ in range(len(positions)):
                item = self.queue.next()
                if item is None:
                    break
                if not await self.position_lock.acquire(item.robux_amount):
                    self.logger.info(
                        "repricer_position_lock_busy",
                        worker_group=self.settings.worker_group,
                        position_amount=item.robux_amount,
                    )
                    continue

                try:
                    result = await engine.process_position(item.robux_amount)
                finally:
                    await self.position_lock.release(item.robux_amount)

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
                if not self._safe_mode_active():
                    await asyncio.sleep(self._profile_jitter_sleep_seconds())
                return

            await self._mark_idle(
                session,
                "Позиции группы заблокированы",
            )
            await asyncio.sleep(self.settings.scheduler_idle_sleep_seconds)

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
        await WorkerHeartbeatRepository(session).upsert(
            worker_group=self.settings.worker_group,
            hostname=self.hostname,
            public_ip=self.public_ip,
            assigned_positions=list(self.settings.assigned_positions),
            request_limit_per_minute=self.settings.worker_request_limit_per_minute,
            effective_request_limit_per_minute=self.effective_request_limit_per_minute,
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

    def _build_rate_limiter(self) -> CompositeRateLimiter:
        profile = RedisFixedWindowRateLimiter(
            self.redis,
            limit=self.effective_request_limit_per_minute,
            window_seconds=60,
            key_prefix=f"repricer:rate-limit:{self.settings.worker_group}",
        )
        global_limiter = RedisFixedWindowRateLimiter(
            self.redis,
            limit=self.settings.global_request_limit_per_minute,
            window_seconds=60,
            key_prefix="repricer:rate-limit:global",
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
