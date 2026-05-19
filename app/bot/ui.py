from collections import OrderedDict

from aiohttp import ClientConnectorError
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.core.logging import get_logger


MAX_MESSAGE_LENGTH = 3900
_ANSWERED_CALLBACK_IDS: OrderedDict[str, None] = OrderedDict()
_MAX_TRACKED_CALLBACK_IDS = 1000
logger = get_logger(__name__)


async def cleanup_pending_prompt(
    state: FSMContext,
    bot,
    *,
    clear_state: bool = False,
) -> None:
    data = await state.get_data()
    chat_id = data.get("last_prompt_chat_id")
    message_id = data.get("last_prompt_message_id")
    if not chat_id or not message_id:
        if clear_state:
            await state.clear()
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass
    if clear_state:
        await state.clear()
    else:
        await state.update_data(
            last_prompt_chat_id=None,
            last_prompt_message_id=None,
        )


async def send_tracked_prompt(
    message: Message,
    state: FSMContext,
    text: str,
) -> None:
    prompt = await message.answer(text)
    await state.update_data(
        last_prompt_chat_id=prompt.chat.id,
        last_prompt_message_id=prompt.message_id,
    )


async def answer_loading(callback: CallbackQuery) -> None:
    await safe_callback_answer(callback, "⏳ загружаю...")


async def safe_callback_answer(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
    force: bool = False,
) -> None:
    if not force and callback.id in _ANSWERED_CALLBACK_IDS:
        return
    try:
        await callback.answer(text, show_alert=show_alert)
        _remember_callback(callback.id)
    except TelegramBadRequest as exc:
        logger.warning(
            "telegram_callback_answer_failed",
            callback_data=callback.data,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    except (TelegramNetworkError, ClientConnectorError, TimeoutError, OSError) as exc:
        logger.warning(
            "telegram_callback_answer_network_failed",
            callback_data=callback.data,
            error_type=type(exc).__name__,
        )


async def safe_send_error(callback: CallbackQuery, text: str = "⚠️ не удалось загрузить") -> None:
    await safe_callback_answer(callback, text, show_alert=True, force=True)
    if callback.message is None:
        return
    try:
        await callback.message.answer(text)
    except (TelegramBadRequest, TelegramNetworkError, ClientConnectorError, TimeoutError, OSError) as exc:
        logger.warning(
            "telegram_callback_error_message_failed",
            callback_data=callback.data,
            error_type=type(exc).__name__,
        )


async def safe_edit_text(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    text = _fit_message(text)
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        try:
            await callback.message.answer(
                "⚠️ не удалось загрузить. Попробуйте открыть раздел еще раз.",
                reply_markup=reply_markup,
            )
        except (TelegramBadRequest, TelegramNetworkError, ClientConnectorError, TimeoutError, OSError) as send_exc:
            logger.warning(
                "telegram_safe_edit_fallback_failed",
                callback_data=callback.data,
                error_type=type(send_exc).__name__,
            )
    except (TelegramNetworkError, ClientConnectorError, TimeoutError, OSError) as exc:
        logger.warning(
            "telegram_safe_edit_network_failed",
            callback_data=callback.data,
            error_type=type(exc).__name__,
        )


def _fit_message(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    suffix = "\n\n… сообщение сокращено, потому что Telegram ограничивает длину."
    return text[: MAX_MESSAGE_LENGTH - len(suffix)].rstrip() + suffix


def _remember_callback(callback_id: str) -> None:
    _ANSWERED_CALLBACK_IDS[callback_id] = None
    _ANSWERED_CALLBACK_IDS.move_to_end(callback_id)
    while len(_ANSWERED_CALLBACK_IDS) > _MAX_TRACKED_CALLBACK_IDS:
        _ANSWERED_CALLBACK_IDS.popitem(last=False)
