from decimal import Decimal

from app.diagnose_competitors import CompetitorDiagnostic, build_report
from app.db.models import Position
from app.market.client import MarketOffersFetchResult
from app.market.schemas import MarketOffer


def test_competitor_diagnostic_report_includes_counts_and_reasons() -> None:
    position = Position(robux_amount=800, lot_id="2002")
    offer = MarketOffer(
        position_amount=800,
        price=Decimal("551.80"),
        seller_id="132166",
        seller_username="bob2minion",
        rating=Decimal("5"),
        is_active=True,
    )
    own_offer = MarketOffer(
        position_amount=800,
        price=Decimal("552.10"),
        seller_id="4111",
        seller_username="zoomplex",
        rating=Decimal("5"),
        is_active=True,
    )
    diagnostic = CompetitorDiagnostic(
        position=position,
        result=MarketOffersFetchResult(
            position_amount=800,
            lot_id="2002",
            method="POST",
            url="/api/offers/list-by-category",
            source="api_list_by_category",
            raw_offer_count=2,
            offers=[offer, own_offer],
            subcategory_id=69,
        ),
        offers_before_filter=2,
        offers_after_filter=1,
        ignored_reason_counts={"own_seller": 1},
        ignored_examples=[(own_offer, "own_seller")],
        best_offer=offer,
    )

    text = build_report([diagnostic])

    assert "Позиция: 800 робуксов" in text
    assert "lot_id: 2002" in text
    assert "URL: POST /api/offers/list-by-category" in text
    assert "subCategoryId: 69" in text
    assert "Офферов до фильтра: 2" in text
    assert "Офферов после фильтра: 1" in text
    assert "own_seller: 1 — это мой продавец" in text
    assert "MARKET_SESSION_COOKIE" not in text
