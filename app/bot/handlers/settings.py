from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import (
    format_general_settings,
    format_logs,
    format_proxy_profiles,
    format_proxy_status,
    format_status,
)
from app.bot.keyboards import back_to_main_keyboard, general_settings_keyboard, main_menu_keyboard
from app.bot.ui import answer_loading, cleanup_pending_prompt, safe_edit_text
from app.core.config import Settings
from app.db.models import UpdateStatus
from app.db.repositories import (
    AppSettingsRepository,
    PositionRepository,
    PositionScheduleStateRepository,
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
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    async with session_factory() as session:
        dry_run = await AppSettingsRepository(session).get_bool("dry_run", settings.dry_run)
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
    await safe_edit_text(
        callback,
        format_general_settings(
            dry_run=dry_run,
            request_limit=settings.request_limit_per_minute,
            high_percent=settings.high_priority_percent,
            normal_percent=settings.normal_priority_percent,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            proxy_mode=settings.proxy_mode,
            global_limit=settings.global_request_limit_per_minute,
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
        ),
        reply_markup=general_settings_keyboard(dry_run=dry_run),
    )


@router.callback_query(lambda query: query.data == "settings:toggle_dry_run")
async def toggle_dry_run(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    async with session_factory() as session:
        repo = AppSettingsRepository(session)
        current = await repo.get_bool("dry_run", settings.dry_run)
        await repo.set_bool("dry_run", not current)
        await session.commit()
        new_value = not current

    text = (
        "Изменение цен остановлено. Сейчас работает только анализ."
        if new_value
        else "Изменение цен включено. Реальная запись сработает только при настроенном endpoint."
    )
    await safe_edit_text(
        callback,
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
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
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
    request_usage = await limiter.current_usage()

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
        priority_counts = await positions.count_by_priority(enabled_only=True)
        all_positions = await positions.list_positions()
        positions_by_amount = {
            position.robux_amount: position
            for position in all_positions
        }
        last_position = (
            await positions.get_by_amount(worker_state.last_position_amount)
            if worker_state and worker_state.last_position_amount
            else None
        )

    if settings.proxy_mode == "enabled":
        text = format_proxy_status(
            worker_states=worker_states,
            heartbeats=heartbeats,
            dry_run=dry_run,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            latest_price_update=latest_price_update,
            latest_price_write_error=latest_price_write_error,
            request_usage=request_usage,
            global_limit=settings.global_request_limit_per_minute,
            group_infos=settings.worker_group_infos,
            success_count=success_count,
            error_count=error_count,
            recent_errors=recent_errors,
            last_positions_by_amount=positions_by_amount,
        )
    else:
        text = format_status(
            worker_state=worker_state,
            dry_run=dry_run,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            latest_price_update=latest_price_update,
            latest_price_write_error=latest_price_write_error,
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
        )

    await safe_edit_text(
        callback,
        text,
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(lambda query: query.data in {"proxies:limits", "servers:limits"})
async def show_proxy_profiles(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    async with session_factory() as session:
        app_settings = AppSettingsRepository(session)
        dry_run = await app_settings.get_bool("dry_run", settings.dry_run)
        heartbeats = await WorkerHeartbeatRepository(session).list_all()

    await safe_edit_text(
        callback,
        format_proxy_profiles(
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            dry_run=dry_run,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            global_limit=settings.global_request_limit_per_minute,
            proxy_mode=settings.proxy_mode,
        ),
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(lambda query: query.data == "logs:recent")
async def show_recent_logs(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    async with session_factory() as session:
        repo = PositionRepository(session)
        logs = await repo.list_latest_price_logs_by_position()
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()

    await safe_edit_text(
        callback,
        format_logs(
            logs,
            proxy_mode=settings.proxy_mode,
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            schedule_states=schedule_states,
        ),
        reply_markup=back_to_main_keyboard(),
    )
