from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message


MAX_MESSAGE_LENGTH = 3900


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
    await callback.answer("⏳ загружаю...")


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
        await callback.message.answer(
            "⚠️ не удалось загрузить. Попробуйте открыть раздел еще раз.",
            reply_markup=reply_markup,
        )


def _fit_message(text: str) -> str:
    if len(text) <= MAX_MESSAGE_LENGTH:
        return text
    suffix = "\n\n… сообщение сокращено, потому что Telegram ограничивает длину."
    return text[: MAX_MESSAGE_LENGTH - len(suffix)].rstrip() + suffix
