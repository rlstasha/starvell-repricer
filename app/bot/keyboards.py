from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Position


def main_menu_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📦 Позиции", callback_data="positions:list")],
            [InlineKeyboardButton(text="💰 Изменение цен", callback_data="price:status")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status:show")],
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="proxies:show"),
                InlineKeyboardButton(text="🚦 Лимиты", callback_data="limits:show"),
            ],
            [InlineKeyboardButton(text="📝 Логи последних действий", callback_data="logs:recent")],
            [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings:general")],
        ]
    )


def back_to_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")]]
    )


def back_to_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="status:show")]]
    )


def status_sections_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💰 Изменение цен", callback_data="price:status")],
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="proxies:show"),
                InlineKeyboardButton(text="🚦 Лимиты", callback_data="limits:show"),
            ],
            [
                InlineKeyboardButton(text="🧠 Планировщик", callback_data="scheduler:show"),
                InlineKeyboardButton(text="🧯 Ошибки", callback_data="errors:show"),
            ],
            [InlineKeyboardButton(text="📝 Последние действия", callback_data="logs:recent")],
            [InlineKeyboardButton(text="🔧 Технический статус", callback_data="technical:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def price_status_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    toggle_text = (
        "💰 Включить изменение цен"
        if dry_run
        else "🛑 Остановить изменение цен"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="settings:toggle_dry_run")],
            [InlineKeyboardButton(text="🧪 Тест записи цены", callback_data="price:test_write")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="status:show")],
        ]
    )


def positions_keyboard(positions: list[Position]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for position in positions:
        marker = "🟢" if position.enabled else "🔴"
        lot_id = position.lot_id or "ID не указан"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker} {position.robux_amount} робуксов · {lot_id}",
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
            ],
            [InlineKeyboardButton(text=ignore_text, callback_data=f"position:toggle_ignore:{amount}")],
            [InlineKeyboardButton(text="📊 Конкуренты", callback_data=f"position:competitors:{amount}")],
            [InlineKeyboardButton(text="🧪 Тест расчета цены", callback_data=f"position:test_calc:{amount}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="positions:list")],
        ]
    )


def general_settings_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    price_change_text = (
        "💰 Включить изменение цен"
        if dry_run
        else "🛑 Остановить изменение цен"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=price_change_text, callback_data="settings:toggle_dry_run")],
            [InlineKeyboardButton(text="📊 Статус", callback_data="status:show")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )
