from decimal import Decimal

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.market.exceptions import StarvellNotImplementedError
from app.market.schemas import AccountInfo, MarketOffer, OwnLot, UpdateResult
from app.repricer.rate_limiter import RateLimiter


class StarvellClient:
    """Single boundary for all Starvell/Statvell HTTP access.

    The real API is intentionally not spread across the project. Replace the TODO bodies below
    with actual requests, parsing raw payloads into the dataclasses from app.market.schemas.
    """

    def __init__(
        self,
        settings: Settings,
        rate_limiter: RateLimiter,
        http_client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self.rate_limiter = rate_limiter
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.logger = get_logger(__name__)

    async def __aenter__(self) -> "StarvellClient":
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=self.settings.market_base_url,
                timeout=httpx.Timeout(30.0),
                headers=self._default_headers(),
                cookies=self._default_cookies(),
            )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http_client and self._owns_http_client:
            await self._http_client.aclose()

    def _default_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.settings.market_api_token:
            headers["Authorization"] = f"Bearer {self.settings.market_api_token}"
        if self.settings.market_csrf_token:
            headers["X-CSRF-Token"] = self.settings.market_csrf_token
        return headers

    def _default_cookies(self) -> dict[str, str]:
        if not self.settings.market_session_cookie:
            return {}
        return {"session": self.settings.market_session_cookie}

    async def _request(self, method: str, url: str, *, request_type: str, **kwargs) -> httpx.Response:
        if self._http_client is None:
            raise RuntimeError("StarvellClient must be used as an async context manager")
        await self.rate_limiter.acquire()
        response = await self._http_client.request(method, url, **kwargs)
        self.logger.info(
            "starvell_http_request",
            method=method,
            url=url,
            request_type=request_type,
            status_code=response.status_code,
        )
        response.raise_for_status()
        return response

    async def get_market_offers(self, position_amount: int, lot_id: str | None) -> list[MarketOffer]:
        """TODO: fetch market offers for a robux package.

        Expected replacement shape:
        - call Starvell/Statvell offers endpoint through self._request(...);
        - filter/parse only Roblox -> Donat robux -> instant offers for position_amount;
        - return MarketOffer objects with price, seller id/name, rating and active status.
        """
        await self.rate_limiter.acquire()
        self.logger.warning(
            "starvell_get_market_offers_stub",
            position_amount=position_amount,
            lot_id=lot_id,
            todo="implement real Starvell offers request",
        )
        return []

    async def get_my_lot(self, position_amount: int, lot_id: str | None) -> OwnLot | None:
        """TODO: fetch own lot for position_amount and return OwnLot or None."""
        await self.rate_limiter.acquire()
        self.logger.warning(
            "starvell_get_my_lot_stub",
            position_amount=position_amount,
            lot_id=lot_id,
            todo="implement real Starvell own-lot request",
        )
        return None

    async def update_my_lot_price(
        self,
        position_amount: int,
        lot_id: str | None,
        new_price: Decimal,
    ) -> UpdateResult:
        """TODO: update own lot price on Starvell/Statvell.

        The stub raises instead of pretending to update a real lot. In dry-run mode the engine
        never calls this method.
        """
        await self.rate_limiter.acquire()
        self.logger.error(
            "starvell_update_my_lot_price_stub",
            position_amount=position_amount,
            lot_id=lot_id,
            new_price=str(new_price),
            todo="implement real Starvell price update request",
        )
        raise StarvellNotImplementedError("Real Starvell price update is not implemented yet")

    async def get_account_info(self) -> AccountInfo:
        """TODO: fetch account information for the authenticated Starvell account."""
        await self.rate_limiter.acquire()
        self.logger.warning(
            "starvell_get_account_info_stub",
            todo="implement real Starvell account info request",
        )
        return AccountInfo(
            seller_id=self.settings.own_seller_id,
            seller_username=self.settings.own_seller_username,
            raw_payload=None,
        )
