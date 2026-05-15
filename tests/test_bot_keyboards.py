from decimal import Decimal

from app.bot.keyboards import position_card_keyboard, positions_keyboard
from app.db.models import Position, PositionSettings


def _position() -> Position:
    position = Position(robux_amount=500, lot_id="2000", enabled=True, priority="high")
    position.settings = PositionSettings(
        min_price=Decimal("1"),
        max_price=Decimal("1000"),
        step=Decimal("1"),
        min_rating=Decimal("4.5"),
        ignore_no_rating=True,
        fallback_behavior="keep_current",
    )
    return position


def test_position_card_keyboard_has_no_priority_button() -> None:
    texts = [
        button.text
        for row in position_card_keyboard(_position()).inline_keyboard
        for button in row
    ]

    assert all("Приоритет" not in text for text in texts)


def test_positions_list_does_not_show_priority_label() -> None:
    texts = [
        button.text
        for row in positions_keyboard([_position()]).inline_keyboard
        for button in row
    ]

    assert all("высокий" not in text for text in texts)
    assert all("обычный" not in text for text in texts)
