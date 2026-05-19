from datetime import UTC, datetime
from decimal import Decimal

from app.bot.formatters import (
    format_errors_screen,
    format_log_page,
    format_limits_screen,
    format_logs,
    format_main_menu,
    format_misc_menu,
    format_position_card,
    format_price_change_toggle_result,
    format_price_write_screen,
    format_proxy_screen,
    format_scheduler_screen,
    format_status_overview,
    format_technical_status,
    format_worker_servers,
)
from app.bot.keyboards import (
    logs_pagination_keyboard,
    main_menu_keyboard,
    misc_menu_keyboard,
    position_card_keyboard,
    proxy_pagination_keyboard,
    status_sections_keyboard,
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
    assert "🕒 Время:\n15.05.2026 13:08" in text
    assert "🟡 пропущено" in text
    assert "Причина:\nНет подходящих конкурентов. Цена оставлена без изменений." in text
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
    assert "🟡 пропущено" in text
    assert "Причина:\nНе найден ID лота." in text
    assert "Что проверить:" not in text


def test_logs_include_positions_without_history() -> None:
    position = Position(robux_amount=22500, lot_id="2012")

    text = format_logs([(position, None)])

    assert "📦 22500 робуксов" in text
    assert "🆔 ID: 2012" in text
    assert "⚪ еще не проверялась" in text


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
    assert "Изменение цен:" in text
    assert "только анализ" in text
    assert "Dry-run" not in text


def test_proxy_profiles_do_not_show_legacy_all_heartbeat_in_user_ui() -> None:
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

    assert "203.0.113.20" not in text
    assert text.count("нет данных") >= 3


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
    assert "⏱ Частота: 0.8–1.3 сек" in text
    assert "High-позиция" not in text
    assert "Normal" not in text


def test_main_menu_is_minimal_start_screen() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        account_request_usage_per_minute=258,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )

    text = format_main_menu(
        dry_run=False,
        real_price_writes_enabled=True,
        price_write_endpoint_configured=True,
        proxy_mode="enabled",
        heartbeats=[heartbeat],
        request_usage=258,
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
    )

    assert "🤖 Starvell Repricer" in text
    assert "Автоматическое управление ценами Starvell." in text
    assert "Выберите раздел ниже." in text
    assert "💰 Реальные цены:" not in text
    assert "🤖 Worker:" not in text
    assert "🌐 Прокси:" not in text
    assert "🚦 Нагрузка:" not in text
    assert "Proxy capacity" not in text
    assert "request_usage" not in text
    assert "last429" not in text


def test_main_menu_keyboard_keeps_technical_sections_in_misc() -> None:
    markup = main_menu_keyboard(dry_run=False)
    rows = [[button.callback_data for button in row] for row in markup.inline_keyboard]

    assert rows == [
        ["positions:list", "price:status"],
        ["status:show", "logs:recent"],
        ["misc:show"],
    ]
    assert all(
        "Управление" not in button.text
        for row in markup.inline_keyboard
        for button in row
    )


def test_misc_menu_contains_technical_sections() -> None:
    text = format_misc_menu()
    markup = misc_menu_keyboard()
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]

    assert "📂 Прочее" in text
    assert "Здесь собраны технические разделы." in text
    assert "proxies:show" in callbacks
    assert "limits:show" in callbacks
    assert "scheduler:show" in callbacks
    assert "errors:show" in callbacks
    assert "technical:status" in callbacks


def test_section_keyboards_do_not_leak_unrelated_buttons() -> None:
    status_callbacks = [
        button.callback_data
        for row in status_sections_keyboard().inline_keyboard
        for button in row
    ]
    proxy_callbacks = [
        button.callback_data
        for row in proxy_pagination_keyboard(page=0, total=3).inline_keyboard
        for button in row
    ]

    assert status_callbacks == ["status:show", "menu:main"]
    assert "limits:show" not in proxy_callbacks
    assert "proxies:refresh:0" in proxy_callbacks


