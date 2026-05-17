from dataclasses import dataclass
from decimal import Decimal

from app.db.models import FallbackBehavior
from app.market.schemas import MarketOffer

MONEY_QUANT = Decimal("0.01")


@dataclass(frozen=True)
class PriceCalculationSettings:
    min_price: Decimal
    max_price: Decimal
    step: Decimal = Decimal("1")
    fallback_behavior: str = FallbackBehavior.KEEP_CURRENT.value


@dataclass(frozen=True)
class PriceDecision:
    target_price: Decimal | None
    competitor: MarketOffer | None
    competitor_price: Decimal | None
    should_update: bool
    reason: str


class UndercutByStepStrategy:
    def calculate(
        self,
        *,
        competitors: list[MarketOffer],
        current_own_price: Decimal | None,
        settings: PriceCalculationSettings,
    ) -> PriceDecision:
        self._validate_settings(settings)
        best_competitor = competitors[0] if competitors else None

        if best_competitor is None:
            return self._fallback_decision(current_own_price=current_own_price, settings=settings)

        raw_target = best_competitor.price - settings.step
        if raw_target < settings.min_price:
            return self._min_price_bounce_decision(
                competitors=competitors,
                current_own_price=current_own_price,
                settings=settings,
            )

        target = self._bounded(raw_target, settings)
        return PriceDecision(
            target_price=target,
            competitor=best_competitor,
            competitor_price=best_competitor.price,
            should_update=current_own_price != target,
            reason="competitor_undercut" if current_own_price != target else "already_at_target",
        )

    def _min_price_bounce_decision(
        self,
        *,
        competitors: list[MarketOffer],
        current_own_price: Decimal | None,
        settings: PriceCalculationSettings,
    ) -> PriceDecision:
        competitor_above_min = next(
            (competitor for competitor in competitors if competitor.price > settings.min_price),
            None,
        )
        if competitor_above_min is None:
            target = self._bounded(settings.min_price, settings)
            return PriceDecision(
                target_price=target,
                competitor=competitors[0],
                competitor_price=competitors[0].price,
                should_update=current_own_price != target,
                reason="all_competitors_below_min_price",
            )

        raw_target = competitor_above_min.price - settings.step
        target = self._bounded(raw_target, settings)
        if raw_target < settings.min_price:
            return PriceDecision(
                target_price=target,
                competitor=competitor_above_min,
                competitor_price=competitor_above_min.price,
                should_update=current_own_price != target,
                reason="competitor_above_min_but_step_hits_min",
            )

        return PriceDecision(
            target_price=target,
            competitor=competitor_above_min,
            competitor_price=competitor_above_min.price,
            should_update=current_own_price != target,
            reason="min_price_bounce_to_upper_competitor",
        )

    def _fallback_decision(
        self,
        *,
        current_own_price: Decimal | None,
        settings: PriceCalculationSettings,
    ) -> PriceDecision:
        if settings.fallback_behavior == FallbackBehavior.SET_MAX_PRICE.value:
            target = self._bounded(settings.max_price, settings)
            return PriceDecision(
                target_price=target,
                competitor=None,
                competitor_price=None,
                should_update=current_own_price != target,
                reason="no_competitors_set_max_price",
            )

        if settings.fallback_behavior != FallbackBehavior.KEEP_CURRENT.value:
            raise ValueError(f"Unsupported fallback behavior: {settings.fallback_behavior}")

        return PriceDecision(
            target_price=current_own_price,
            competitor=None,
            competitor_price=None,
            should_update=False,
            reason="no_competitors_keep_current_price",
        )

    def _bounded(self, value: Decimal, settings: PriceCalculationSettings) -> Decimal:
        return min(max(value, settings.min_price), settings.max_price).quantize(MONEY_QUANT)

    def _validate_settings(self, settings: PriceCalculationSettings) -> None:
        if settings.step <= 0:
            raise ValueError("step must be greater than zero")
        if settings.min_price > settings.max_price:
            raise ValueError("min_price cannot be greater than max_price")
