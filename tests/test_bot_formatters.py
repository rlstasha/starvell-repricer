from datetime import UTC, datetime
from decimal import Decimal

from app.bot.formatters import (
    format_general_settings,
    format_logs,
    format_position_card,
    format_proxy_status,
    format_worker_servers,
)
from app.core.config import Settings
from app.db.models import (
    Position,
    PositionScheduleState,
    PositionSettings,
    PositionState,
    PriceUpdateLog,
    UpdateStatus,
    WorkerHeartbeat,
    WorkerState,
)


def _position(amount: int, lot_id: str | None = None) -> Position:
    position = Position(
        robux_amount=amount,
        lot_id=lot_id,
        enabled=True,
        priority="high",
    )
    position.settings = PositionSettings(
        min_price=Decimal("1"),
        max_price=Decimal("9999"),
        step=Decimal("1"),
        min_rating=Decimal("4.5"),
        ignore_no_rating=True,
        fallback_behavior="keep_current",
    )
    position.state = PositionState(
        current_own_price=Decimal("312.50"),
        last_seen_competitor_price=Decimal("310.00"),
        calculated_price=Decimal("309.70"),
        last_update_time=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )
    return position


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
    assert "Что проверить: фильтр рейтинга, категорию, список конкурентов" in text
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

    assert "📊 Прокси и лимиты" in text
    assert "🚀 Fast 1" in text
    assert "500 · 800 · 1000" in text
    assert "Частота: 1.5–2.2 сек" in text
    assert "🚀 Fast 2" in text
    assert "400 · 1200 · 1700 · 2000" in text
    assert "Частота: 2.0–3.0 сек" in text
    assert "🐢 Slow" in text
    assert "Частота: 4.5–6.5 сек" in text
    assert "203.0.113.10" in text
    assert "Dry-run: включен" in text


def test_proxy_profiles_use_direct_worker_heartbeat_as_fallback() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="all",
        hostname="server",
        public_ip="203.0.113.20",
        assigned_positions=[500],
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

    assert text.count("203.0.113.20") == 3


def test_position_card_uses_proxy_group_frequency_and_ip() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="dry_run",
        dry_run=True,
    )

    text = format_position_card(
        _position(500, "2000"),
        proxy_mode="enabled",
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
    )

    assert "🌐 Прокси-группа: Fast 1" in text
    assert "🌍 IP: 45.132.20.115" in text
    assert "⏱ Частота: 1.5–2.2 сек" in text
    assert "High-позиция" not in text
    assert "Normal" not in text


def test_general_settings_are_proxy_aware() -> None:
    settings = Settings(_env_file=None)

    text = format_general_settings(
        dry_run=True,
        request_limit=settings.request_limit_per_minute,
        high_percent=settings.high_priority_percent,
        normal_percent=settings.normal_priority_percent,
        proxy_mode="enabled",
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
    )

    assert "🌐 Режим запросов: прокси" in text
    assert "🚦 Общий лимит: 300/мин" in text
    assert "🧠 Account effective limit: 300/мин" in text
    assert "🚀 Fast 1" in text
    assert "500 · 800 · 1000" in text
    assert "High:" not in text
    assert "Normal:" not in text


def test_proxy_status_does_not_show_high_normal_budget() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_2",
        hostname="server",
        public_ip="45.132.20.205",
        assigned_positions=[400, 1200, 1700, 2000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="dry_run",
        dry_run=True,
    )
    worker_state = WorkerState(
        name="repricer:fast_2",
        last_heartbeat_at=datetime.now(UTC),
        last_cycle_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
        last_position_amount=400,
        last_status="dry_run",
        last_error=None,
    )

    text = format_proxy_status(
        worker_states=[worker_state],
        heartbeats=[heartbeat],
        dry_run=True,
        request_usage=12,
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
        success_count=0,
        error_count=0,
        recent_errors=[],
        last_positions_by_amount={400: _position(400, "1999")},
    )

    assert "🌐 Режим: прокси" in text
    assert "Proxy capacity:" in text
    assert "300/мин" in text
    assert "🚀 Fast 2" in text
    assert "IP: 45.132.20.205" in text
    assert "Последняя позиция: 400 робуксов, ID 1999" in text
    assert "High:" not in text
    assert "Normal:" not in text


