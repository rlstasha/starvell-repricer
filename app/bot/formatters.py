from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.db.models import (
    CompetitorSnapshot,
    Position,
    PositionScheduleState,
    PriceUpdateLog,
    PriorityLevel,
    UpdateStatus,
    WorkerHeartbeat,
    WorkerState,
)
from app.repricer.adaptive_scheduler import display_interval_range, market_activity_label
from app.repricer.worker_groups import WorkerGroupInfo


REQUESTS_PER_POSITION_CHECK = 1
LOCAL_TIMEZONE = ZoneInfo("Europe/Moscow")
MONEY_QUANT = Decimal("0.01")
SEPARATOR = "━━━━━━━━━━━━"

STATUS_LABELS = {
    "success": "успешно",
    "updated": "цена обновлена",
    "running": "выполняется",
    "idle": "ожидает",
    "dry_run": "только анализ",
    "skipped": "пропущено",
    "failed": "ошибка",
    "error": "ошибка",
}

REASON_LABELS = {
    "no_target_price": "Не удалось рассчитать цену.",
    "no_competitors_keep_current_price": (
        "Нет подходящих конкурентов. "
        "Цена оставлена без изменений."
    ),
    "no_competitors_set_max_price": (
        "Нет подходящих конкурентов. "
        "Выбрана максимальная цена."
    ),
    "competitor_undercut": (
        "Найден конкурент. Расчетная цена ниже на шаг."
    ),
    "already_at_target": "Цена уже равна расчетной.",
    "dry_run": "Только анализ. Цена не изменена.",
    "updated": "Цена обновлена.",
    "skipped": "Позиция пропущена.",
    "error": "Произошла ошибка.",
    "missing_lot_id": "Не найден ID лота.",
    "real_price_writes_disabled": "Реальное изменение цен выключено.",
    "price_update_endpoint_missing": "Не настроен endpoint изменения цены.",
    "price_update_payload_unknown": "Неизвестный payload изменения цены.",
    "unauthorized": "Ошибка авторизации Starvell.",
    "forbidden": "Доступ временно запрещен.",
    "timeout": "Сайт слишком долго отвечает.",
    "rate_limited": "Сайт ограничил частоту запросов.",
    "starvell_server_error": "Ошибка на стороне Starvell.",
    "position_disabled": "Позиция выключена.",
    "position_not_found": "Позиция не найдена.",
}

IGNORE_REASON_LABELS = {
    "seller_inactive": "продавец неактивен",
    "own_seller": "это мой продавец",
    "no_rating": "нет рейтинга",
    "rating_too_low": "рейтинг ниже минимума",
}

CHECK_HINTS = {
    "no_target_price": (
        "подключение Starvell, цену конкурента, "
        "мою текущую цену"
    ),
    "no_competitors_keep_current_price": (
        "фильтр рейтинга, категорию, список конкурентов"
    ),
    "missing_lot_id": "указать ID лота в карточке позиции",
    "unauthorized": "MARKET_SESSION_COOKIE и права аккаунта Starvell",
    "forbidden": "доступ аккаунта Starvell и паузу перед следующей проверкой",
    "timeout": "прокси, сеть и доступность Starvell",
    "rate_limited": "лимит запросов и паузу между проверками",
}
MISSING_REASON_TEXT = "Причина не записана. Нужно проверить лог worker."


def money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    try:
        value = value.quantize(MONEY_QUANT)
    except (InvalidOperation, ValueError):
        return f"{value} ₽"
    return f"{value} ₽"


def dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TIMEZONE).strftime("%d.%m.%Y %H:%M")


def yes_no(value: bool | None) -> str:
    if value is None:
        return "—"
    return "да" if value else "нет"


def format_priority_frequency(
    *,
    priority: str,
    request_limit: int,
    high_percent: int,
    normal_percent: int,
    high_count: int,
    normal_count: int,
    enabled: bool = True,
) -> str:
    if not enabled:
        return "позиция выключена"

    percent = high_percent if priority == PriorityLevel.HIGH.value else normal_percent
    count = high_count if priority == PriorityLevel.HIGH.value else normal_count
    if count <= 0:
        return "нет включенных позиций этого приоритета"

    requests_per_minute = request_limit * percent / 100
    checks_per_minute = requests_per_minute / REQUESTS_PER_POSITION_CHECK
    if checks_per_minute <= 0:
        return "нет выделенного лимита"

    seconds = 60 * count / checks_per_minute
    return f"каждые ~{_duration(seconds)}"


def format_main_menu(*, dry_run: bool) -> str:
    return "\n".join(
        [
            SEPARATOR,
            "🤖 Starvell Repricer",
            "",
            "💰 Изменение цен:",
            _price_change_state_line(dry_run=dry_run),
            SEPARATOR,
        ]
    )


