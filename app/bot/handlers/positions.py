from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.ext.asyncio.session import AsyncSession

from app.bot.formatters import format_competitors, format_position_card, format_price_test
from app.bot.keyboards import position_card_keyboard, positions_keyboard
from app.bot.ui import (
    answer_loading,
    cleanup_pending_prompt,
    safe_edit_text,
    send_tracked_prompt,
)
from app.core.config import Settings
from app.db.models import Position
from app.db.repositories import (
    PositionRepository,
    PositionScheduleStateRepository,
    WorkerHeartbeatRepository,
)
from app.market.schemas import MarketOffer
from app.repricer.price_strategy import PriceCalculationSettings, UndercutByStepStrategy

router = Router()

EDIT_LABELS = {
    "min_price": "минимальную цену",
    "max_price": "максимальную цену",
    "step": "шаг",
    "min_rating": "минимальный рейтинг",
    "lot_id": "ID лота",
}
LOT_ID_CLEAR_VALUES = {"-", "нет", "не указан"}


class EditPositionState(StatesGroup):
    waiting_for_value = State()


@router.callback_query(lambda query: query.data == "positions:list")
async def list_positions(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    state: FSMContext,
) -> None:
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)
    await answer_loading(callback)
    async with session_factory() as session:
        positions = await PositionRepository(session).list_positions()
    await safe_edit_text(callback, "📦 Позиции", reply_markup=positions_keyboard(positions))


@router.callback_query(F.data.startswith("position:"))
async def position_actions(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    parts = callback.data.split(":")
    await cleanup_pending_prompt(state, callback.bot, clear_state=True)

    if len(parts) == 2:
        await answer_loading(callback)
        await _show_card(callback, session_factory, settings, int(parts[1]))
        return

    action = parts[1]
    amount = int(parts[-1])

    if action == "toggle":
        await answer_loading(callback)
        async with session_factory() as session:
            repo = PositionRepository(session)
            position = await repo.get_by_amount(amount)
            if position:
                position.enabled = not position.enabled
                await session.commit()
        await _show_card(callback, session_factory, settings, amount)
        return

    if action == "toggle_ignore":
        await answer_loading(callback)
        async with session_factory() as session:
            repo = PositionRepository(session)
            position = await repo.get_by_amount(amount)
            if position:
                position.settings.ignore_no_rating = not position.settings.ignore_no_rating
                await session.commit()
        await _show_card(callback, session_factory, settings, amount)
        return

    if action == "competitors":
        await answer_loading(callback)
        await _show_competitors(callback, session_factory, settings, amount)
        return

    if action == "test_calc":
        await answer_loading(callback)
        await _show_price_test(callback, session_factory, settings, amount)
        return

    if action == "edit":
        field_name = parts[2]
        if field_name not in EDIT_LABELS:
            await callback.answer("Неизвестная настройка.", show_alert=True)
            return
        await state.set_state(EditPositionState.waiting_for_value)
        await state.update_data(amount=amount, field_name=field_name)
        if field_name == "lot_id":
            await send_tracked_prompt(
                callback.message,
                state,
                "Введите ID лота. Чтобы очистить, отправьте: -",
            )
        else:
            await send_tracked_prompt(
                callback.message,
                state,
                f"Введите новую {EDIT_LABELS[field_name]}.",
            )
        await callback.answer("Жду значение")
        return

    await callback.answer("Неизвестное действие.", show_alert=True)


@router.message(EditPositionState.waiting_for_value)
async def save_position_value(
    message: Message,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    amount = int(data["amount"])
    field_name = data["field_name"]

    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        if position is None:
            await cleanup_pending_prompt(state, message.bot)
            await state.clear()
            await message.answer("Позиция не найдена.")
            return

        if field_name == "lot_id":
            lot_id, error = _parse_lot_id(message.text)
            if error:
                await message.answer(error)
                return
            await repo.set_lot_id(amount, lot_id)
        else:
            try:
                value = Decimal(message.text.strip().replace(",", "."))
            except (InvalidOperation, AttributeError):
                await message.answer("Нужно число. Например: 499 или 4.5")
                return

            error = _validate_numeric_setting(position, field_name, value)
            if error:
                await message.answer(error)
                return

            await repo.update_setting(amount, field_name, value)

        await session.commit()
        position = await repo.get_by_amount(amount)
        counts = await repo.count_by_priority(enabled_only=True)
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()

    await cleanup_pending_prompt(state, message.bot)
    await state.clear()
    await message.answer(
        "Настройка сохранена.\n\n"
        + _format_position_card(position, settings, counts, heartbeats, schedule_states),
        reply_markup=position_card_keyboard(position),
    )


async def _show_card(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    amount: int,
) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        counts = await repo.count_by_priority(enabled_only=True)
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()
    if position is None:
        await safe_edit_text(
            callback,
            "⚠️ Позиция не найдена.",
            reply_markup=positions_keyboard([]),
        )
        return
    await safe_edit_text(
        callback,
        _format_position_card(position, settings, counts, heartbeats, schedule_states),
        reply_markup=position_card_keyboard(position),
    )


async def _show_competitors(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    amount: int,
) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        competitors = await repo.list_recent_competitors(position) if position else []
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()
    if position is None:
        await safe_edit_text(
            callback,
            "⚠️ Позиция не найдена.",
            reply_markup=positions_keyboard([]),
        )
        return
    await safe_edit_text(
        callback,
        format_competitors(
            position,
            competitors,
            proxy_mode=settings.proxy_mode,
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            schedule_states=schedule_states,
        ),
        reply_markup=position_card_keyboard(position),
    )


async def _show_price_test(
    callback: CallbackQuery,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    amount: int,
) -> None:
    async with session_factory() as session:
        repo = PositionRepository(session)
        position = await repo.get_by_amount(amount)
        competitors = await repo.list_recent_active_competitors(position) if position else []
        heartbeats = await WorkerHeartbeatRepository(session).list_all()
        schedule_states = await PositionScheduleStateRepository(session).list_all()

    if position is None:
        await safe_edit_text(
            callback,
            "⚠️ Позиция не найдена.",
            reply_markup=positions_keyboard([]),
        )
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
    await safe_edit_text(
        callback,
        format_price_test(
            position=position,
            target_price=decision.target_price,
            competitor_price=decision.competitor_price,
            reason=decision.reason,
            should_update=decision.should_update,
            proxy_mode=settings.proxy_mode,
            group_infos=settings.worker_group_infos,
            heartbeats=heartbeats,
            schedule_states=schedule_states,
        ),
        reply_markup=position_card_keyboard(position),
    )


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


def _parse_lot_id(raw_value: str | None) -> tuple[str | None, str | None]:
    value = (raw_value or "").strip()
    if value.casefold() in LOT_ID_CLEAR_VALUES:
        return None, None
    if not value:
        return None, "Введите ID лота или отправьте -, чтобы очистить."
    if not value.isdigit() or int(value) <= 0:
        return None, "ID лота должен быть положительным числом."
    return value, None


def _format_position_card(
    position: Position,
    settings: Settings,
    counts: dict[str, int],
    heartbeats,
    schedule_states,
) -> str:
    return format_position_card(
        position,
        request_limit=settings.request_limit_per_minute,
        high_percent=settings.high_priority_percent,
        normal_percent=settings.normal_priority_percent,
        high_count=counts.get("high", 0),
        normal_count=counts.get("normal", 0),
        proxy_mode=settings.proxy_mode,
        group_infos=settings.worker_group_infos,
        heartbeats=heartbeats,
        schedule_states=schedule_states,
    )
