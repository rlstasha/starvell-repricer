from datetime import UTC, datetime
from decimal import Decimal

from app.db.models import CompetitorSnapshot, Position, PriceUpdateLog, WorkerState


def money(value: Decimal | None) -> str:
    return f"{value} ₽" if value is not None else "—"


def dt(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def yes_no(value: bool) -> str:
    return "да" if value else "нет"


def format_main_menu(*, dry_run: bool) -> str:
    mode = "включен" if dry_run else "выключен"
    return f"Панель управления репрайсером\n\nDry-run: {mode}"


def format_position_card(position: Position) -> str:
    settings = position.settings
    state = position.state
    enabled = "включено" if position.enabled else "выключено"
    return (
        f"⚙️ Настройки позиции\n\n"
        f"Название: {position.robux_amount} робуксов\n"
        f"Категория: Roblox, донат робуксов, моментально\n\n"
        f"Статус: {enabled}\n"
        f"Текущая моя цена: {money(state.current_own_price if state else None)}\n"
        f"Последняя цена конкурента: {money(state.last_seen_competitor_price if state else None)}\n"
        f"Мин. цена: {money(settings.min_price)}\n"
        f"Макс. цена: {money(settings.max_price)}\n"
        f"Шаг: {money(settings.step)}\n"
        f"Мин. рейтинг: {settings.min_rating}\n"
        f"Игнор без рейтинга: {yes_no(settings.ignore_no_rating)}\n"
        f"Приоритет: {position.priority}\n"
        f"Последнее обновление: {dt(state.last_update_time if state else None)}"
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


def format_general_settings(*, dry_run: bool, request_limit: int, high_weight: int, normal_weight: int) -> str:
    mode = "включен" if dry_run else "выключен"
    return (
        "⚙️ Общие настройки\n\n"
        f"Dry-run: {mode}\n"
        f"Лимит запросов: {request_limit} в минуту\n"
        f"Вес high priority: {high_weight}\n"
        f"Вес normal priority: {normal_weight}\n"
        "Остальные настройки позиций меняются в карточке каждой позиции."
    )


def format_status(
    *,
    worker_state: WorkerState | None,
    dry_run: bool,
    request_usage: int,
    request_limit: int,
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
        f"Запросов за текущую минуту: {request_usage}/{request_limit}",
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


def format_logs(logs: list[PriceUpdateLog]) -> str:
    if not logs:
        return "Логов действий пока нет."

    lines = ["📝 Последние действия", ""]
    for log in logs:
        lines.append(
            f"• {dt(log.created_at)} · status={log.status} · "
            f"{money(log.old_price)} -> {money(log.new_price)} · {log.reason or '—'}"
        )
    return "\n".join(lines)