def format_position_card(
    position: Position,
    *,
    request_limit: int = 100,
    high_percent: int = 70,
    normal_percent: int = 30,
    high_count: int = 0,
    normal_count: int = 0,
    proxy_mode: str = "disabled",
    group_infos: list[WorkerGroupInfo] | None = None,
    heartbeats: list[WorkerHeartbeat] | None = None,
    schedule_states: list[PositionScheduleState] | None = None,
) -> str:
    settings = position.settings
    state = position.state
    lot_id = _lot_id_text(position)
    enabled = "включена" if position.enabled else "выключена"
    proxy_lines = _position_proxy_context_lines(
        position,
        group_infos=group_infos,
        heartbeats=heartbeats,
        proxy_mode=proxy_mode,
        group_label="Прокси-группа",
        include_ip=True,
        include_frequency=True,
        schedule_states=schedule_states,
    )

    lines = [
        SEPARATOR,
        f"📦 Позиция: {position.robux_amount} робуксов",
        f"🆔 ID лота: {lot_id}",
        "",
        "💰 Цены",
        f"• Моя цена: {money(state.current_own_price if state else None)}",
        f"• Конкурент: {money(state.last_seen_competitor_price if state else None)}",
        f"• Расчетная: {money(state.calculated_price if state else None)}",
        "",
        "⚙️ Настройки",
        f"• Мин. цена: {money(settings.min_price)}",
        f"• Макс. цена: {money(settings.max_price)}",
        f"• Шаг: {money(settings.step)}",
        f"• Мин. рейтинг: {settings.min_rating}",
        f"• Игнор без рейтинга: {yes_no(settings.ignore_no_rating)}",
    ]

    if proxy_lines:
        lines.extend(["", *proxy_lines])
    else:
        frequency = format_priority_frequency(
            priority=position.priority,
            enabled=position.enabled,
            request_limit=request_limit,
            high_percent=high_percent,
            normal_percent=normal_percent,
            high_count=high_count,
            normal_count=normal_count,
        )
        lines.extend(
            [
                "",
                f"⏱ Частота проверки: {frequency}",
            ]
        )

    lines.extend(
        [
            "",
            "📌 Состояние",
            f"• Работа: {enabled}",
            f"• Последнее обновление: "
            f"{dt(state.last_update_time if state else None)}",
        ]
    )

    if state and state.error_status:
        lines.append(
            "• Последняя проблема: "
            f"{_reason_label(_reason_key(state.error_status), state.error_status)}"
        )
    if not position.lot_id:
        lines.extend(
            ["", "⚠️ Не найден ID лота. Репрайс невозможен."]
        )
    lines.append(SEPARATOR)
    return "\n".join(lines)


def format_competitors(
    position: Position,
    competitors: list[CompetitorSnapshot],
    *,
    proxy_mode: str = "disabled",
    group_infos: list[WorkerGroupInfo] | None = None,
    heartbeats: list[WorkerHeartbeat] | None = None,
    schedule_states: list[PositionScheduleState] | None = None,
) -> str:
    lines = [
        SEPARATOR,
        "📊 Конкуренты",
        f"📦 Позиция: {position.robux_amount} робуксов",
        f"🆔 ID лота: {_lot_id_text(position)}",
    ]
    proxy_lines = _position_proxy_context_lines(
        position,
        group_infos=group_infos,
        heartbeats=heartbeats,
        proxy_mode=proxy_mode,
        group_label="Группа",
        include_ip=False,
        include_frequency=True,
        schedule_states=schedule_states,
    )
    if proxy_lines:
        lines.extend(proxy_lines)
    lines.append("")
    if not competitors:
        lines.extend(
            [
                "Пока нет сохраненных конкурентов.",
                "Что проверить: worker уже обработал позицию, "
                "фильтр рейтинга "
                "и данные рынка Starvell.",
                SEPARATOR,
            ]
        )
        return "\n".join(lines)

    for index, item in enumerate(competitors, start=1):
        seller = item.seller_username or item.seller_id or "неизвестно"
        offer_id = _snapshot_offer_id(item)
        lines.extend(
            [
                f"{index}. 🏷️ Продавец: {seller}",
                f"   🆔 Offer ID: {offer_id or 'не найден'}",
                f"   💰 Цена: {money(item.price)}",
                f"   ⭐ Рейтинг: {item.rating if item.rating is not None else '—'}",
                f"   ✅ Активен: {yes_no(item.is_active)}",
            ]
        )
        if item.is_ignored:
            lines.append(f"   🚫 Игнор: {_ignore_reason_label(item.ignore_reason)}")
        lines.append("")

    lines.append(SEPARATOR)
    return "\n".join(lines).rstrip()


def format_price_test(
    *,
    position: Position,
    target_price: Decimal | None,
    competitor_price: Decimal | None,
    reason: str,
    should_update: bool,
    proxy_mode: str = "disabled",
    group_infos: list[WorkerGroupInfo] | None = None,
    heartbeats: list[WorkerHeartbeat] | None = None,
    schedule_states: list[PositionScheduleState] | None = None,
) -> str:
    action = (
        "цена будет изменена"
        if should_update
        else "изменение не требуется"
    )
    lines = [
        SEPARATOR,
        "🧪 Тест расчета цены",
        f"📦 Позиция: {position.robux_amount} робуксов",
        f"🆔 ID лота: {_lot_id_text(position)}",
    ]
    proxy_lines = _position_proxy_context_lines(
        position,
        group_infos=group_infos,
        heartbeats=heartbeats,
        proxy_mode=proxy_mode,
        group_label="Группа",
        include_ip=False,
        include_frequency=True,
        schedule_states=schedule_states,
    )
    if proxy_lines:
        lines.extend(proxy_lines)
    lines.extend(
        [
            "",
            "💰 Расчет",
            f"• Моя цена: "
            f"{money(position.state.current_own_price if position.state else None)}",
            f"• Конкурент: {money(competitor_price)}",
            f"• Новая цена: {money(target_price)}",
            "",
            "📌 Результат",
            f"• Статус: {action}",
            f"• Причина: {_reason_label(_reason_key(reason), reason)}",
        ]
    )
    if not position.lot_id:
        lines.extend(
            ["", "⚠️ Не найден ID лота. Репрайс невозможен."]
        )
    lines.append(SEPARATOR)
    return "\n".join(lines)