def test_proxy_status_shows_effective_limit_backoff_and_last_429() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        effective_request_limit_per_minute=80,
        last_seen_at=datetime.now(UTC),
        status="safe_mode_429",
        errors_429=2,
        consecutive_errors=2,
        backoff_active=True,
        last_429_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
        safe_mode=True,
        dry_run=True,
    )

    text = format_proxy_status(
        worker_states=[],
        heartbeats=[heartbeat],
        dry_run=True,
        request_usage=12,
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
        success_count=0,
        error_count=2,
        recent_errors=[],
        last_positions_by_amount={},
    )

    assert "Лимит: 100/мин" in text
    assert "Effective: 80/мин" in text
    assert "Backoff: активен" in text
    assert "Last 429: 15.05.2026 22:10" in text
    assert "Средний интервал: 2.5–4.0 сек" in text


def test_position_card_uses_effective_proxy_frequency_after_backoff() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        effective_request_limit_per_minute=50,
        current_delay_seconds=3.6,
        last_seen_at=datetime.now(UTC),
        status="dry_run",
        backoff_active=True,
        dry_run=True,
    )

    text = format_position_card(
        _position(500, "2000"),
        proxy_mode="enabled",
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
    )

    assert "🌐 Прокси-группа: Fast 1" in text
    assert "⏱ Частота: 2.5–4.0 сек" in text
    assert "• Текущая: 3.6 сек" in text


def test_position_card_shows_market_activity_from_schedule_state() -> None:
    settings = Settings(_env_file=None)
    schedule_state = PositionScheduleState(
        position_id=1,
        position_amount=500,
        lot_id="2000",
        proxy_profile="fast_1",
        base_interval_seconds=1.8,
        current_interval_seconds=1.9,
        change_score=0.9,
        error_score=0.0,
    )

    text = format_position_card(
        _position(500, "2000"),
        proxy_mode="enabled",
        group_infos=settings.worker_group_infos,
        heartbeats=[],
        schedule_states=[schedule_state],
    )

    assert "🧠 Активность рынка: высокая" in text
    assert "• Текущая: 1.9 сек" in text


def test_proxy_status_explains_safe_mode_without_technical_links() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="safe_mode_429",
        errors_429=4,
        consecutive_errors=4,
        account_request_usage_per_minute=38,
        safe_mode=True,
        dry_run=True,
    )

    text = format_proxy_status(
        worker_states=[],
        heartbeats=[heartbeat],
        dry_run=True,
        request_usage=38,
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
        success_count=0,
        error_count=4,
        recent_errors=[],
        last_positions_by_amount={},
    )

    assert "🟡 Safe mode активен" in text
    assert "Причина: Starvell временно ограничил частоту запросов" in text
    assert "Запросов: 38/300" in text
    assert "Следующая попытка: через" in text
    assert "developer.mozilla.org" not in text


def test_logs_show_proxy_group_and_missing_reason_text() -> None:
    settings = Settings(_env_file=None)
    position = _position(1200, "2004")
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.FAILED.value,
        reason="",
        old_price=Decimal("312.50"),
        competitor_price=Decimal("310.00"),
        new_price=Decimal("309.70"),
        created_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    text = format_logs(
        [(position, log)],
        proxy_mode="enabled",
        group_infos=settings.worker_group_infos,
        heartbeats=[],
    )

    assert "🌐 Группа: Fast 2" in text
    assert "Причина: Причина не записана. Нужно проверить лог worker." in text
    assert "—" not in text


def test_logs_hide_technical_http_links_from_old_errors() -> None:
    position = _position(500, "2000")
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.FAILED.value,
        reason=(
            "Client error '429 Too Many Requests' for url "
            "'https://starvell.com/api/offers/list-by-category'. "
            "For more information check: https://developer.mozilla.org/"
        ),
        created_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    text = format_logs([(position, log)])

    assert "Причина: Сайт ограничил частоту запросов." in text
    assert "developer.mozilla.org" not in text
