import asyncio

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.repositories import AppSettingsRepository, PositionRepository
from app.db.session import create_session_factory
from app.repricer.scheduler import RepricerScheduler

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    session_factory = create_session_factory(settings=settings)

    async with session_factory() as session:
        repository = PositionRepository(session)
        await repository.seed_default_positions(
            min_price=settings.default_min_price,
            max_price=settings.default_max_price,
            step=settings.default_price_step,
            min_rating=settings.default_min_rating,
            ignore_no_rating=settings.default_ignore_no_rating,
            fallback_behavior=settings.default_fallback_behavior,
        )
        await AppSettingsRepository(session).ensure_defaults(dry_run=settings.dry_run)
        await session.commit()

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        logger.info("repricer_worker_started", dry_run=settings.dry_run)
        scheduler = RepricerScheduler(
            settings=settings,
            session_factory=session_factory,
            redis=redis,
        )
        await scheduler.run_forever()
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