def format_general_settings(
    *,
    dry_run: bool,
    request_limit: int,
    high_percent: int,
    normal_percent: int,
    real_price_writes_enabled: bool = False,
    price_write_endpoint_configured: bool = False,
    proxy_mode: str = "disabled",
    global_limit: int | None = None,
    group_infos: list[WorkerGroupInfo] | None = None,
    heartbeats: list[WorkerHeartbeat] | None = None,
) -> str:
    if _use_proxy_ui(proxy_mode) and group_infos:
        heartbeat_by_group = {
            heartbeat.worker_group: heartbeat
            for heartbeat in (heartbeats or [])
        }
        lines = [
            SEPARATOR,
            "⚙️ Общие настройки",
            "",
            "💰 Изменение цен:",
            _price_change_state_line(
                dry_run=dry_run,
                real_price_writes_enabled=real_price_writes_enabled,
                endpoint_configured=price_write_endpoint_configured,
            ),
            "🌐 Режим запросов: прокси",
            f"🚦 Общий лимит: {global_limit or request_limit}/мин",
            f"🧠 Account effective limit: {_account_effective_limit(heartbeats or [], global_limit or request_limit)}/мин",
            f"📉 Backoff: {'активен' if _account_backoff_active(heartbeats or [], _account_effective_limit(heartbeats or [], global_limit or request_limit), global_limit or request_limit) else 'нет'}",
            "",
        ]
        for info in group_infos:
            heartbeat = heartbeat_by_group.get(info.name) or heartbeat_by_group.get("all")
            configured_limit = _configured_limit(info, heartbeat)
            effective_limit = _effective_limit(info, heartbeat)
            interval_min, interval_max = _heartbeat_interval_range(info, heartbeat)
            lines.extend(
                [
                    f"{info.icon} {info.label}",
                    f"• Configured limit: {configured_limit}/мин",
                    f"• Effective limit: {effective_limit}/мин",
                    f"• Backoff: {_backoff_text(info, heartbeat)}",
                    f"• Last 429: {_last_429_text(heartbeat)}",
                    f"• Позиции: {_positions_inline(info.positions)}",
                    f"• Частота: {_seconds_range(interval_min, interval_max)}",
                    f"• Текущая: {_seconds_value(getattr(heartbeat, 'current_delay_seconds', None))}",
                    "",
                ]
            )
        lines.extend(
            [
                "Настройки конкретной позиции меняются в карточке позиции.",
                SEPARATOR,
            ]
        )
        return "\n".join(lines)

    high_budget = _request_budget(request_limit, high_percent)
    normal_budget = _request_budget(request_limit, normal_percent)
    return "\n".join(
        [
            SEPARATOR,
            "⚙️ Общие настройки",
            "",
            "💰 Изменение цен:",
            _price_change_state_line(
                dry_run=dry_run,
                real_price_writes_enabled=real_price_writes_enabled,
                endpoint_configured=price_write_endpoint_configured,
            ),
            f"🚦 Общий лимит: {request_limit}/мин",
            "",
            "⚡ Распределение запросов",
            f"• High: {high_percent}% = {high_budget}/мин",
            f"• Normal: {normal_percent}% = {normal_budget}/мин",
            "",
            "Настройки конкретной позиции меняются "
            "в ее карточке.",
            SEPARATOR,
        ]
    )


def format_status(
    *,
    worker_state: WorkerState | None,
    dry_run: bool,
    request_usage: int,
    request_limit: int,
    high_percent: int,
    normal_percent: int,
    real_price_writes_enabled: bool = False,
    price_write_endpoint_configured: bool = False,
    latest_price_update: tuple[PriceUpdateLog, Position | None] | None = None,
    latest_price_write_error: tuple[PriceUpdateLog, Position | None] | None = None,
    high_count: int,
    normal_count: int,
    success_count: int,
    error_count: int,
    recent_errors: list[tuple[PriceUpdateLog, Position | None]],
    last_position: Position | None = None,
) -> str:
    now = datetime.now(UTC)
    heartbeat = worker_state.last_heartbeat_at if worker_state else None
    is_running = bool(heartbeat and (now - heartbeat).total_seconds() <= 90)
    high_budget = _request_budget(request_limit, high_percent)
    normal_budget = _request_budget(request_limit, normal_percent)
    high_frequency = format_priority_frequency(
        priority=PriorityLevel.HIGH.value,
        request_limit=request_limit,
        high_percent=high_percent,
        normal_percent=normal_percent,
        high_count=high_count,
        normal_count=normal_count,
    )
    normal_frequency = format_priority_frequency(
        priority=PriorityLevel.NORMAL.value,
        request_limit=request_limit,
        high_percent=high_percent,
        normal_percent=normal_percent,
        high_count=high_count,
        normal_count=normal_count,
    )
    last_status = (
        _status_label(worker_state.last_status)
        if worker_state and worker_state.last_status
        else "—"
    )
    last_error = (
        _reason_label(_reason_key(worker_state.last_error), worker_state.last_error)
        if worker_state and worker_state.last_error
        else "нет"
    )

    lines = [
        SEPARATOR,
        "📊 Статус репрайсера",
        "",
        f"🤖 Worker: {'работает' if is_running else 'не отвечает'}",
        f"🚦 Запросы: {request_usage}/{request_limit} за текущую минуту",
        "",
        *_price_write_status_lines(
            dry_run=dry_run,
            real_price_writes_enabled=real_price_writes_enabled,
            endpoint_configured=price_write_endpoint_configured,
            latest_price_update=latest_price_update,
            latest_price_write_error=latest_price_write_error,
        ),
        "",
        "⚡ Бюджет приоритетов",
        f"• Общий лимит: {request_limit}/мин",
        f"• High: {high_budget}/мин, позиций: {high_count}",
        f"• Normal: {normal_budget}/мин, позиций: {normal_count}",
        f"• High-позиция: {high_frequency}",
        f"• Normal-позиция: {normal_frequency}",
        "",
        "📌 Последний цикл",
        f"• Время: {dt(worker_state.last_cycle_at if worker_state else None)}",
        f"• Позиция: {_last_position_text(worker_state, last_position)}",
        f"• Статус: {last_status}",
        f"• Ошибка: {last_error}",
        "",
        "📈 Итоги",
        f"• Успешно: {success_count}",
        f"• Ошибок: {error_count}",
    ]

    if recent_errors:
        lines.extend(["", "🧯 Последние ошибки:"])
        for log, position in recent_errors:
            position_text = (
                f"{position.robux_amount} робуксов, ID {_lot_id_text(position)}"
                if position
                else f"позиция #{log.position_id}"
            )
            lines.append(
                f"• {dt(log.created_at)} · {position_text}: "
                f"{_reason_label(_reason_key(log.reason), log.reason)}"
            )
    else:
        lines.extend(["", "🧯 Последние ошибки: нет"])

    lines.append(SEPARATOR)
    return "\n".join(lines)


def format_proxy_status(
    *,
    worker_states: list[WorkerState],
    heartbeats: list[WorkerHeartbeat],
    dry_run: bool,
    real_price_writes_enabled: bool = False,
    price_write_endpoint_configured: bool = False,
    latest_price_update: tuple[PriceUpdateLog, Position | None] | None = None,
    latest_price_write_error: tuple[PriceUpdateLog, Position | None] | None = None,
    request_usage: int,
    global_limit: int,
    group_infos: list[WorkerGroupInfo],
    success_count: int,
    error_count: int,
    recent_errors: list[tuple[PriceUpdateLog, Position | None]],
    last_positions_by_amount: dict[int, Position],
) -> str:
    now = datetime.now(UTC)
    heartbeat_by_group = {heartbeat.worker_group: heartbeat for heartbeat in heartbeats}
    active_heartbeats = [
        heartbeat
        for heartbeat in heartbeats
        if (now - heartbeat.last_seen_at).total_seconds() <= 90
    ]
    latest_state = _latest_worker_state(worker_states)
    latest_position = (
        last_positions_by_amount.get(latest_state.last_position_amount)
        if latest_state and latest_state.last_position_amount
        else None
    )
    proxy_capacity = sum(info.request_limit_per_minute for info in group_infos)
    account_effective_limit = _account_effective_limit(heartbeats, global_limit)
    account_usage = _account_usage(heartbeats, request_usage)
    account_backoff = _account_backoff_active(heartbeats, account_effective_limit, global_limit)
    account_last_429 = _account_last_429(heartbeats)

    lines = [
        SEPARATOR,
        "📊 Статус репрайсера",
        "",
        f"🤖 Worker: {'активен' if active_heartbeats else 'не отвечает'}",
        "",
        *_price_write_status_lines(
            dry_run=dry_run,
            real_price_writes_enabled=real_price_writes_enabled,
            endpoint_configured=price_write_endpoint_configured,
            latest_price_update=latest_price_update,
            latest_price_write_error=latest_price_write_error,
        ),
        "",
        "🌐 Режим: прокси",
        "",
        "🚦 Proxy capacity:",
        f"{proxy_capacity}/мин",
        "",
        "🧠 Account effective limit:",
        f"{account_effective_limit}/мин",
        "",
        "📡 Текущая нагрузка:",
        *[
            f"{info.icon} {info.label}: {_profile_usage(heartbeat_by_group.get(info.name))}/{_configured_limit(info, heartbeat_by_group.get(info.name))}"
            for info in group_infos
        ],
        "",
        "Итого:",
        f"{account_usage}/{account_effective_limit}",
        "",
        "📉 Backoff:",
        "активен" if account_backoff else "нет",
        "",
        "Last 429:",
        dt(account_last_429),
        "",
        "📊 Умный планировщик",
    ]

    for info in group_infos:
        heartbeat = heartbeat_by_group.get(info.name)
        if heartbeat is None and len(heartbeat_by_group) == 1:
            heartbeat = heartbeat_by_group.get("all")
        is_active = bool(
            heartbeat
            and (now - heartbeat.last_seen_at).total_seconds() <= 90
        )
        configured_limit = _configured_limit(info, heartbeat)
        effective_limit = _effective_limit(info, heartbeat)
        interval_min, interval_max = _heartbeat_interval_range(info, heartbeat)
        lines.extend(
            [
                f"{info.icon} {info.label}",
                f"• Позиции: {_positions_inline(info.positions)}",
                f"• Лимит: {configured_limit}/мин",
                f"• Effective: {effective_limit}/мин",
                f"• Нагрузка: {_profile_usage(heartbeat)}/{configured_limit}",
                f"• Средний интервал: {_seconds_range(interval_min, interval_max)}",
                f"• Текущая: {_seconds_value(getattr(heartbeat, 'current_delay_seconds', None))}",
                f"• Самая активная позиция: {getattr(heartbeat, 'most_active_position_amount', None) or '—'}",
                f"• Backoff: {_backoff_text(info, heartbeat)}",
                f"• Last 429: {_last_429_text(heartbeat)}",
                f"• IP: {heartbeat.public_ip if heartbeat and heartbeat.public_ip else 'нет данных'}",
                f"• Статус: {_server_status(heartbeat, is_active)}",
            ]
        )
        if _is_safe_mode_heartbeat(heartbeat):
            lines.extend(
                [
                    f"• Причина: {_safe_mode_reason(heartbeat)}",
                    f"• Запросов: {account_usage}/{account_effective_limit}",
                    f"• Следующая попытка: {_safe_mode_retry_text(heartbeat)}",
                ]
            )
        if heartbeat and not is_active:
            lines.append(f"• Последний сигнал: {dt(heartbeat.last_seen_at)}")
        lines.append("")

    last_status = (
        _status_label(latest_state.last_status)
        if latest_state and latest_state.last_status
        else "нет данных"
    )
    last_error = (
        _reason_label(_reason_key(latest_state.last_error), latest_state.last_error)
        if latest_state and latest_state.last_error
        else "нет"
    )
    lines.extend(
        [
            "📌 Последний цикл",
            f"• Время: {dt(latest_state.last_cycle_at if latest_state else None)}",
            f"• Последняя позиция: {_last_position_text(latest_state, latest_position)}",
            f"• Последний статус: {last_status}",
            f"• Ошибки: {error_count}",
            f"• Последняя ошибка: {last_error}",
            "",
            "📈 Итоги",
            f"• Успешно: {success_count}",
            f"• Ошибок: {error_count}",
        ]
    )

    if recent_errors:
        lines.extend(["", "🧯 Последние ошибки:"])
        for log, position in recent_errors:
            position_text = (
                f"{position.robux_amount} робуксов, ID {_lot_id_text(position)}"
                if position
                else f"позиция #{log.position_id}"
            )
            group_line = ""
            if position:
                info = _group_info_for_position(group_infos, position.robux_amount)
                if info:
                    group_line = f" · {info.label}"
            lines.append(
                f"• {dt(log.created_at)} · {position_text}{group_line}: "
                f"{_reason_label(_reason_key(log.reason), log.reason)}"
            )
    else:
        lines.extend(["", "🧯 Последние ошибки: нет"])

    lines.append(SEPARATOR)
    return "\n".join(lines)


