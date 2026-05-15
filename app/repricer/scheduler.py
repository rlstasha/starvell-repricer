import asyncio
import os
import socket
import time

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
from app.market.client import StarvellClient
from app.repricer.engine import RepricerEngine
from app.repricer.locks import RedisPositionLock
from app.repricer.priority_queue import PercentagePositionQueue
from app.repricer.rate_limiter import RedisFixedWindowRateLimiter
from app.repricer.worker_groups import WORKER_GROUP_ALL


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
        self.logger = get_logger(__name__)

    async def run_forever(self) -> None:
        rate_limiter = RedisFixedWindowRateLimiter(
            self.redis,
            limit=self.settings.worker_request_limit_per_minute,
            window_seconds=60,
            key_prefix=f"repricer:rate-limit:{self.settings.worker_group}",
        )
        async with StarvellClient(self.settings, rate_limiter) as starvell_client:
            while True:
                try:
                    await self.run_once(starvell_client)
                except Exception as exc:
                    await self._mark_error(exc)
                    self.logger.exception(
                        "repricer_scheduler_cycle_failed",
                        worker_group=self.settings.worker_group,
                        error=str(exc),
                    )
                    async with self.session_factory() as session:
                        await WorkerStateRepository(session).mark_cycle(
                            name=self._worker_state_name(),
                            position_amount=None,
                            status="failed",
                            error=str(exc),
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
            async with self.session_factory() as session:
                await self._write_heartbeat(
                    session,
                    status="safe_mode",
                    dry_run=self.settings.dry_run,
                )
                await session.commit()
            await asyncio.sleep(self.settings.worker_safe_mode_seconds)
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
                    position_amount=result.position_amount,
                    status=result.status,
                    reason=result.reason,
                    old_price=str(result.old_price),
                    new_price=str(result.new_price),
                    competitor_price=str(result.competitor_price),
                )
                return

            await self._mark_idle(
                session,
                "Все позиции этой группы сейчас заблокированы",
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
        await WorkerHeartbeatRepository(session).upsert(
            worker_group=self.settings.worker_group,
            hostname=self.hostname,
            public_ip=self.public_ip,
            assigned_positions=list(self.settings.assigned_positions),
            request_limit_per_minute=self.settings.worker_request_limit_per_minute,
            status=status,
            errors_429=self.errors_429,
            errors_403=self.errors_403,
            errors_timeout=self.errors_timeout,
            consecutive_errors=self.consecutive_errors,
            safe_mode=self._safe_mode_active(),
            dry_run=dry_run,
        )

    async def _update_error_state(self, status: str, reason: str) -> None:
        error_kind = self._error_kind(status, reason)
        if error_kind is None:
            self.consecutive_errors = 0
            return

        self.consecutive_errors += 1
        if error_kind == "429":
            self.errors_429 += 1
        elif error_kind == "403":
            self.errors_403 += 1
        elif error_kind == "timeout":
            self.errors_timeout += 1

        if self.consecutive_errors >= self.settings.worker_safe_mode_error_threshold:
            self.safe_mode_until = time.monotonic() + self.settings.worker_safe_mode_seconds

        if error_kind in {"429", "403", "timeout"}:
            await asyncio.sleep(self.settings.worker_error_backoff_seconds)

    async def _mark_error(self, exc: Exception) -> None:
        await self._update_error_state("failed", str(exc))

    def _safe_mode_active(self) -> bool:
        return time.monotonic() < self.safe_mode_until

    def _heartbeat_status(self, status: str) -> str:
        if self._safe_mode_active():
            return "safe_mode"
        return status

    def _error_kind(self, status: str, reason: str) -> str | None:
        if status != "failed":
            return None
        normalized = reason.lower()
        if "429" in normalized:
            return "429"
        if "403" in normalized:
            return "403"
        if "timeout" in normalized or "таймаут" in normalized:
            return "timeout"
        return "failed"

    def _worker_state_name(self) -> str:
        if self.settings.worker_group == WORKER_GROUP_ALL:
            return "repricer"
        return f"repricer:{self.settings.worker_group}"
