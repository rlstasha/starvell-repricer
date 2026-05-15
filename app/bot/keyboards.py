from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Position


def main_menu_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    dry_run_text = "🧪 Dry-run выключить" if dry_run else "🧪 Dry-run включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Позиции", callback_data="positions:list")],
            [InlineKeyboardButton(text="⚙️ Общие настройки", callback_data="settings:general")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status:show")],
            [InlineKeyboardButton(text="📊 Серверы и лимиты", callback_data="servers:limits")],
            [InlineKeyboardButton(text=dry_run_text, callback_data="settings:toggle_dry_run")],
            [InlineKeyboardButton(text="📝 Логи последних действий", callback_data="logs:recent")],
        ]
    )


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]]
    )


def positions_keyboard(positions: list[Position]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for position in positions:
        marker = "🟢" if position.enabled else "🔴"
        priority = "высокий" if position.priority == "high" else "обычный"
        lot_id = position.lot_id or "ID не указан"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {position.robux_amount} робуксов · {lot_id} · {priority}",
                    callback_data=f"position:{position.robux_amount}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def position_card_keyboard(position: Position) -> InlineKeyboardMarkup:
    amount = position.robux_amount
    enabled_text = "🔴 Выключить" if position.enabled else "🟢 Включить"
    ignore_text = (
        "🚫 Не игнорировать без рейтинга"
        if position.settings.ignore_no_rating
        else "🚫 Игнор без рейтинга"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=enabled_text, callback_data=f"position:toggle:{amount}")],
            [
                InlineKeyboardButton(text="✏️ Мин. цена", callback_data=f"position:edit:min_price:{amount}"),
                InlineKeyboardButton(text="✏️ Макс. цена", callback_data=f"position:edit:max_price:{amount}"),
            ],
            [
                InlineKeyboardButton(text="✏️ Шаг", callback_data=f"position:edit:step:{amount}"),
                InlineKeyboardButton(text="⭐ Мин. рейтинг", callback_data=f"position:edit:min_rating:{amount}"),
            ],
            [
                InlineKeyboardButton(text="🔗 ID лота", callback_data=f"position:edit:lot_id:{amount}"),
                InlineKeyboardButton(text="⚡ Приоритет", callback_data=f"position:toggle_priority:{amount}"),
            ],
            [InlineKeyboardButton(text=ignore_text, callback_data=f"position:toggle_ignore:{amount}")],
            [InlineKeyboardButton(text="📊 Конкуренты", callback_data=f"position:competitors:{amount}")],
            [InlineKeyboardButton(text="🧪 Тест расчета цены", callback_data=f"position:test_calc:{amount}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="positions:list")],
        ]
    )


def general_settings_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    dry_run_text = "🧪 Dry-run выключить" if dry_run else "🧪 Dry-run включить"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=dry_run_text, callback_data="settings:toggle_dry_run")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status:show")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )
