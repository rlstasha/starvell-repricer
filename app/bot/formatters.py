from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from app.db.models import (
    CompetitorSnapshot,
    Position,
    PriceUpdateLog,
    PriorityLevel,
    WorkerHeartbeat,
    WorkerState,
)
from app.repricer.worker_groups import WorkerGroupInfo


REQUESTS_PER_POSITION_CHECK = 1
LOCAL_TIMEZONE = ZoneInfo("Europe/Moscow")
MONEY_QUANT = Decimal("0.01")
SEPARATOR = "━━━━━━━━━━━━"

STATUS_LABELS = {
    "success": "успешно",
    "updated": "обновлено",
    "running": "выполняется",
    "idle": "ожидает",
    "dry_run": "тестовый режим",
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
    "dry_run": "Тестовый режим. Цена не изменена.",
    "updated": "Цена обновлена.",
    "skipped": "Позиция пропущена.",
    "error": "Произошла ошибка.",
    "missing_lot_id": "Не найден ID лота.",
    "unauthorized": "Ошибка авторизации Starvell.",
    "rate_limited": "Сайт ограничил частоту запросов.",
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
    "missing_lot_id": "указать ID лота в карточке позиции",
    "unauthorized": "MARKET_SESSION_COOKIE и права аккаунта Starvell",
    "rate_limited": "лимит запросов и паузу между проверками",
}


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


def priority_label(value: str) -> str:
    return "высокий" if value == PriorityLevel.HIGH.value else "обычный"


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
    mode = "включен" if dry_run else "выключен"
    safety = (
        "реальные цены не меняются"
        if dry_run
        else "режим реальных изменений"
    )
    return "\n".join(
        [
            SEPARATOR,
            "🤖 Starvell Repricer",
            "",
            f"🧪 Dry-run: {mode}",
            f"📌 Сейчас: {safety}",
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
) -> str:
    settings = position.settings
    state = position.state
    lot_id = _lot_id_text(position)
    enabled = "включена" if position.enabled else "выключена"
    frequency = format_priority_frequency(
        priority=position.priority,
        enabled=position.enabled,
        request_limit=request_limit,
        high_percent=high_percent,
        normal_percent=normal_percent,
        high_count=high_count,
        normal_count=normal_count,
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
        "",
        "⚡ Приоритет",
        f"• Уровень: {priority_label(position.priority)}",
        f"• Частота проверки: {frequency}",
        "",
        "📌 Состояние",
        f"• Работа: {enabled}",
        f"• Последнее обновление: "
        f"{dt(state.last_update_time if state else None)}",
    ]

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


def format_competitors(position: Position, competitors: list[CompetitorSnapshot]) -> str:
    lines = [
        SEPARATOR,
        "📊 Конкуренты",
        f"📦 Позиция: {position.robux_amount} робуксов",
        f"🆔 ID лота: {_lot_id_text(position)}",
        "",
    ]
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
) -> str:
    mode = "включен" if dry_run else "выключен"
    high_budget = _request_budget(request_limit, high_percent)
    normal_budget = _request_budget(request_limit, normal_percent)
    return "\n".join(
        [
            SEPARATOR,
            "⚙️ Общие настройки",
            "",
            f"🧪 Dry-run: {mode}",
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
        else "—"
    )

    lines = [
        SEPARATOR,
        "📊 Статус репрайсера",
        "",
        f"🤖 Worker: {'работает' if is_running else 'не отвечает'}",
        f"🧪 Dry-run: {'включен' if dry_run else 'выключен'}",
        f"🚦 Запросы: {request_usage}/{request_limit} за текущую минуту",
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


def format_worker_servers(
    *,
    group_infos: list[WorkerGroupInfo],
    heartbeats: list[WorkerHeartbeat],
    dry_run: bool,
    global_limit: int,
) -> str:
    now = datetime.now(UTC)
    heartbeat_by_group = {heartbeat.worker_group: heartbeat for heartbeat in heartbeats}
    safe_mode = any(heartbeat.safe_mode for heartbeat in heartbeats)
    lines = [
        SEPARATOR,
        "📊 Серверы и лимиты",
        "",
    ]

    for info in group_infos:
        heartbeat = heartbeat_by_group.get(info.name)
        is_active = bool(
            heartbeat
            and (now - heartbeat.last_seen_at).total_seconds() <= 90
        )
        lines.extend(
            [
                f"{info.icon} {info.label}",
                "",
                "Позиции:",
                *_position_lines(info.positions),
                "",
                "Лимит:",
                f"{info.request_limit_per_minute}/мин",
                "",
                "Частота:",
                _server_frequency(info.request_limit_per_minute, len(info.positions)),
                "",
                "IP:",
                heartbeat.public_ip if heartbeat and heartbeat.public_ip else "нет данных",
                "",
                "Статус:",
                _server_status(heartbeat, is_active),
                "",
                "Последний сигнал:",
                dt(heartbeat.last_seen_at if heartbeat else None),
                "",
            ]
        )

    lines.extend(
        [
            "Общий лимит:",
            f"{global_limit}/мин",
            "",
            "Dry-run:",
            "включен" if dry_run else "выключен",
            "",
            "Safe mode:",
            "включен" if safe_mode else "выключен",
            SEPARATOR,
        ]
    )
    return "\n".join(lines)


def format_logs(logs: list[tuple[Position, PriceUpdateLog | None]]) -> str:
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

        if log is None:
            lines.extend(["📌 Статус: еще не проверялась", SEPARATOR])
            continue

        reason_key = _reason_key(log.reason)
        lines.extend(
            [
                f"🕒 Время: {dt(log.created_at)}",
                f"📌 Статус: {_status_label(log.status)}",
                f"Причина: {_reason_label(reason_key, log.reason)}",
            ]
        )
        inner_reason = _dry_run_inner_reason(log.reason)
        if inner_reason:
            lines.append(f"Расчет: {_reason_label(inner_reason, inner_reason)}")
        lines.append(
            f"💰 Моя: {money(log.old_price)} · "
            f"🏆 Конкурент: {money(log.competitor_price)} · "
            f"📉 Новая: {money(log.new_price)}"
        )
        hint = _check_hint(reason_key, log.status)
        if hint:
            lines.append(f"Что проверить: {hint}")
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
    return reason


def _dry_run_inner_reason(reason: str | None) -> str | None:
    if not reason or not reason.startswith("dry_run_would_update:"):
        return None
    inner = reason.split(":", maxsplit=1)[1]
    return inner or None


def _reason_label(reason_key: str | None, raw_reason: str | None) -> str:
    if not raw_reason and not reason_key:
        return "—"
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
        return "DRY_RUN=true, поэтому реальные цены не меняются"
    if status in {"failed", "error"}:
        return "логи worker и настройки Starvell"
    return None


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


def _server_frequency(request_limit: int, position_count: int) -> str:
    if request_limit <= 0 or position_count <= 0:
        return "нет данных"
    seconds = 60 * position_count / request_limit
    return f"~{_duration(seconds)}"


def _server_status(heartbeat: WorkerHeartbeat | None, is_active: bool) -> str:
    if heartbeat is None:
        return "нет сигнала"
    if heartbeat.safe_mode:
        return "safe mode"
    return "активен" if is_active else "не отвечает"