def format_proxy_profiles(
    *,
    group_infos: list[WorkerGroupInfo],
    heartbeats: list[WorkerHeartbeat],
    dry_run: bool,
    real_price_writes_enabled: bool = False,
    price_write_endpoint_configured: bool = False,
    global_limit: int,
    proxy_mode: str = "enabled",
) -> str:
    now = datetime.now(UTC)
    heartbeat_by_group = {heartbeat.worker_group: heartbeat for heartbeat in heartbeats}
    safe_mode = any(heartbeat.safe_mode for heartbeat in heartbeats)
    proxy_capacity = sum(info.request_limit_per_minute for info in group_infos)
    account_effective_limit = _account_effective_limit(heartbeats, global_limit)
    account_usage = _account_usage(heartbeats, 0)
    lines = [
        SEPARATOR,
        "📊 Прокси и лимиты",
        f"Режим: {'прокси' if _use_proxy_ui(proxy_mode) else 'напрямую'}",
        "",
        f"Proxy capacity: {proxy_capacity}/мин",
        f"Account effective limit: {account_effective_limit}/мин",
        f"Текущая нагрузка: {account_usage}/{account_effective_limit}",
        f"Backoff: {'активен' if _account_backoff_active(heartbeats, account_effective_limit, global_limit) else 'нет'}",
        f"Last 429: {dt(_account_last_429(heartbeats))}",
        "",
    ]

    for info in group_infos:
        heartbeat = heartbeat_by_group.get(info.name) or heartbeat_by_group.get("all")
        is_active = bool(
            heartbeat
            and (now - heartbeat.last_seen_at).total_seconds() <= 90
        )
        configured_limit = _configured_limit(info, heartbeat)
        effective_limit = _effective_limit(info, heartbeat)
        interval_min, interval_max = _heartbeat_interval_range(info, heartbeat)
        lines.extend(
            [
                f"{info.icon} {info.label}",
                _positions_inline(info.positions),
                f"Configured limit: {configured_limit}/мин",
                f"Effective limit: {effective_limit}/мин",
                f"Нагрузка: {_profile_usage(heartbeat)}/{configured_limit}",
                f"Частота: {_seconds_range(interval_min, interval_max)}",
                f"Текущая: {_seconds_value(getattr(heartbeat, 'current_delay_seconds', None))}",
                f"Backoff: {_backoff_text(info, heartbeat)}",
                f"Last 429: {_last_429_text(heartbeat)}",
                f"IP: {heartbeat.public_ip if heartbeat and heartbeat.public_ip else 'нет данных'}",
                f"Статус: {_server_status(heartbeat, is_active)}",
                f"Последний сигнал: {dt(heartbeat.last_seen_at if heartbeat else None)}",
                "",
            ]
        )

    lines.extend(
        [
            f"Общий лимит: {global_limit}/мин",
            "Изменение цен:",
            _price_change_state_line(
                dry_run=dry_run,
                real_price_writes_enabled=real_price_writes_enabled,
                endpoint_configured=price_write_endpoint_configured,
            ),
            f"Safe mode: {'включен' if safe_mode else 'выключен'}",
            SEPARATOR,
        ]
    )
    return "\n".join(lines)


