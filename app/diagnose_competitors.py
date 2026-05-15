import asyncio
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.models import Position
from app.db.repositories import PositionRepository
from app.db.session import create_session_factory
from app.market.client import MarketOffersFetchResult, StarvellClient
from app.market.schemas import MarketOffer
from app.repricer.competitor_filter import CompetitorFilter, CompetitorFilterSettings
from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


REASON_LABELS = {
    "seller_inactive": "продавец неактивен",
    "own_seller": "это мой продавец",
    "no_rating": "нет рейтинга",
    "rating_too_low": "рейтинг ниже минимума",
    "missing_lot_id": "не указан lot_id",
    "parser_rejected": (
        "ответ Starvell не сопоставлен с этой позицией"
    ),
}


@dataclass(frozen=True)
class CompetitorDiagnostic:
    position: Position
    result: MarketOffersFetchResult | None
    offers_before_filter: int
    offers_after_filter: int
    ignored_reason_counts: dict[str, int]
    ignored_examples: list[tuple[MarketOffer, str]]
    best_offer: MarketOffer | None


async def main() -> int:
    settings = get_settings()
    configure_logging("WARNING")

    session_factory = create_session_factory(settings=settings)
    async with session_factory() as session:
        positions = await PositionRepository(session).list_positions()

    limiter = InMemoryFixedWindowRateLimiter(limit=settings.request_limit_per_minute)
    async with StarvellClient(settings, limiter) as client:
        diagnostics = await diagnose_positions(positions, client=client, settings=settings)

    print(build_report(diagnostics))
    return 0 if _all_known_positions_have_competitors(diagnostics) else 1


async def diagnose_positions(
    positions: list[Position],
    *,
    client: StarvellClient,
    settings: Settings,
) -> list[CompetitorDiagnostic]:
    diagnostics: list[CompetitorDiagnostic] = []
    filter_ = CompetitorFilter()

    for position in positions:
        if not position.lot_id:
            diagnostics.append(
                CompetitorDiagnostic(
                    position=position,
                    result=None,
                    offers_before_filter=0,
                    offers_after_filter=0,
                    ignored_reason_counts={"missing_lot_id": 1},
                    ignored_examples=[],
                    best_offer=None,
                )
            )
            continue

        result = await client.get_market_offers_result(position.robux_amount, position.lot_id)
        filter_settings = CompetitorFilterSettings(
            min_rating=position.settings.min_rating,
            ignore_no_rating=position.settings.ignore_no_rating,
            own_seller_id=settings.own_seller_id,
            own_seller_username=settings.own_seller_username,
        )
        filter_result = filter_.filter(result.offers, filter_settings)
        ignored_reasons: list[tuple[MarketOffer, str]] = []
        for offer in result.offers:
            reason = filter_.ignore_reason(offer, filter_settings)
            if reason:
                ignored_reasons.append((offer, reason))

        reason_counts = Counter(reason for _, reason in ignored_reasons)
        if result.parser_rejected_count:
            reason_counts["parser_rejected"] += result.parser_rejected_count

        diagnostics.append(
            CompetitorDiagnostic(
                position=position,
                result=result,
                offers_before_filter=len(result.offers),
                offers_after_filter=len(filter_result.accepted),
                ignored_reason_counts=dict(reason_counts),
                ignored_examples=ignored_reasons[:5],
                best_offer=filter_result.accepted[0] if filter_result.accepted else None,
            )
        )

    return diagnostics


def build_report(diagnostics: list[CompetitorDiagnostic]) -> str:
    lines = [
        "Диагностика конкурентов Starvell",
        "Режим: только чтение рынка. Цены не меняются.",
        "Cookie/session/token не выводятся.",
        "",
    ]

    for diagnostic in diagnostics:
        position = diagnostic.position
        lines.extend(
            [
                "━━━━━━━━━━━━",
                f"Позиция: {position.robux_amount} робуксов",
                f"lot_id: {position.lot_id or 'не указан'}",
            ]
        )
        if diagnostic.result is None:
            lines.extend(
                [
                    "URL: не запрошен",
                    "Офферов до фильтра: 0",
                    "Офферов после фильтра: 0",
                    "Отброшено: missing_lot_id — не указан lot_id",
                    (
                        "Причина: репрайсер пропускает позицию "
                        "до запроса рынка."
                    ),
                ]
            )
            continue

        result = diagnostic.result
        lines.extend(
            [
                f"URL: {result.method} {result.url}",
                f"subCategoryId: {result.subcategory_id or 'не найден'}",
                f"Сырых офферов в ответе: {result.raw_offer_count}",
                f"Офферов до фильтра: {diagnostic.offers_before_filter}",
                f"Офферов после фильтра: {diagnostic.offers_after_filter}",
            ]
        )

        if diagnostic.best_offer:
            offer = diagnostic.best_offer
            lines.append(
                "Лучший конкурент: "
                f"{offer.seller_username or offer.seller_id or 'неизвестно'} · "
                f"{_money(offer.price)} · рейтинг {offer.rating or '—'}"
            )
        else:
            lines.append("Лучший конкурент: не найден")

        lines.append("Отброшено:")
        if diagnostic.ignored_reason_counts:
            for reason, count in sorted(diagnostic.ignored_reason_counts.items()):
                lines.append(f"- {reason}: {count} — {_reason_label(reason)}")
        else:
            lines.append("- нет")

        if diagnostic.ignored_examples:
            lines.append("Примеры отброшенных:")
            for offer, reason in diagnostic.ignored_examples:
                lines.append(
                    f"- {offer.seller_username or offer.seller_id or 'неизвестно'} · "
                    f"{_money(offer.price)} · {_reason_label(reason)}"
                )

    lines.append("━━━━━━━━━━━━")
    return "\n".join(lines)


def _all_known_positions_have_competitors(diagnostics: list[CompetitorDiagnostic]) -> bool:
    required_amounts = {
        80,
        200,
        400,
        500,
        800,
        1000,
        1200,
        1700,
        2000,
        2100,
        2500,
        3600,
        4500,
        10000,
        22500,
    }
    for diagnostic in diagnostics:
        if diagnostic.position.robux_amount not in required_amounts:
            continue
        if diagnostic.offers_after_filter <= 0:
            return False
    return True


def _reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason.replace("_", " "))


def _money(value: Decimal | None) -> str:
    return f"{value} ₽" if value is not None else "—"


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
