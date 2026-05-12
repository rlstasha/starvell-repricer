from decimal import Decimal

from app.db.models import FallbackBehavior
from app.market.schemas import MarketOffer
from app.repricer.price_strategy import PriceCalculationSettings, UndercutByStepStrategy


def offer(price: str) -> MarketOffer:
    return MarketOffer(
        position_amount=400,
        price=Decimal(price),
        seller_id="competitor",
        seller_username="competitor",
        rating=Decimal("5.0"),
        is_active=True,
    )


def settings(**overrides) -> PriceCalculationSettings:
    base = {
        "min_price": Decimal("0"),
        "max_price": Decimal("999999"),
        "step": Decimal("1"),
        "fallback_behavior": FallbackBehavior.KEEP_CURRENT.value,
    }
    base.update(overrides)
    return PriceCalculationSettings(**base)


def test_competitor_price_500_sets_own_price_499() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("500")],
        current_own_price=Decimal("510"),
        settings=settings(),
    )

    assert decision.target_price == Decimal("499.00")
    assert decision.should_update is True


def test_min_price_prevents_falling_below_480() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("470")],
        current_own_price=Decimal("500"),
        settings=settings(min_price=Decimal("480")),
    )

    assert decision.target_price == Decimal("480.00")


def test_max_price_prevents_rising_above_600() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("700")],
        current_own_price=Decimal("500"),
        settings=settings(max_price=Decimal("600")),
    )

    assert decision.target_price == Decimal("600.00")