def format_worker_servers(
    *,
    group_infos: list[WorkerGroupInfo],
    heartbeats: list[WorkerHeartbeat],
    dry_run: bool,
    global_limit: int,
) -> str:
    return format_proxy_profiles(
        group_infos=group_infos,
        heartbeats=heartbeats,
        dry_run=dry_run,
        real_price_writes_enabled=False,
        price_write_endpoint_configured=False,
        global_limit=global_limit,
    )


def format_logs(
    logs: list[tuple[Position, PriceUpdateLog | None]],
    *,
    proxy_mode: str = "disabled",
    group_infos: list[WorkerGroupInfo] | None = None,
    heartbeats: list[WorkerHeartbeat] | None = None,
    schedule_states: list[PositionScheduleState] | None = None,
) -> str:
    if not logs:
        return "Логов действий пока нет."

    lines = [SEPARATOR, "📝 Последние действия", ""]
    for position, log in logs:
        lines.extend(
            [
                f"📦 {position.robux_amount} робуксов · "
                f"🆔 ID: {_lot_id_text(position)}",
            ]
        )
        proxy_lines = _position_proxy_context_lines(
            position,
            group_infos=group_infos,
            heartbeats=heartbeats,
            proxy_mode=proxy_mode,
            group_label="Группа",
            include_ip=False,
            include_frequency=False,
            schedule_states=schedule_states,
        )
        lines.extend(proxy_lines)

        if log is None:
            lines.extend(["📌 Статус: еще не проверялась", SEPARATOR])
            continue

        reason_key = _reason_key(log.reason)
        inner_reason = _dry_run_inner_reason(log.reason)
        display_reason_key = inner_reason or reason_key
        display_reason = inner_reason or log.reason
        if log.status == UpdateStatus.DRY_RUN.value:
            lines.append("💰 Изменение цен: только анализ")
        lines.extend(
            [
                f"📌 Статус: {_status_label(log.status)}",
                f"Причина: {_reason_label(display_reason_key, display_reason)}",
            ]
        )
        price_parts = [
            f"💰 Моя: {money(log.old_price)}",
            f"🏆 Конкурент: {money(log.competitor_price)}",
            f"📉 Расчетная: {money(log.new_price)}",
        ]
        lines.append(" · ".join(price_parts))
        hint = _check_hint(display_reason_key, log.status)
        if hint:
            lines.append(f"Что проверить: {hint}")
        lines.append(f"🕒 Время: {dt(log.created_at)}")
        lines.append(SEPARATOR)
    return "\n".join(lines)


def _status_label(status: str | None) -> str:
    if not status:
        return "—"
    return STATUS_LABELS.get(status, _humanize_code(status))


def _reason_key(reason: str | None) -> str:
    if not reason:
        return ""
    if reason.startswith("dry_run_would_update:"):
        return "dry_run"
    normalized = reason.lower()
    if "429" in normalized or "too many requests" in normalized:
        return "rate_limited"
    if "403" in normalized:
        return "forbidden"
    if "401" in normalized:
        return "unauthorized"
    if "timeout" in normalized or "таймаут" in normalized:
        return "timeout"
    return reason


def _dry_run_inner_reason(reason: str | None) -> str | None:
    if not reason or not reason.startswith("dry_run_would_update:"):
        return None
    inner = reason.split(":", maxsplit=1)[1]
    return inner or None


def _reason_label(reason_key: str | None, raw_reason: str | None) -> str:
    if not raw_reason and not reason_key:
        return MISSING_REASON_TEXT
    if reason_key in REASON_LABELS:
        return REASON_LABELS[reason_key]
    return _humanize_code(raw_reason or reason_key or "")


def _ignore_reason_label(reason: str | None) -> str:
    if not reason:
        return "не указано"
    return IGNORE_REASON_LABELS.get(reason, _humanize_code(reason))


def _check_hint(reason_key: str, status: str) -> str | None:
    if reason_key in CHECK_HINTS:
        return CHECK_HINTS[reason_key]
    if reason_key == "dry_run":
        return "включить реальные изменения и настроить endpoint записи"
    if reason_key == "price_update_endpoint_missing":
        return "MARKET_UPDATE_LOT_PRICE_URL и payload изменения цены"
    if reason_key == "real_price_writes_disabled":
        return "ENABLE_REAL_PRICE_WRITES и режим изменения цен"
    if reason_key == "price_update_payload_unknown":
        return "payload в DevTools -> Network при сохранении цены"
    if status in {"failed", "error"}:
        return "логи worker и настройки Starvell"
    return None


def _price_change_state_line(
    *,
    dry_run: bool,
    real_price_writes_enabled: bool = False,
    endpoint_configured: bool = False,
) -> str:
    if dry_run:
        return "❌ только анализ"
    if real_price_writes_enabled and endpoint_configured:
        return "✅ активно"
    return "❌ только анализ"


def _price_write_status_lines(
    *,
    dry_run: bool,
    real_price_writes_enabled: bool,
    endpoint_configured: bool,
    latest_price_update: tuple[PriceUpdateLog, Position | None] | None,
    latest_price_write_error: tuple[PriceUpdateLog, Position | None] | None,
) -> list[str]:
    ready = (not dry_run) and real_price_writes_enabled and endpoint_configured
    if dry_run:
        status = "заблокировано"
        reason = "включен режим только анализа"
    elif not real_price_writes_enabled:
        status = "заблокировано"
        reason = "ENABLE_REAL_PRICE_WRITES=false"
    elif not endpoint_configured:
        status = "заблокировано"
        reason = "endpoint изменения цены не настроен"
    else:
        status = "готов"
        reason = "все условия выполнены"

    return [
        "💰 Изменение цен",
        f"• Режим: {'реальные изменения' if ready else 'только анализ'}",
        f"• Endpoint: {'настроен' if endpoint_configured else 'отсутствует'}",
        f"• Статус: {status}",
        f"• Причина: {reason}",
        f"• Последнее изменение: {_price_log_summary(latest_price_update)}",
        f"• Последняя ошибка записи: {_price_log_summary(latest_price_write_error)}",
    ]


