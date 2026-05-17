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
    assert decision.reason == "all_competitors_below_min_price"


def test_max_price_prevents_rising_above_600() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("700")],
        current_own_price=Decimal("500"),
        settings=settings(max_price=Decimal("600")),
    )

    assert decision.target_price == Decimal("600.00")


def test_min_price_bounce_uses_next_competitor_above_min() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("74.90"), offer("90.00")],
        current_own_price=Decimal("75.00"),
        settings=settings(
            min_price=Decimal("75.00"),
            max_price=Decimal("120.00"),
            step=Decimal("0.10"),
        ),
    )

    assert decision.target_price == Decimal("89.90")
    assert decision.competitor_price == Decimal("90.00")
    assert decision.should_update is True
    assert decision.reason == "min_price_bounce_to_upper_competitor"


def test_min_price_bounce_keeps_min_when_all_competitors_below_min() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("70.00"), offer("74.90")],
        current_own_price=Decimal("90.00"),
        settings=settings(
            min_price=Decimal("75.00"),
            max_price=Decimal("120.00"),
            step=Decimal("0.10"),
        ),
    )

    assert decision.target_price == Decimal("75.00")
    assert decision.competitor_price == Decimal("70.00")
    assert decision.should_update is True
    assert decision.reason == "all_competitors_below_min_price"


def test_min_price_bounce_keeps_min_when_upper_competitor_step_hits_min() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("75.05")],
        current_own_price=Decimal("90.00"),
        settings=settings(
            min_price=Decimal("75.00"),
            max_price=Decimal("120.00"),
            step=Decimal("0.10"),
        ),
    )

    assert decision.target_price == Decimal("75.00")
    assert decision.competitor_price == Decimal("75.05")
    assert decision.should_update is True
    assert decision.reason == "competitor_above_min_but_step_hits_min"


def test_regular_undercut_still_works_above_min_price() -> None:
    decision = UndercutByStepStrategy().calculate(
        competitors=[offer("80.00")],
        current_own_price=Decimal("100.00"),
        settings=settings(
            min_price=Decimal("75.00"),
            max_price=Decimal("120.00"),
            step=Decimal("0.10"),
        ),
    )

    assert decision.target_price == Decimal("79.90")
    assert decision.competitor_price == Decimal("80.00")
    assert decision.should_update is True
    assert decision.reason == "competitor_undercut"
