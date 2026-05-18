from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db.models import Position


def main_menu_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Позиции", callback_data="positions:list"),
                InlineKeyboardButton(text="💰 Изменение цен", callback_data="price:status"),
            ],
            [
                InlineKeyboardButton(text="📊 Статус", callback_data="status:show"),
                InlineKeyboardButton(text="📝 Последние действия", callback_data="logs:recent"),
            ],
            [
                InlineKeyboardButton(text="🛠 Управление", callback_data="settings:general"),
                InlineKeyboardButton(text="📂 Прочее", callback_data="misc:show"),
            ],
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


def back_to_misc_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="misc:show")]]
    )


def status_sections_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="status:show")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def misc_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="proxies:show"),
                InlineKeyboardButton(text="🚦 Лимиты", callback_data="limits:show"),
            ],
            [
                InlineKeyboardButton(text="🧠 Планировщик", callback_data="scheduler:show"),
                InlineKeyboardButton(text="🧯 Ошибки", callback_data="errors:show"),
            ],
            [InlineKeyboardButton(text="🔧 Технический статус", callback_data="technical:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )


def logs_pagination_keyboard(*, page: int, total: int) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="⬅️ Предыдущая", callback_data=f"logs:page:{page - 1}")
        )
    if page < total - 1:
        navigation.append(
            InlineKeyboardButton(text="➡️ Следующая", callback_data=f"logs:page:{page + 1}")
        )
    rows: list[list[InlineKeyboardButton]] = []
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"logs:refresh:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def proxy_pagination_keyboard(*, page: int, total: int) -> InlineKeyboardMarkup:
    navigation: list[InlineKeyboardButton] = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(text="⬅️ Предыдущий", callback_data=f"proxies:page:{page - 1}")
        )
    if page < total - 1:
        navigation.append(
            InlineKeyboardButton(text="➡️ Следующий", callback_data=f"proxies:page:{page + 1}")
        )
    rows: list[list[InlineKeyboardButton]] = []
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=f"proxies:refresh:{page}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="misc:show")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def price_status_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    toggle_text = (
        "💰 Включить изменение цен"
        if dry_run
        else "🛑 Остановить изменение цен"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_text, callback_data="settings:toggle_dry_run")],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="price:status")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
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
                InlineKeyboardButton(text="🆔 ID лота", callback_data=f"position:edit:lot_id:{amount}"),
                InlineKeyboardButton(text="🌐 Группа", callback_data=f"position:group:{amount}"),
            ],
            [
                InlineKeyboardButton(text=ignore_text, callback_data=f"position:toggle_ignore:{amount}"),
                InlineKeyboardButton(text="📊 Конкуренты", callback_data=f"position:competitors:{amount}"),
            ],
            [InlineKeyboardButton(text="🧪 Тест расчета цены", callback_data=f"position:test_calc:{amount}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="positions:list")],
        ]
    )


def general_settings_keyboard(*, dry_run: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📦 Лоты", callback_data="positions:list"),
                InlineKeyboardButton(text="💰 Изменение цен", callback_data="price:status"),
            ],
            [
                InlineKeyboardButton(text="🌐 Прокси", callback_data="proxies:show"),
                InlineKeyboardButton(text="🚦 Лимиты", callback_data="limits:show"),
            ],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu:main")],
        ]
    )