def _price_log_summary(item: tuple[PriceUpdateLog, Position | None] | None) -> str:
    if item is None:
        return "—"
    log, position = item
    position_text = (
        f"{position.robux_amount} робуксов, ID {_lot_id_text(position)}"
        if position
        else f"позиция #{log.position_id}"
    )
    price_text = money(log.new_price)
    reason = _reason_label(_reason_key(log.reason), log.reason) if log.reason else ""
    if reason:
        return f"{dt(log.created_at)} · {position_text} · {price_text} · {reason}"
    return f"{dt(log.created_at)} · {position_text} · {price_text}"


def _request_budget(request_limit: int, percent: int) -> int:
    return round(request_limit * percent / 100)


def _duration(seconds: float) -> str:
    if seconds < 60:
        if seconds < 10:
            return f"{max(seconds, 0.1):.1f} сек"
        return f"{max(round(seconds), 1)} сек"
    minutes = seconds / 60
    if minutes < 10:
        return f"{minutes:.1f} мин"
    return f"{round(minutes)} мин"


def _lot_id_text(position: Position) -> str:
    return position.lot_id or "не указан"


def _snapshot_offer_id(item: CompetitorSnapshot) -> str | None:
    raw_payload = item.raw_payload or {}
    for key in ("id", "lot_id", "lotId", "listing_id", "listingId", "offer_id", "offerId"):
        value = raw_payload.get(key)
        if value is not None:
            return str(value)
    return None


def _last_position_text(worker_state: WorkerState | None, last_position: Position | None) -> str:
    if last_position is not None:
        return f"{last_position.robux_amount} робуксов, ID {_lot_id_text(last_position)}"
    if worker_state and worker_state.last_position_amount:
        return f"{worker_state.last_position_amount} робуксов"
    return "—"


def _humanize_code(value: str) -> str:
    value = " ".join(value.replace("_", " ").split()).strip()
    return value or "—"


def _position_lines(positions: tuple[int, ...]) -> list[str]:
    return [str(amount) for amount in positions] if positions else ["нет позиций"]


def _positions_inline(positions: tuple[int, ...] | list[int]) -> str:
    return " · ".join(str(amount) for amount in positions) if positions else "нет позиций"


def _server_frequency(request_limit: int, position_count: int) -> str:
    if request_limit <= 0 or position_count <= 0:
        return "нет данных"
    seconds = 60 * position_count / request_limit
    return f"~{_duration(seconds)}"


def _seconds_value(value: float | Decimal | None) -> str:
    if value is None:
        return "—"
    return f"{float(value):.1f} сек"


def _seconds_range(min_seconds: float | None, max_seconds: float | None) -> str:
    if min_seconds is None or max_seconds is None:
        return "динамическая"
    return f"{float(min_seconds):.1f}–{float(max_seconds):.1f} сек"


def _profile_usage(heartbeat: WorkerHeartbeat | None) -> int:
    return int(getattr(heartbeat, "profile_request_usage_per_minute", 0) or 0)


def _account_effective_limit(heartbeats: list[WorkerHeartbeat], default: int) -> int:
    values = [
        int(value)
        for heartbeat in heartbeats
        if (value := getattr(heartbeat, "account_effective_limit_per_minute", None))
    ]
    return min(values) if values else default


def _account_usage(heartbeats: list[WorkerHeartbeat], fallback: int) -> int:
    values = [
        int(getattr(heartbeat, "account_request_usage_per_minute", 0) or 0)
        for heartbeat in heartbeats
    ]
    return max(values) if values else fallback


def _account_backoff_active(
    heartbeats: list[WorkerHeartbeat],
    effective_limit: int,
    configured_limit: int,
) -> bool:
    return bool(
        effective_limit < configured_limit
        or any(getattr(heartbeat, "account_backoff_active", False) for heartbeat in heartbeats)
    )


def _account_last_429(heartbeats: list[WorkerHeartbeat]) -> datetime | None:
    values = [
        value
        for heartbeat in heartbeats
        if (value := getattr(heartbeat, "account_last_429_at", None)) is not None
    ]
    return max(values) if values else None


def _heartbeat_interval_range(
    info: WorkerGroupInfo,
    heartbeat: WorkerHeartbeat | None,
) -> tuple[float | None, float | None]:
    min_value = getattr(heartbeat, "interval_min_seconds", None)
    max_value = getattr(heartbeat, "interval_max_seconds", None)
    if min_value is not None and max_value is not None:
        return float(min_value), float(max_value)
    return display_interval_range(info.name, backoff_active=_backoff_active(info, heartbeat))


def _configured_limit(
    info: WorkerGroupInfo,
    heartbeat: WorkerHeartbeat | None,
) -> int:
    if heartbeat and heartbeat.request_limit_per_minute:
        return heartbeat.request_limit_per_minute
    return info.request_limit_per_minute


def _effective_limit(
    info: WorkerGroupInfo,
    heartbeat: WorkerHeartbeat | None,
) -> int:
    value = getattr(heartbeat, "effective_request_limit_per_minute", None)
    if value:
        return int(value)
    return _configured_limit(info, heartbeat)


