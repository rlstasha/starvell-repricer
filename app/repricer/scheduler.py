import asyncio

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.core.logging import get_logger
from app.db.repositories import AppSettingsRepository, PositionRepository, WorkerStateRepository
from app.market.client import StarvellClient
from app.repricer.engine import RepricerEngine
from app.repricer.priority_queue import WeightedPositionQueue
from app.repricer.rate_limiter import RedisFixedWindowRateLimiter


class RepricerScheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis
        self.queue = WeightedPositionQueue(
            high_weight=settings.high_priority_weight,
            normal_weight=settings.normal_priority_weight,
        )
        self.logger = get_logger(__name__)

    async def run_forever(self) -> None:
        rate_limiter = RedisFixedWindowRateLimiter(
            self.redis,
            limit=self.settings.request_limit_per_minute,
            window_seconds=60,
        )
        async with StarvellClient(self.settings, rate_limiter) as starvell_client:
            while True:
                try:
                    await self.run_once(starvell_client)
                except Exception as exc:
                    self.logger.exception("repricer_scheduler_cycle_failed", error=str(exc))
                    async with self.session_factory() as session:
                        await WorkerStateRepository(session).mark_cycle(
                            position_amount=None,
                            status="failed",
                            error=str(exc),
                        )
                        await session.commit()
                    await asyncio.sleep(self.settings.scheduler_idle_sleep_seconds)

    async def run_once(self, starvell_client: StarvellClient) -> None:
        async with self.session_factory() as session:
            repository = PositionRepository(session)
            positions = await repository.list_enabled_positions()
            self.queue.update(positions)
            item = self.queue.next()

            if item is None:
                self.logger.warning("repricer_no_enabled_positions")
                await WorkerStateRepository(session).mark_cycle(
                    position_amount=None,
                    status="idle",
                    error="Нет включенных позиций",
                )
                await session.commit()
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
            result = await engine.process_position(item.robux_amount)
            await WorkerStateRepository(session).mark_cycle(
                position_amount=result.position_amount,
                status=result.status,
                error=result.reason if result.status == "failed" else None,
            )
            await session.commit()
            self.logger.info(
                "repricer_position_processed",
                position_amount=result.position_amount,
                status=result.status,
                reason=result.reason,
                old_price=str(result.old_price),
                new_price=str(result.new_price),
                competitor_price=str(result.competitor_price),
            )
