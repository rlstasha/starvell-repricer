from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import format_competitors, format_position_card, format_price_test
from app.bot.keyboards import position_card_keyboard, positions_keyboard
from app.db.models import Position
from app.db.repositories import PositionRepository
from app.market.schemas import MarketOffer
from app.repricer.price_strategy import PriceCalculationSettings, UndercutByStepStrategy

router = Router()

EDIT_LABELS = {
    "min_price": "минимальную цену",
    "max_price": "максимальную цену",
    "step": "шаг",
    "min_rating": "минимальный рейтинг",
}


class EditPositionState(StatesGroup):
    waiting_for_value = State()


@router.callback_query(lambda query: query.data == "positions:list")
async def list_positions(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        positions = await PositionRepository(session).list_positions()
    await callback.message.edit_text("📦 Позиции", reply_markup=positions_keyboard(positions))
    await callback.answer()


@router.callback_query(F.data.startswith("position:"))
async def position_actions(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
) -> None:
    parts = callback.data.split(":")

    if len(parts) == 2:
        await _show_card(callback, session_factory, int(parts[1]))
        return

    action = parts[1]
    amount = int(parts[-1])

    if action == "toggle":
        async with session_factory() as session:
            repo = PositionRepository(session)
            position = await repo.get_by_amount(amount)
            if position:
                position.enabled = not position.enabled
                await session.commit()
        await _show_card(callback, session_factory, amount)
        return

    if action == "toggle_ignore":
        async with session_factory() as session:
            repo = PositionRepository(session)
            position = await repo.get_by_amount(amount)
            if position:
                position.settings.ignore_no_rating = not position.settings.ignore_no_rating
                await session.commit()
        await _show_card(callback, session_factory, amount)
        return

    if action == "competitors":
        await _show_competitors(callback, session_factory, amount)
        return

    if action == "test_calc":
        await _show_price_test(callback, session_factory, amount)
        return

    if action == "edit":
        field_name = parts[2]
        if field_name not in EDIT_LABELS:
            await callback.answer("Неизвестная настройка.", show_alert=True)
            return
        await state.set_state(EditPositionState.waiting_for_value)
        await state.update_data(amount=amount, field_name=field_name)
        await callback.message.answer(f"Введите новую {EDIT_LABELS[field_name]}.")
        await callback.answer()
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(EditPositionState.waiting_for_value)
async def save_position_value(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
) -> None:
    data = await state.get_data()
    amount = int(data["amount"])
    field_name = data["field_name"]

    try:
        value = Decimal(message.text.strip().replace(",", "."))
    except (InvalidOperation, AttributeError):
        await message.answer("Нужно число. Например: 499 или 4.5")
        return

    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        if position is None:
            await state.clear()
            await message.answer("Позиция не найдена.")
            return

        error = _validate_numeric_setting(position, field_name, value)
        if error:
            await message.answer(error)
            return

        await repo.update_setting(amount, field_name, value)
        await session.commit()
        position = await repo.get_by_amount(amount)

    await state.clear()
    await message.answer(
        "Настройка сохранена.\n\n" + format_position_card(position),
        reply_markup=position_card_keyboard(position),
    )


async def _show_card(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    amount: int,
) -> None:
    async with session_factory() as session:
        position = await PositionRepository(session).get_by_amount(amount)
    if position is None:
        await callback.answer("Позиция не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        format_position_card(position),
        reply_markup=position_card_keyboard(position),
    )
    await callback.answer()


async def _show_competitors(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    amount: int,
) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        competitors = await repo.list_recent_competitors(position) if position else []
    if position is None:
        await callback.answer("Позиция не найдена.", show_alert=True)
        return
    await callback.message.edit_text(
        format_competitors(amount, competitors),
        reply_markup=position_card_keyboard(position),
    )
    await callback.answer()


async def _show_price_test(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    amount: int,
) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        competitors = await repo.list_recent_active_competitors(position) if position else []

    if position is None:
        await callback.answer("Позиция не найдена.", show_alert=True)
        return

    offers = [
        MarketOffer(
            position_amount=position.robux_amount,
            price=item.price,
            seller_id=item.seller_id,
            seller_username=item.seller_username,
            rating=item.rating,
            is_active=item.is_active,
            raw_payload=item.raw_payload,
        )
        for item in competitors
    ]
    decision = UndercutByStepStrategy().calculate(
        competitors=sorted(offers, key=lambda item: item.price),
        current_own_price=position.state.current_own_price if position.state else None,
        settings=PriceCalculationSettings(
            min_price=position.settings.min_price,
            max_price=position.settings.max_price,
            step=position.settings.step,
            fallback_behavior=position.settings.fallback_behavior,
        ),
    )
    await callback.message.edit_text(
        format_price_test(
            position=position,
            target_price=decision.target_price,
            competitor_price=decision.competitor_price,
            reason=decision.reason,
            should_update=decision.should_update,
        ),
        reply_markup=position_card_keyboard(position),
    )
    await callback.answer()


def _validate_numeric_setting(position: Position, field_name: str, value: Decimal) -> str | None:
    if value < 0:
        return "Значение не может быть меньше 0."
    if field_name == "step" and value <= 0:
        return "Шаг должен быть больше 0."
    if field_name == "min_rating" and value > Decimal("5"):
        return "Минимальный рейтинг должен быть от 0 до 5."
    if field_name == "min_price" and value > position.settings.max_price:
        return "Минимальная цена не может быть выше максимальной."
    if field_name == "max_price" and value < position.settings.min_price:
        return "Максимальная цена не может быть ниже минимальной."
    return None

