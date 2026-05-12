from dataclasses import dataclass
from decimal import Decimal

from app.market.schemas import MarketOffer


@dataclass(frozen=True)
class CompetitorFilterSettings:
    min_rating: Decimal = Decimal("4.5")
    ignore_no_rating: bool = True
    own_seller_id: str | None = None
    own_seller_username: str | None = None


@dataclass(frozen=True)
class FilterResult:
    accepted: list[MarketOffer]
    ignored_reasons: dict[str, str]


class CompetitorFilter:
    def filter(
        self,
        offers: list[MarketOffer],
        settings: CompetitorFilterSettings,
    ) -> FilterResult:
        accepted: list[MarketOffer] = []
        ignored_reasons: dict[str, str] = {}

        for offer in offers:
            reason = self._ignore_reason(offer, settings)
            if reason:
                ignored_reasons[offer.key] = reason
                continue
            accepted.append(offer)

        accepted.sort(key=lambda item: item.price)
        return FilterResult(accepted=accepted, ignored_reasons=ignored_reasons)

    def _ignore_reason(
        self,
        offer: MarketOffer,
        settings: CompetitorFilterSettings,
    ) -> str | None:
        if offer.is_active is False:
            return "seller_inactive"

        if self._is_own_seller(offer, settings):
            return "own_seller"

        if offer.rating is None and settings.ignore_no_rating:
            return "no_rating"

        if offer.rating is not None and offer.rating < settings.min_rating:
            return "rating_too_low"

        return None

    def _is_own_seller(
        self,
        offer: MarketOffer,
        settings: CompetitorFilterSettings,
    ) -> bool:
        if settings.own_seller_id and offer.seller_id:
            if str(settings.own_seller_id) == str(offer.seller_id):
                return True
        if settings.own_seller_username and offer.seller_username:
            return settings.own_seller_username.casefold() == offer.seller_username.casefold()
        return False

