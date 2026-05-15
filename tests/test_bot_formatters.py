from datetime import UTC, datetime

from app.bot.formatters import format_logs, format_worker_servers
from app.core.config import Settings
from app.db.models import Position, PriceUpdateLog, UpdateStatus, WorkerHeartbeat


def test_logs_show_all_position_context_for_no_competitors_keep_current_price() -> None:
    position = Position(robux_amount=40, lot_id=None)
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SKIPPED.value,
        reason="no_competitors_keep_current_price",
        created_at=datetime(2026, 5, 15, 10, 8, tzinfo=UTC),
    )

    text = format_logs([(position, log)])

    assert "📦 40 робуксов" in text
    assert "🆔 ID: не указан" in text
    assert "🕒 Время: 15.05.2026 13:08" in text
    assert "📌 Статус: пропущено" in text
    assert "Причина: Нет подходящих конкурентов. Цена оставлена без изменений." in text
    assert "Что проверить:" not in text
    assert "UTC" not in text


def test_logs_translate_missing_lot_id() -> None:
    position = Position(robux_amount=40, lot_id=None)
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SKIPPED.value,
        reason="missing_lot_id",
        created_at=datetime(2026, 5, 15, tzinfo=UTC),
    )

    text = format_logs([(position, log)])

    assert "📦 40 робуксов" in text
    assert "📌 Статус: пропущено" in text
    assert "Причина: Не найден ID лота." in text
    assert "Что проверить: указать ID лота в карточке позиции" in text


def test_logs_include_positions_without_history() -> None:
    position = Position(robux_amount=22500, lot_id="2012")

    text = format_logs([(position, None)])

    assert "📦 22500 робуксов" in text
    assert "🆔 ID: 2012" in text
    assert "📌 Статус: еще не проверялась" in text


def test_worker_servers_show_new_fast_split_and_frequency() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="vps-fast-1",
        public_ip="203.0.113.10",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="dry_run",
        dry_run=True,
    )

    text = format_worker_servers(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
        dry_run=True,
        global_limit=settings.global_request_limit_per_minute,
    )

    assert "🚀 Fast 1" in text
    assert "500\n800\n1000" in text
    assert "~1.8 сек" in text
    assert "🚀 Fast 2" in text
    assert "400\n1200\n1700\n2000" in text
    assert "~2.4 сек" in text
    assert "🐢 Slow" in text
    assert "~5.4 сек" in text
    assert "203.0.113.10" in text
    assert "Dry-run:\nвключен" in text
