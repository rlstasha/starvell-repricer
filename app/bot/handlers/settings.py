from aiogram import Router
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import (
    format_general_settings,
    format_logs,
    format_proxy_profiles,
    format_status,
)
from app.bot.keyboards import back_to_main_keyboard, general_settings_keyboard, main_menu_keyboard
from app.core.config import Settings
from app.db.models import UpdateStatus
from app.db.repositories import (
    AppSettingsRepository,
    PositionRepository,
    WorkerHeartbeatRepository,
    WorkerStateRepository,
)
from app.repricer.rate_limiter import RedisFixedWindowRateLimiter

router = Router()


@router.callback_query(lambda query: query.data == "settings:general")
async def general_settings(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        dry_run = await AppSettingsRepository(session).get_bool("dry_run", settings.dry_run)
    await callback.message.edit_text(
        format_general_settings(
            dry_run=dry_run,
            request_limit=settings.request_limit_per_minute,
            high_percent=settings.high_priority_percent,
            normal_percent=settings.normal_priority_percent,
        ),
        reply_markup=general_settings_keyboard(dry_run=dry_run),
    )
    await callback.answer()


@router.callback_query(lambda query: query.data == "settings:toggle_dry_run")
async def toggle_dry_run(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        repo = AppSettingsRepository(session)
        current = await repo.get_bool("dry_run", settings.dry_run)
        await repo.set_bool("dry_run", not current)
        await session.commit()
        new_value = not current

    text = "Dry-run включен." if new_value else "Dry-run выключен."
    await callback.message.edit_text(
        text,
        reply_markup=main_menu_keyboard(dry_run=new_value),
    )
    await callback.answer(text)


@router.callback_query(lambda query: query.data == "status:show")
async def show_status(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
) -> None:
    limiter = RedisFixedWindowRateLimiter(
        redis,
        limit=settings.request_limit_per_minute,
        window_seconds=60,
    )
    request_usage = await limiter.current_usage()

    async with session_factory() as session:
        app_settings = AppSettingsRepository(session)
        positions = PositionRepository(session)
        worker_state = await WorkerStateRepository(session).get()
        dry_run = await app_settings.get_bool("dry_run", settings.dry_run)
        success_count = await positions.count_price_logs(UpdateStatus.SUCCESS)
        error_count = await positions.count_price_logs(UpdateStatus.FAILED)
        recent_errors = await positions.list_recent_errors_with_positions(limit=5)
        priority_counts = await positions.count_by_priority(enabled_only=True)
        last_position = (
            await positions.get_by_amount(worker_state.last_position_amount)
            if worker_state and worker_state.last_position_amount
            else None
        )

    await callback.message.edit_text(
        format_status(
            worker_state=worker_state,
            dry_run=dry_run,
            request_usage=request_usage,
            request_limit=settings.request_limit_per_minute,
            high_percent=settings.high_priority_percent,
            normal_percent=settings.normal_priority_percent,
            high_count=priority_counts.get("high", 0),
            normal_count=priority_counts.get("normal", 0),
            success_count=success_count,
            error_count=error_count,
            recent_errors=recent_errors,
            last_position=last_position,
        ),
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(lambda query: query.data in {"proxies:limits", "servers:limits"})
async def show_proxy_profiles(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    async with session_factory() as session:
        app_settings = AppSettingsRepository(session)
        dry_run = await app_settings.get_bool("dry_run", settings.dry_run)
        heartbeats = await WorkerHeartbeatRepository(session).list_all()

    await callback.message.edit_text(
        format_proxy_profiles(
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            dry_run=dry_run,
            global_limit=settings.global_request_limit_per_minute,
            proxy_mode=settings.proxy_mode,
        ),
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()


@router.callback_query(lambda query: query.data == "logs:recent")
async def show_recent_logs(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        logs = await PositionRepository(session).list_latest_price_logs_by_position()

    await callback.message.edit_text(
        format_logs(logs),
        reply_markup=back_to_main_keyboard(),
    )
    await callback.answer()
