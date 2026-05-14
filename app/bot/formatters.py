from datetime import UTC, datetime
from decimal import Decimal

from app.db.models import CompetitorSnapshot, Position, PriceUpdateLog, PriorityLevel, WorkerState


REQUESTS_PER_POSITION_CHECK = 2

STATUS_LABELS = {
    "success": "цена обновлена",
    "updated": "цена обновлена",
    "dry_run": "тестовый режим, цена не изменена",
    "skipped": "пропущено",
    "failed": "ошибка",
    "error": "ошибка",
}

REASON_LABELS = {
    "no_target_price": "не удалось рассчитать цену",
    "no_competitors_keep_current_price": "нет подходящих конкурентов, цена оставлена без изменений",
    "no_competitors_set_max_price": "нет подходящих конкурентов, выбрана максимальная цена",
    "competitor_undercut": "найден конкурент, расчетная цена ниже на шаг",
    "already_at_target": "цена уже равна расчетной",
    "missing_lot_id": "не найден ID лота",
    "unauthorized": "ошибка авторизации Starvell",
    "rate_limited": "сайт ограничил частоту запросов",
    "position_disabled": "позиция выключена",
    "position_not_found": "позиция не найдена",
}

CHECK_HINTS = {
    "no_target_price": "подключение Starvell, цену конкурента, мою текущую цену",
    "missing_lot_id": "указать ID лота в карточке позиции",
    "unauthorized": "токен/сессию Starvell и права аккаунта",
    "rate_limited": "лимит запросов и паузу между проверками",
    "no_competitors_keep_current_price": "фильтр рейтинга, список конкурентов, категорию позиции",
    "no_competitors_set_max_price": "фильтр рейтинга, список конкурентов, категорию позиции",
}


def money(value: Decimal | None) -> str:
    return f"{value} ₽" if value is not None else "—"


def dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def yes_no(value: bool) -> str:
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
    return f"Панель управления репрайсером\n\nDry-run: {mode}"


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
    enabled = "включено" if position.enabled else "выключено"
    lot_id = position.lot_id or "не указан"
    missing_lot_text = "" if position.lot_id else "\nНе найден ID лота. Репрайс невозможен."
    frequency = format_priority_frequency(
        priority=position.priority,
        enabled=position.enabled,
        request_limit=request_limit,
        high_percent=high_percent,
        normal_percent=normal_percent,
        high_count=high_count,
        normal_count=normal_count,
    )
    return (
        f"⚙️ Настройки позиции\n\n"
        f"Название: {position.robux_amount} робуксов\n"
        f"ID лота: {lot_id}\n"
        f"Категория: Roblox, донат робуксов, моментально\n\n"
        f"Статус: {enabled}\n"
        f"Текущая моя цена: {money(state.current_own_price if state else None)}\n"
        f"Последняя цена конкурента: {money(state.last_seen_competitor_price if state else None)}\n"
        f"Мин. цена: {money(settings.min_price)}\n"
        f"Макс. цена: {money(settings.max_price)}\n"
        f"Шаг: {money(settings.step)}\n"
        f"Мин. рейтинг: {settings.min_rating}\n"
        f"Игнор без рейтинга: {yes_no(settings.ignore_no_rating)}\n"
        f"Приоритет: {priority_label(position.priority)}\n"
        f"Примерная частота проверки: {frequency}\n"
        f"Последнее обновление: {dt(state.last_update_time if state else None)}"
        f"{missing_lot_text}"
    )


def format_competitors(amount: int, competitors: list[CompetitorSnapshot]) -> str:
    if not competitors:
        return f"По позиции {amount} робуксов пока нет сохраненных конкурентов."

    lines = [f"📊 Последние конкуренты: {amount} робуксов", ""]
    for item in competitors:
        ignored = f" · игнор: {item.ignore_reason}" if item.is_ignored else ""
        lines.append(
            f"• {item.seller_username or item.seller_id or 'неизвестно'}: "
            f"{money(item.price)}, рейтинг {item.rating if item.rating is not None else '—'}, "
            f"активен {item.is_active if item.is_active is not None else '—'}{ignored}"
        )
    return "\n".join(lines)


def format_price_test(
    *,
    position: Position,
    target_price: Decimal | None,
    competitor_price: Decimal | None,
    reason: str,
    should_update: bool,
) -> str:
    action = "цена изменилась бы" if should_update else "изменение не требуется"
    return (
        f"🧪 Тест расчета цены\n\n"
        f"Позиция: {position.robux_amount} робуксов\n"
        f"Текущая моя цена: {money(position.state.current_own_price if position.state else None)}\n"
        f"Цена конкурента: {money(competitor_price)}\n"
        f"Расчетная цена: {money(target_price)}\n"
        f"Результат: {action}\n"
        f"Причина: {reason}"
    )


