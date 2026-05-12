from decimal import Decimal

from app.market.schemas import MarketOffer
from app.repricer.competitor_filter import CompetitorFilter, CompetitorFilterSettings


def make_offer(
    *,
    seller_id: str = "seller",
    seller_username: str = "seller",
    rating: Decimal | None = Decimal("5.0"),
) -> MarketOffer:
    return MarketOffer(
        position_amount=400,
        price=Decimal("500"),
        seller_id=seller_id,
        seller_username=seller_username,
        rating=rating,
        is_active=True,
    )


def test_seller_with_rating_4_4_is_ignored() -> None:
    result = CompetitorFilter().filter(
        [make_offer(rating=Decimal("4.4"))],
        CompetitorFilterSettings(min_rating=Decimal("4.5")),
    )

    assert result.accepted == []
    assert list(result.ignored_reasons.values()) == ["rating_too_low"]


def test_seller_without_rating_is_ignored() -> None:
    result = CompetitorFilter().filter(
        [make_offer(rating=None)],
        CompetitorFilterSettings(ignore_no_rating=True),
    )

    assert result.accepted == []
    assert list(result.ignored_reasons.values()) == ["no_rating"]


def test_own_seller_is_ignored() -> None:
    result = CompetitorFilter().filter(
        [make_offer(seller_id="own-id", seller_username="strvlBot")],
        CompetitorFilterSettings(
            own_seller_id="own-id",
            own_seller_username="strvlBot",
        ),
    )

    assert result.accepted == []
    assert list(result.ignored_reasons.values()) == ["own_seller"]