def test_status_overview_is_short_and_proxy_aware() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_2",
        hostname="server",
        public_ip="45.132.20.205",
        assigned_positions=[400, 1200, 1700, 2000],
        request_limit_per_minute=100,
        account_request_usage_per_minute=258,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )
    latest_update = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SUCCESS.value,
        new_price=Decimal("580.70"),
        created_at=datetime(2026, 5, 16, 23, 21, tzinfo=UTC),
    )

    text = format_status_overview(
        worker_state=WorkerState(
            name="repricer:fast_2",
            last_heartbeat_at=datetime.now(UTC),
            last_cycle_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
            last_position_amount=400,
            last_status="success",
            last_error=None,
        ),
        heartbeats=[heartbeat],
        dry_run=False,
        real_price_writes_enabled=True,
        price_write_endpoint_configured=True,
        latest_price_update=(latest_update, _position(800, "2002")),
        request_usage=258,
        global_limit=settings.global_request_limit_per_minute,
        group_infos=settings.worker_group_infos,
    )

    assert "📊 Статус" in text
    assert "🤖 Worker:\n✅ активен" in text
    assert "💰 Реальные цены:\n✅ активно" in text
    assert "🌐 Прокси:\n✅ активны (1/3)" in text
    assert "🚦 Нагрузка:\n258/300" in text
    assert "🧯 Ошибки:\nнет" in text
    assert "800 робуксов" not in text
    assert "580.70 ₽" not in text
    assert "High:" not in text
    assert "Proxy capacity" not in text


def test_proxy_screen_contains_only_proxy_health() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )

    text = format_proxy_screen(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
    )

    assert "🌐 Прокси" in text
    assert "1/3" in text
    assert "🚀 Fast 1" in text
    assert "IP:\n45.132.20.115" in text
    assert "500 · 800 · 1000" in text
    assert "Лимит:\n100/мин" in text
    assert "Нагрузка:\n0/100" in text
    assert "Статус:\n✅ активен" in text
    assert "Пропускная способность" not in text


def test_proxy_screen_is_paginated_one_profile_per_page() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_2",
        hostname="server",
        public_ip="45.132.20.205",
        assigned_positions=[400, 1200, 1700, 2000],
        request_limit_per_minute=100,
        profile_request_usage_per_minute=89,
        current_delay_seconds=2.6,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )

    text = format_proxy_screen(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
        page=1,
    )

    assert "🌐 Прокси" in text
    assert "2/3" in text
    assert "🚀 Fast 2" in text
    assert "45.132.20.205" in text
    assert "400 · 1200 · 1700 · 2000" in text
    assert "Нагрузка:\n89/100" in text
    assert "Текущий интервал:\n2.6 сек" in text
    assert "Fast 1" not in text
    assert "Slow" not in text


def test_proxy_pagination_keyboard_hides_unavailable_edges() -> None:
    first_page = proxy_pagination_keyboard(page=0, total=3)
    middle_page = proxy_pagination_keyboard(page=1, total=3)
    first_texts = [button.text for row in first_page.inline_keyboard for button in row]
    middle_callbacks = [
        button.callback_data
        for row in middle_page.inline_keyboard
        for button in row
    ]

    assert "⬅️ Предыдущий" not in first_texts
    assert "➡️ Следующий" in first_texts
    assert "proxies:page:0" in middle_callbacks
    assert "proxies:page:2" in middle_callbacks


def test_limits_screen_uses_russian_labels() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        profile_request_usage_per_minute=82,
        account_request_usage_per_minute=258,
        account_effective_limit_per_minute=300,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )

    text = format_limits_screen(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
        request_usage=258,
        global_limit=settings.global_request_limit_per_minute,
    )

    assert "🚦 Лимиты" in text
    assert "🌐 Общая мощность:" in text
    assert "🧠 Лимит аккаунта:" in text
    assert "Fast1: 82/100" in text
    assert "Итого:\n258/300" in text
    assert "📉 Замедление:\nнет" in text
    assert "Последний 429" not in text
    assert "Proxy capacity" not in text
    assert "Backoff" not in text


def test_scheduler_screen_is_separate_from_limits() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        current_delay_seconds=2.3,
        last_seen_at=datetime.now(UTC),
        status="success",
        dry_run=False,
    )

    text = format_scheduler_screen(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
    )

    assert "🧠 Планировщик" in text
    assert "🚀 Fast1" in text
    assert "Интервал:\n1.5–2.7 сек" in text
    assert "⚡ 500 робуксов:" in text
    assert "0.8–1.3 сек через Fast 1" in text
    assert "Текущий:\n2.3 сек" in text
    assert "Нагрузка" not in text


