from datetime import UTC, datetime

from app.bot.formatters import format_logs
from app.db.models import PriceUpdateLog, UpdateStatus


def test_logs_are_human_readable_for_no_target_price() -> None:
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SKIPPED.value,
        reason="no_target_price",
        created_at=datetime(2026, 5, 15, tzinfo=UTC),
    )

    text = format_logs([(log, 40)])

    assert "40 робуксов: пропущено" in text
    assert "Причина: не удалось рассчитать цену" in text
    assert "Что проверить: подключение Starvell, цену конкурента, мою текущую цену" in text


def test_logs_translate_missing_lot_id() -> None:
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SKIPPED.value,
        reason="missing_lot_id",
        created_at=datetime(2026, 5, 15, tzinfo=UTC),
    )

    text = format_logs([(log, 40)])

    assert "40 робуксов: пропущено" in text
    assert "Причина: не найден ID лота" in text
    assert "Что проверить: указать ID лота в карточке позиции" in text
