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
        global_request_limit_per_minute=settings.global_request_limit_per_minute,
        account_effective_limit_per_minute=settings.account_effective_limit_per_minute,
        public_ip=public_ip,
        proxy_profile=settings.worker_group,
        proxy=mask_proxy_url(proxy_url),
    )
    min_delay_ms, jitter_ms = settings.request_delay_for_group(settings.worker_group)
    logger.info(
        "repricer_worker_active_tuning",
        worker_group=settings.worker_group,
        request_min_delay_ms=settings.request_min_delay_ms,
        request_jitter_ms=settings.request_jitter_ms,
        group_min_delay_ms=min_delay_ms,
        group_jitter_ms=jitter_ms,
        global_request_limit_per_minute=settings.global_request_limit_per_minute,
        account_effective_limit_per_minute=settings.account_effective_limit_per_minute,
        worker_fast_1_request_limit_per_minute=settings.worker_fast_1_request_limit_per_minute,
        worker_fast_2_request_limit_per_minute=settings.worker_fast_2_request_limit_per_minute,
        worker_slow_request_limit_per_minute=settings.worker_slow_request_limit_per_minute,
        proxy_fast_1_request_limit_per_minute=settings.proxy_request_limits["fast_1"],
        proxy_fast_2_request_limit_per_minute=settings.proxy_request_limits["fast_2"],
        proxy_slow_request_limit_per_minute=settings.proxy_request_limits["slow"],
        rate_limiter_soft_cap_enabled=settings.rate_limiter_soft_cap_enabled,
        fast1_min_delay_ms=settings.fast1_min_delay_ms,
        fast1_jitter_ms=settings.fast1_jitter_ms,
        my_lot_state_cache_ttl_seconds=settings.my_lot_state_cache_ttl_seconds,
        price_update_context_cache_ttl_seconds=settings.price_update_context_cache_ttl_seconds,
        account_limit_ramp_step_per_minute=(
            settings.ramp_step_per_minute or settings.account_limit_ramp_step_per_minute
        ),
        account_limit_ramp_idle_seconds=(
            settings.ramp_idle_seconds or settings.account_limit_ramp_idle_seconds
        ),
        market_http2_enabled=settings.market_http2_enabled,
        scheduler_max_concurrent_positions=settings.scheduler_max_concurrent_positions,
        ultra_fast_min_interval_seconds=settings.ultra_fast_min_interval_seconds,
        fast1_min_interval_seconds=settings.fast1_min_interval_seconds,
        fast2_min_interval_seconds=settings.fast2_min_interval_seconds,
        hot_mode_enabled=settings.hot_mode_enabled,
        hot_mode_min_interval_seconds=settings.hot_mode_min_interval_seconds,
        hot_mode_max_interval_seconds=settings.hot_mode_max_interval_seconds,
        hot_mode_cooldown_interval_seconds=settings.hot_mode_cooldown_interval_seconds,
        fast_mode_enabled=settings.fast_mode_enabled,
        fast_mode_min_interval_seconds=settings.fast_mode_min_interval_seconds,
        fast_mode_max_interval_seconds=settings.fast_mode_max_interval_seconds,
        post_update_interval_multiplier=settings.post_update_interval_multiplier,
        post_update_interval_positions=list(settings.post_update_interval_position_amounts),
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