def _backoff_text(
    info: WorkerGroupInfo,
    heartbeat: WorkerHeartbeat | None,
) -> str:
    return "активен" if _backoff_active(info, heartbeat) else "нет"


def _backoff_active(
    info: WorkerGroupInfo,
    heartbeat: WorkerHeartbeat | None,
) -> bool:
    if heartbeat is None:
        return False
    return bool(
        getattr(heartbeat, "backoff_active", False)
        or _is_safe_mode_heartbeat(heartbeat)
        or _effective_limit(info, heartbeat) < _configured_limit(info, heartbeat)
    )


def _last_429_text(heartbeat: WorkerHeartbeat | None) -> str:
    return dt(getattr(heartbeat, "last_429_at", None))


def _server_status(heartbeat: WorkerHeartbeat | None, is_active: bool) -> str:
    if heartbeat is None:
        return "⚪ нет сигнала"
    if _is_safe_mode_heartbeat(heartbeat):
        return "🟡 Safe mode активен"
    return "✅ активен" if is_active else "⚠️ не отвечает"


def _is_safe_mode_heartbeat(heartbeat: WorkerHeartbeat | None) -> bool:
    return bool(heartbeat and (heartbeat.safe_mode or heartbeat.status.startswith("safe_mode")))


def _safe_mode_reason(heartbeat: WorkerHeartbeat | None) -> str:
    status = heartbeat.status if heartbeat else ""
    if "429" in status or (heartbeat and heartbeat.errors_429 > 0):
        return "Starvell временно ограничил частоту запросов"
    if "403" in status or (heartbeat and heartbeat.errors_403 > 0):
        return "доступ временно запрещен"
    if "timeout" in status or (heartbeat and heartbeat.errors_timeout > 0):
        return "сайт слишком долго отвечает"
    return "временная защита от частых запросов"


def _safe_mode_retry_text(heartbeat: WorkerHeartbeat | None) -> str:
    if heartbeat is None:
        return "нет данных"
    delay = _adaptive_retry_seconds(heartbeat.consecutive_errors)
    elapsed = (datetime.now(UTC) - heartbeat.last_seen_at).total_seconds()
    remaining = max(delay - elapsed, 0.0)
    if remaining <= 0.1:
        return "сейчас"
    return f"через {_duration(remaining)}"


def _adaptive_retry_seconds(consecutive_errors: int) -> float:
    if consecutive_errors <= 0:
        return 0.0
    steps = (1.0, 2.0, 4.0, 8.0, 15.0)
    return steps[min(consecutive_errors, len(steps)) - 1]


def _use_proxy_ui(proxy_mode: str) -> bool:
    return proxy_mode == "enabled"


def _group_info_for_position(
    group_infos: list[WorkerGroupInfo] | None,
    amount: int,
) -> WorkerGroupInfo | None:
    if not group_infos:
        return None
    for info in group_infos:
        if amount in info.positions:
            return info
    return None


def _heartbeat_for_group(
    info: WorkerGroupInfo | None,
    heartbeats: list[WorkerHeartbeat] | None,
) -> WorkerHeartbeat | None:
    if info is None or not heartbeats:
        return None
    heartbeat_by_group = {heartbeat.worker_group: heartbeat for heartbeat in heartbeats}
    return heartbeat_by_group.get(info.name) or heartbeat_by_group.get("all")


def _position_proxy_context_lines(
    position: Position,
    *,
    group_infos: list[WorkerGroupInfo] | None,
    heartbeats: list[WorkerHeartbeat] | None,
    schedule_states: list[PositionScheduleState] | None,
    proxy_mode: str,
    group_label: str,
    include_ip: bool,
    include_frequency: bool,
) -> list[str]:
    if not _use_proxy_ui(proxy_mode):
        return []
    info = _group_info_for_position(group_infos, position.robux_amount)
    if info is None:
        return ["🌐 Группа: не назначена"]

    heartbeat = _heartbeat_for_group(info, heartbeats)
    schedule_state = _schedule_for_position(schedule_states, position.robux_amount)
    lines = [f"🌐 {group_label}: {info.label}"]
    if include_ip:
        lines.append(
            f"🌍 IP: {heartbeat.public_ip if heartbeat and heartbeat.public_ip else 'нет данных'}"
        )
    if include_frequency:
        interval_min, interval_max = _heartbeat_interval_range(info, heartbeat)
        if schedule_state is not None:
            lines.append(f"🧠 Активность рынка: {market_activity_label(schedule_state.change_score)}")
            lines.append(f"⏱ Частота: {_seconds_range(interval_min, interval_max)}")
            lines.append(f"• Текущая: {_seconds_value(schedule_state.current_interval_seconds)}")
        else:
            lines.append(f"⏱ Частота: {_seconds_range(interval_min, interval_max)}")
            lines.append(f"• Текущая: {_seconds_value(getattr(heartbeat, 'current_delay_seconds', None))}")
    return lines


def _schedule_for_position(
    schedule_states: list[PositionScheduleState] | None,
    amount: int,
) -> PositionScheduleState | None:
    if not schedule_states:
        return None
    for state in schedule_states:
        if state.position_amount == amount:
            return state
    return None


def _latest_worker_state(worker_states: list[WorkerState]) -> WorkerState | None:
    states_with_time = [
        state
        for state in worker_states
        if state.last_cycle_at is not None
    ]
    if not states_with_time:
        return None
    return max(states_with_time, key=lambda state: state.last_cycle_at)
