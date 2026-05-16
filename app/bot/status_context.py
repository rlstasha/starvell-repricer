from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.core.config import Settings
from app.db.models import Position, PriceUpdateLog, UpdateStatus, WorkerHeartbeat, WorkerState
from app.db.repositories import AppSettingsRepository, PositionRepository, WorkerHeartbeatRepository, WorkerStateRepository
from app.repricer.rate_limiter import RedisFixedWindowRateLimiter


@dataclass(frozen=True)
class TelegramStatusContext:
    dry_run: bool
    request_usage: int
    worker_state: WorkerState | None
    worker_states: list[WorkerState]
    heartbeats: list[WorkerHeartbeat]
    latest_price_update: tuple[PriceUpdateLog, Position | None] | None
    latest_price_write_error: tuple[PriceUpdateLog, Position | None] | None
    recent_errors: list[tuple[PriceUpdateLog, Position | None]]
    success_count: int
    error_count: int
    positions_by_amount: dict[int, Position]
    last_position: Position | None


async def load_telegram_status_context(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
) -> TelegramStatusContext:
    request_usage = await current_request_usage(settings=settings, redis=redis)
    async with session_factory() as session:
        app_settings = AppSettingsRepository(session)
        positions = PositionRepository(session)
        worker_state_repo = WorkerStateRepository(session)
        worker_state = await worker_state_repo.get()
        worker_states = await worker_state_repo.list_all()
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        dry_run = await app_settings.get_bool("dry_run", settings.dry_run)
        success_count = await positions.count_price_logs(UpdateStatus.SUCCESS)
        error_count = await positions.count_price_logs(UpdateStatus.FAILED)
        recent_errors = await positions.list_recent_errors_with_positions(limit=5)
        latest_price_update = await positions.get_latest_price_log_with_position(UpdateStatus.SUCCESS)
        latest_price_write_error = await positions.get_latest_price_log_with_position(UpdateStatus.FAILED)
        all_positions = await positions.list_positions()
        positions_by_amount = {position.robux_amount: position for position in all_positions}
        last_position = (
            await positions.get_by_amount(worker_state.last_position_amount)
            if worker_state and worker_state.last_position_amount
            else None
        )
    return TelegramStatusContext(
        dry_run=dry_run,
        request_usage=request_usage,
        worker_state=worker_state,
        worker_states=worker_states,
        heartbeats=heartbeats,
        latest_price_update=latest_price_update,
        latest_price_write_error=latest_price_write_error,
        recent_errors=recent_errors,
        success_count=success_count,
        error_count=error_count,
        positions_by_amount=positions_by_amount,
        last_position=last_position,
    )


async def current_request_usage(*, settings: Settings, redis: Redis) -> int:
    limiter = RedisFixedWindowRateLimiter(
        redis,
        limit=(
            settings.global_request_limit_per_minute
            if settings.proxy_mode == "enabled"
            else settings.request_limit_per_minute
        ),
        window_seconds=60,
        key_prefix=(
            "repricer:rate-limit:global"
            if settings.proxy_mode == "enabled"
            else "repricer:rate-limit"
        ),
    )
    return await limiter.current_usage()
