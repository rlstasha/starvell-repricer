import asyncio

from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.network import mask_proxy_url, resolve_public_ip
from app.db.repositories import AppSettingsRepository, PositionRepository
from app.db.session import create_session_factory
from app.repricer.scheduler import RepricerScheduler
from app.repricer.worker_groups import ALL_WORKER_GROUPS, WORKER_GROUP_ALL

logger = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.app_mode not in {"all", "worker"}:
        raise RuntimeError("APP_MODE must be worker or all to start worker")
    session_factory = create_session_factory(settings=settings)

    if settings.app_mode == "all":
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
        if settings.proxy_profiles_enabled and settings.worker_group == WORKER_GROUP_ALL:
            await _run_proxy_profile_workers(settings, session_factory, redis)
            return
        await _run_single_worker(settings, session_factory, redis)
    finally:
        await redis.aclose()


async def _run_proxy_profile_workers(settings, session_factory, redis: Redis) -> None:
    logger.info(
        "repricer_proxy_profiles_enabled",
        proxy_mode=settings.proxy_mode,
        proxy_profiles=list(ALL_WORKER_GROUPS),
    )
    tasks = []
    for group in ALL_WORKER_GROUPS:
        group_settings = settings.model_copy(update={"worker_group": group})
        tasks.append(
            asyncio.create_task(
                _run_single_worker(group_settings, session_factory, redis),
                name=f"repricer-{group}",
            )
        )
    await asyncio.gather(*tasks)


async def _run_single_worker(settings, session_factory, redis: Redis) -> None:
    proxy_url = settings.proxy_url_for_group()
    public_ip = await resolve_public_ip(
        settings.public_ip if proxy_url is None else None,
        proxy_url=proxy_url,
    )
    logger.info(
        "repricer_worker_started",
        dry_run=settings.dry_run,
        worker_group=settings.worker_group,
        assigned_positions=list(settings.assigned_positions),
        request_limit_per_minute=settings.worker_request_limit_per_minute,
        public_ip=public_ip,
        proxy_profile=settings.worker_group,
        proxy=mask_proxy_url(proxy_url),
    )
    scheduler = RepricerScheduler(
        settings=settings,
        session_factory=session_factory,
        redis=redis,
        public_ip=public_ip,
    )
    await scheduler.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
