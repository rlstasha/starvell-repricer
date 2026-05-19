from aiogram import Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import (
    format_errors_screen,
    format_log_page,
    format_limits_screen,
    format_main_menu,
    format_misc_menu,
    format_price_change_toggle_result,
    format_price_write_screen,
    format_price_write_test_hint,
    format_proxy_screen,
    format_scheduler_screen,
    format_status_overview,
    format_technical_status,
)
from app.bot.keyboards import (
    back_to_main_keyboard,
    back_to_misc_keyboard,
    logs_pagination_keyboard,
    main_menu_keyboard,
    misc_menu_keyboard,
    price_status_keyboard,
    proxy_pagination_keyboard,
    status_sections_keyboard,
)
from app.bot.status_context import load_telegram_status_context
from app.bot.ui import answer_loading, cleanup_pending_prompt, safe_callback_answer, safe_edit_text
from app.core.config import Settings
from app.db.repositories import (
    AppSettingsRepository,
    PositionRepository,
    PositionScheduleStateRepository,
    WorkerHeartbeatRepository,
)

router = Router()


@router.callback_query(lambda query: query.data == "settings:toggle_dry_run")
async def toggle_dry_run(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    async with session_factory() as session:
        repo = AppSettingsRepository(session)
        current = await repo.get_bool("dry_run", settings.dry_run)
        await repo.set_bool("dry_run", not current)
        await session.commit()
        new_value = not current

    text = format_price_change_toggle_result(
        dry_run=new_value,
        real_price_writes_enabled=settings.enable_real_price_writes,
        endpoint_configured=bool(settings.market_update_lot_price_url),
    )
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await safe_edit_text(
        callback,
        format_main_menu(
            dry_run=status_context.dry_run,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            proxy_mode=settings.proxy_mode,
            worker_state=status_context.worker_state,
            heartbeats=status_context.heartbeats,
            request_usage=status_context.request_usage,
            global_limit=settings.global_request_limit_per_minute,
            group_infos=settings.worker_group_infos,
        ),
        reply_markup=main_menu_keyboard(dry_run=status_context.dry_run),
    )
    await safe_callback_answer(callback, text, show_alert=True, force=True)


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
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    text = format_status_overview(
        worker_state=status_context.worker_state,
        heartbeats=status_context.heartbeats,
        dry_run=status_context.dry_run,
        real_price_writes_enabled=settings.enable_real_price_writes,
        price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
        latest_price_update=status_context.latest_price_update,
        request_usage=status_context.request_usage,
        global_limit=(
            settings.global_request_limit_per_minute
            if settings.proxy_mode == "enabled"
            else settings.request_limit_per_minute
        ),
        group_infos=settings.worker_group_infos if settings.proxy_mode == "enabled" else [],
    )

    await safe_edit_text(
        callback,
        text,
        reply_markup=status_sections_keyboard(),
    )


@router.callback_query(lambda query: query.data == "misc:show")
async def show_misc_menu(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    await safe_edit_text(
        callback,
        format_misc_menu(),
        reply_markup=misc_menu_keyboard(),
    )


@router.callback_query(
    lambda query: query.data in {"proxies:show", "proxies:limits", "servers:limits"}
    or (query.data or "").startswith("proxies:page:")
    or (query.data or "").startswith("proxies:refresh:")
)
async def show_proxy_profiles(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    page = _page_from_callback(callback.data)
    async with session_factory() as session:
        heartbeats = await WorkerHeartbeatRepository(session).list_all()

    await safe_edit_text(
        callback,
        format_proxy_screen(
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            page=page,
        ),
        reply_markup=proxy_pagination_keyboard(
            page=min(page, max(len(settings.worker_group_infos) - 1, 0)),
            total=len(settings.worker_group_infos),
        ),
    )


@router.callback_query(lambda query: query.data == "price:status")
async def show_price_write_status(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await safe_edit_text(
        callback,
        format_price_write_screen(
            dry_run=status_context.dry_run,
            real_price_writes_enabled=settings.enable_real_price_writes,
            price_write_endpoint_configured=bool(settings.market_update_lot_price_url),
            latest_price_update=status_context.latest_price_update,
            latest_price_write_error=status_context.latest_price_write_error,
        ),
        reply_markup=price_status_keyboard(dry_run=status_context.dry_run),
    )


@router.callback_query(lambda query: query.data == "price:test_write")
async def show_price_write_test_hint(
    callback: CallbackQuery,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    await safe_edit_text(
        callback,
        format_price_write_test_hint(),
        reply_markup=back_to_main_keyboard(),
    )


@router.callback_query(lambda query: query.data == "limits:show")
async def show_limits(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await safe_edit_text(
        callback,
        format_limits_screen(
            group_infos=settings.worker_group_infos,
            heartbeats=status_context.heartbeats,
            request_usage=status_context.request_usage,
            global_limit=settings.global_request_limit_per_minute,
        ),
        reply_markup=back_to_misc_keyboard(),
    )


@router.callback_query(lambda query: query.data == "scheduler:show")
async def show_scheduler(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    async with session_factory() as session:
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
    await safe_edit_text(
        callback,
        format_scheduler_screen(
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
        ),
        reply_markup=back_to_misc_keyboard(),
    )


@router.callback_query(lambda query: query.data == "errors:show")
async def show_errors(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await safe_edit_text(
        callback,
        format_errors_screen(
            latest_price_update=status_context.latest_price_update,
            latest_price_write_error=status_context.latest_price_write_error,
            recent_errors=status_context.recent_errors,
            heartbeats=status_context.heartbeats,
            group_infos=settings.worker_group_infos,
        ),
        reply_markup=back_to_misc_keyboard(),
    )


@router.callback_query(lambda query: query.data == "technical:status")
async def show_technical_status(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await safe_edit_text(
        callback,
        format_technical_status(
            worker_states=status_context.worker_states,
            heartbeats=status_context.heartbeats,
            request_usage=status_context.request_usage,
            global_limit=settings.global_request_limit_per_minute,
            recent_errors=status_context.recent_errors,
            group_infos=settings.worker_group_infos,
        ),
        reply_markup=back_to_misc_keyboard(),
    )


@router.callback_query(
    lambda query: query.data == "logs:recent"
    or (query.data or "").startswith("logs:page:")
    or (query.data or "").startswith("logs:refresh:")
)
async def show_recent_logs(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    page = _page_from_callback(callback.data)
    async with session_factory() as session:
        repo = PositionRepository(session)
        logs = await repo.list_latest_price_logs_by_position()
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()

    await safe_edit_text(
        callback,
        format_log_page(
            logs,
            page=page,
            proxy_mode=settings.proxy_mode,
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            schedule_states=schedule_states,
        ),
        reply_markup=logs_pagination_keyboard(
            page=min(page, max(len(logs) - 1, 0)),
            total=len(logs),
        ),
    )


def _page_from_callback(callback_data: str | None) -> int:
    if not callback_data:
        return 0
    parts = callback_data.split(":")
    if len(parts) < 3:
        return 0
    try:
        return max(0, int(parts[2]))
    except ValueError:
        return 0
