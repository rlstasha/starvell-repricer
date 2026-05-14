from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.core.config import Settings
from app.core.logging import get_logger


logger = get_logger(__name__)


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
        owner_ids = self.settings.allowed_owner_telegram_ids
        if not owner_ids:
            return None
        if user is None or user.id not in owner_ids:
            if isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("Нет доступа")
            return None
        logger.info(
            "telegram_owner_action",
            owner_telegram_id=user.id,
            event_type=type(event).__name__,
            action=self._action_name(event),
        )
        return await handler(event, data)

    def _action_name(self, event: TelegramObject) -> str:
        if isinstance(event, CallbackQuery):
            return event.data or "callback"
        if isinstance(event, Message) and event.text and event.text.startswith("/"):
            return event.text.split(maxsplit=1)[0]
        return "message"