def test_technical_status_keeps_raw_fields_separate() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="fast_1",
        hostname="server",
        public_ip="45.132.20.115",
        assigned_positions=[500, 800, 1000],
        request_limit_per_minute=100,
        last_seen_at=datetime.now(UTC),
        status="safe_mode_429",
        safe_mode=True,
        dry_run=False,
    )
    worker_state = WorkerState(
        name="repricer:fast_2",
        last_heartbeat_at=datetime.now(UTC),
        last_cycle_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
        last_position_amount=400,
        last_status="failed",
        last_error="http_400",
    )

    text = format_technical_status(
        worker_states=[worker_state],
        heartbeats=[heartbeat],
        request_usage=12,
        global_limit=settings.global_request_limit_per_minute,
        recent_errors=[],
        group_infos=settings.worker_group_infos,
    )

    assert "🔧 Технический статус" in text
    assert "worker_group=fast_1" in text
    assert "record_status=актуальная запись" in text
    assert "status=safe_mode_429" in text
    assert "last_error=http_400" in text


def test_technical_status_marks_legacy_records() -> None:
    settings = Settings(_env_file=None)
    heartbeat = WorkerHeartbeat(
        worker_group="all",
        hostname="old-server",
        public_ip="203.0.113.20",
        assigned_positions=[500],
        request_limit_per_minute=100,
        last_seen_at=datetime(2026, 5, 15, 18, 26, tzinfo=UTC),
        status="dry_run",
        dry_run=True,
    )
    worker_state = WorkerState(
        name="repricer",
        last_heartbeat_at=datetime(2026, 5, 15, 18, 26, tzinfo=UTC),
        last_cycle_at=datetime(2026, 5, 15, 18, 26, tzinfo=UTC),
        last_position_amount=500,
        last_status="dry_run",
    )

    text = format_technical_status(
        worker_states=[worker_state],
        heartbeats=[heartbeat],
        request_usage=0,
        global_limit=settings.global_request_limit_per_minute,
        recent_errors=[],
        group_infos=settings.worker_group_infos,
    )

    assert "worker_group=all" in text
    assert "name=repricer" in text
    assert text.count("record_status=устаревшая запись") == 2


def test_limits_screen_shows_effective_limit_backoff_and_last_429() -> None:
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

    text = format_limits_screen(
        group_infos=settings.worker_group_infos,
        heartbeats=[heartbeat],
        request_usage=12,
        global_limit=settings.global_request_limit_per_minute,
    )

    assert "Fast1: 0/100" in text
    assert "📉 Замедление:\nактивно" in text
    assert "Последний 429" not in text


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
    assert "⏱ Частота: 2.0–4.0 сек" in text
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
    assert "⏱ Частота: 0.8–1.3 сек" in text
    assert "• Текущая: 1.9 сек" in text


def test_errors_screen_explains_safe_mode_without_technical_links() -> None:
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

    text = format_errors_screen(
        latest_price_write_error=None,
        recent_errors=[],
        heartbeats=[heartbeat],
    )

    assert "🧯 Ошибки" in text
    assert "Прокси:\nесть предупреждения" in text
    assert "Последняя ошибка:\nнет" in text
    assert "developer.mozilla.org" not in text


def test_errors_screen_hides_old_errors_after_successful_price_update() -> None:
    old_error = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.FAILED.value,
        reason="http_400",
        created_at=datetime(2026, 5, 16, 17, 55, tzinfo=UTC),
    )
    success_log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SUCCESS.value,
        new_price=Decimal("580.70"),
        created_at=datetime(2026, 5, 16, 23, 21, tzinfo=UTC),
    )

    text = format_errors_screen(
        latest_price_update=(success_log, _position(800, "2002")),
        latest_price_write_error=(old_error, _position(500, "2000")),
        recent_errors=[(old_error, _position(500, "2000"))],
        heartbeats=[],
    )

    assert "Актуальных ошибок нет." in text
    assert "Последняя старая ошибка:" in text
    assert "http 400" in text
    assert "Сейчас запись цены работает." in text
    assert "Системные:\nесть ошибки" not in text


def test_price_write_screen_shows_ready_state() -> None:
    settings = Settings(_env_file=None)
    success_log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SUCCESS.value,
        reason="competitor_undercut",
        new_price=Decimal("315.00"),
        created_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    text = format_price_write_screen(
        dry_run=False,
        real_price_writes_enabled=True,
        price_write_endpoint_configured=True,
        latest_price_update=(success_log, _position(500, "2000")),
        latest_price_write_error=None,
    )

    assert "💰 Изменение цен" in text
    assert "Статус:\n✅ включено" in text
    assert "Режим:" not in text
    assert "реальные изменения" not in text
    assert "Endpoint:\n✅ настроен" in text
    assert "Последнее успешное изменение:" not in text
    assert "Последняя ошибка:" not in text
    assert "500 робуксов" not in text
    assert "ID 2000" not in text


