from aiogram import Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import format_main_menu
from app.bot.keyboards import main_menu_keyboard
from app.core.config import Settings
from app.db.repositories import AppSettingsRepository

router = Router()


@router.message(Command("start", "menu"))
async def start(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    dry_run = await _get_dry_run(session_factory, settings)
    await message.answer(format_main_menu(dry_run=dry_run), reply_markup=main_menu_keyboard(dry_run=dry_run))


@router.message(Command("id"))
async def telegram_id(message: Message) -> None:
    await message.answer(f"Ваш Telegram ID: {message.from_user.id}")


@router.callback_query(lambda query: query.data == "menu:main")
async def main_menu(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    dry_run = await _get_dry_run(session_factory, settings)
    await callback.message.edit_text(
        format_main_menu(dry_run=dry_run),
        reply_markup=main_menu_keyboard(dry_run=dry_run),
    )
    await callback.answer()


async def _get_dry_run(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> bool:
    async with session_factory() as session:
        return await AppSettingsRepository(session).get_bool("dry_run", settings.dry_run)