def format_general_settings(
    *,
    dry_run: bool,
    request_limit: int,
    high_percent: int,
    normal_percent: int,
) -> str:
    mode = "включен" if dry_run else "выключен"
    return (
        "⚙️ Общие настройки\n\n"
        f"Dry-run: {mode}\n"
        f"Лимит запросов: {request_limit} в минуту\n"
        f"High priority: {high_percent}% лимита\n"
        f"Normal priority: {normal_percent}% лимита\n"
        "Остальные настройки позиций меняются в карточке каждой позиции."
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
    recent_errors: list[PriceUpdateLog],
) -> str:
    now = datetime.now(UTC)
    heartbeat = worker_state.last_heartbeat_at if worker_state else None
    is_running = bool(heartbeat and (now - heartbeat).total_seconds() <= 90)
    lines = [
        "📊 Статус репрайсера",
        "",
        f"Работает: {'да' if is_running else 'нет'}",
        f"Dry-run: {'включен' if dry_run else 'выключен'}",
        f"Общий лимит запросов: {request_limit}/мин",
        f"Запросов за текущую минуту: {request_usage}/{request_limit}",
        f"High priority: {_request_budget(request_limit, high_percent)}/мин",
        f"Normal priority: {_request_budget(request_limit, normal_percent)}/мин",
        f"Позиций high: {high_count}",
        f"Позиций normal: {normal_count}",
        "Частота high-позиции: "
        + format_priority_frequency(
            priority=PriorityLevel.HIGH.value,
            request_limit=request_limit,
            high_percent=high_percent,
            normal_percent=normal_percent,
            high_count=high_count,
            normal_count=normal_count,
        ),
        "Частота normal-позиции: "
        + format_priority_frequency(
            priority=PriorityLevel.NORMAL.value,
            request_limit=request_limit,
            high_percent=high_percent,
            normal_percent=normal_percent,
            high_count=high_count,
            normal_count=normal_count,
        ),
        f"Успешных обновлений: {success_count}",
        f"Ошибок: {error_count}",
        f"Последний цикл: {dt(worker_state.last_cycle_at if worker_state else None)}",
        f"Последняя позиция: {worker_state.last_position_amount if worker_state and worker_state.last_position_amount else '—'}",
        f"Последний статус: {worker_state.last_status if worker_state and worker_state.last_status else '—'}",
    ]

    if recent_errors:
        lines.extend(["", "Последние ошибки:"])
        for log in recent_errors:
            lines.append(f"• {dt(log.created_at)} · позиция #{log.position_id}: {log.reason}")
    else:
        lines.extend(["", "Последние ошибки: нет"])

    return "\n".join(lines)


def format_logs(logs: list[tuple[PriceUpdateLog, int | None]]) -> str:
    if not logs:
        return "Логов действий пока нет."

    lines = ["📝 Последние действия", ""]
    for log, amount in logs:
        reason_key = _reason_key(log.reason)
        amount_text = f"{amount} робуксов" if amount is not None else f"позиция #{log.position_id}"
        lines.append(f"{amount_text}: {_status_label(log.status)}")
        lines.append(f"Причина: {_reason_label(reason_key, log.reason)}")
        hint = _check_hint(reason_key, log.status)
        if hint:
            lines.append(f"Что проверить: {hint}")
        lines.append("")
    return "\n".join(lines)


def _status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def _reason_key(reason: str | None) -> str:
    if not reason:
        return ""
    if reason.startswith("dry_run_would_update:"):
        return reason.split(":", maxsplit=1)[1]
    return reason


def _reason_label(reason_key: str, raw_reason: str | None) -> str:
    if raw_reason and raw_reason.startswith("dry_run_would_update:"):
        return "тестовый режим, цена не изменена"
    return REASON_LABELS.get(reason_key, raw_reason or "—")


def _check_hint(reason_key: str, status: str) -> str | None:
    if reason_key in CHECK_HINTS:
        return CHECK_HINTS[reason_key]
    if status in {"failed", "error"}:
        return "логи worker и настройки Starvell"
    return None


def _request_budget(request_limit: int, percent: int) -> int:
    return round(request_limit * percent / 100)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{max(round(seconds), 1)} сек"
    minutes = seconds / 60
    if minutes < 10:
        return f"{minutes:.1f} мин"
    return f"{round(minutes)} мин"