def test_price_change_toggle_result_is_clear_when_ready() -> None:
    text = format_price_change_toggle_result(
        dry_run=False,
        real_price_writes_enabled=True,
        endpoint_configured=True,
    )

    assert text == "✅ Изменение цен включено\n\nРеальная запись настроена и активна."


def test_price_change_toggle_result_explains_missing_endpoint() -> None:
    text = format_price_change_toggle_result(
        dry_run=False,
        real_price_writes_enabled=True,
        endpoint_configured=False,
    )

    assert "endpoint не настроен" in text
    assert "Реальные цены меняться не будут." in text


def test_price_change_toggle_result_explains_analysis_mode() -> None:
    text = format_price_change_toggle_result(
        dry_run=True,
        real_price_writes_enabled=True,
        endpoint_configured=True,
    )

    assert text == "🛑 Изменение цен остановлено\n\nБот продолжит анализировать рынок, но цены менять не будет."


def test_price_write_screen_is_action_only_when_disabled() -> None:
    text = format_price_write_screen(
        dry_run=True,
        real_price_writes_enabled=True,
        price_write_endpoint_configured=True,
        latest_price_update=None,
        latest_price_write_error=None,
    )

    assert text == (
        "💰 Изменение цен\n\n"
        "Статус:\n"
        "🛑 выключено\n\n"
        "Бот анализирует рынок, но цены не меняет."
    )


def test_log_page_shows_one_action_per_page() -> None:
    settings = Settings(_env_file=None)
    first_position = _position(40, None)
    second_position = _position(500, "2000")
    first_log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SKIPPED.value,
        reason="missing_lot_id",
        created_at=datetime(2026, 5, 15, 11, 16, tzinfo=UTC),
    )
    second_log = PriceUpdateLog(
        position_id=2,
        status=UpdateStatus.SUCCESS.value,
        reason="updated",
        old_price=Decimal("312.50"),
        competitor_price=Decimal("310.00"),
        new_price=Decimal("309.70"),
        created_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    text = format_log_page(
        [(first_position, first_log), (second_position, second_log)],
        page=0,
        proxy_mode="enabled",
        group_infos=settings.worker_group_infos,
    )

    assert "📝 Последние действия" in text
    assert "1/2" in text
    assert "📦 40 робуксов" in text
    assert "🆔 ID: не указан" in text
    assert "🌐 Группа: Slow" in text
    assert "🟡 пропущено" in text
    assert "Причина:\nНе найден ID лота." in text
    assert "Что проверить:" not in text
    assert "500 робуксов" not in text


def test_logs_pagination_keyboard_hides_unavailable_edges() -> None:
    first_page = logs_pagination_keyboard(page=0, total=2)
    last_page = logs_pagination_keyboard(page=1, total=2)
    first_texts = [button.text for row in first_page.inline_keyboard for button in row]
    last_texts = [button.text for row in last_page.inline_keyboard for button in row]

    assert "⬅️ Предыдущая" not in first_texts
    assert "➡️ Следующая" in first_texts
    assert "⬅️ Предыдущая" in last_texts
    assert "➡️ Следующая" not in last_texts


def test_position_card_keyboard_is_compact() -> None:
    markup = position_card_keyboard(_position(500, "2000"))
    rows = [[button.callback_data for button in row] for row in markup.inline_keyboard]

    assert ["position:edit:min_price:500", "position:edit:max_price:500"] in rows
    assert ["position:edit:step:500", "position:edit:min_rating:500"] in rows
    assert ["position:edit:lot_id:500", "position:group:500"] in rows
    assert ["position:toggle_ignore:500", "position:competitors:500"] in rows


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
    assert "Причина:\nПричина не записана. Нужно проверить лог worker." in text
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

    assert "Причина:\nСайт ограничил частоту запросов." in text
    assert "developer.mozilla.org" not in text


def test_logs_translate_min_price_bounce_reason() -> None:
    position = _position(500, "2000")
    log = PriceUpdateLog(
        position_id=1,
        status=UpdateStatus.SUCCESS.value,
        reason="min_price_bounce_to_upper_competitor",
        old_price=Decimal("75.00"),
        competitor_price=Decimal("90.00"),
        new_price=Decimal("89.90"),
        created_at=datetime(2026, 5, 15, 19, 10, tzinfo=UTC),
    )

    text = format_logs([(position, log)])

    assert (
        "Причина:\nконкурент ниже минимума"
    ) in text
    assert "Что проверить:" not in text
    assert "🏆 Конкурент:\n90.00 ₽" in text
    assert "📉 Расчетная:\n89.90 ₽" in text
