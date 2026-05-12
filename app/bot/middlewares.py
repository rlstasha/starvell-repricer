from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.config import Settings


class OwnerOnlyMiddleware(BaseMiddleware):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if self.settings.owner_telegram_id is None:
            return None
        if user is None or user.id != self.settings.owner_telegram_id:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Нет доступа")
            return None
        return await handler(event, data)
