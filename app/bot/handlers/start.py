from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import format_main_menu
from app.bot.keyboards import main_menu_keyboard
from app.bot.status_context import load_telegram_status_context
from app.bot.ui import cleanup_pending_prompt, safe_edit_text
from app.core.config import Settings

router = Router()


@router.message(Command("start", "menu"))
async def start(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, message.bot, clear_state=True)
    status_context = await load_telegram_status_context(
        session_factory=session_factory,
        settings=settings,
        redis=redis,
    )
    await message.answer(
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


@router.message(Command("id"))
async def telegram_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.callback_query(lambda query: query.data == "menu:main")
async def main_menu(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    redis: Redis,
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
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
    await callback.answer()
