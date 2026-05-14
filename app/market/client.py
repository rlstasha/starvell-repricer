from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.market.exceptions import StarvellEndpointNotConfiguredError, StarvellNotImplementedError
from app.market.schemas import (
    AccountInfo,
    MarketOffer,
    MyLotSummary,
    OwnLot,
    StarvellConnectionCheck,
    UpdateResult,
)
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

    async def _request(
        self,
        method: str,
        url: str,
        *,
        request_type: str,
        **kwargs,
    ) -> httpx.Response:
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

    async def _get_json(self, url: str, *, request_type: str) -> tuple[Any, int]:
        response = await self._request("GET", url, request_type=request_type)
        try:
            return response.json(), response.status_code
        except ValueError:
            return None, response.status_code

    async def check_connection(self) -> StarvellConnectionCheck:
        """Check safe GET endpoints without guessing Starvell API URLs."""
        account_info: AccountInfo | None = None
        my_lots: list[MyLotSummary] = []
        account_error: str | None = None
        lots_error: str | None = None
        account_status_code: int | None = None
        lots_status_code: int | None = None
        authorized: bool | None = None

        if self.settings.market_account_info_url:
            try:
                account_info = await self.get_account_info()
                account_status_code = 200
                authorized = True
            except httpx.HTTPStatusError as exc:
                account_status_code = exc.response.status_code
                authorized = False if account_status_code in {401, 403} else None
                account_error = explain_http_status(account_status_code)
            except Exception as exc:
                account_error = _safe_error(exc)
        else:
            account_error = "MARKET_ACCOUNT_INFO_URL не настроен"

        if self.settings.market_my_lots_url:
            try:
                my_lots = await self.get_my_lots()
                lots_status_code = 200
                if authorized is None:
                    authorized = True
            except httpx.HTTPStatusError as exc:
                lots_status_code = exc.response.status_code
                lots_error = explain_http_status(lots_status_code)
            except Exception as exc:
                lots_error = _safe_error(exc)
        else:
            lots_error = "MARKET_MY_LOTS_URL не настроен"

        return StarvellConnectionCheck(
            account_endpoint_configured=bool(self.settings.market_account_info_url),
            lots_endpoint_configured=bool(self.settings.market_my_lots_url),
            authorized=authorized,
            account_info=account_info,
            my_lots=my_lots,
            account_status_code=account_status_code,
            lots_status_code=lots_status_code,
            account_error=account_error,
            lots_error=lots_error,
        )

    async def get_market_offers(
        self,
        position_amount: int,
        lot_id: str | None,
    ) -> list[MarketOffer]:
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

    async def get_my_lots(self) -> list[MyLotSummary]:
        """TODO: fetch own active lots through a real safe GET endpoint.

        Configure MARKET_MY_LOTS_URL with the GET URL found in DevTools. The parser below is
        defensive because the real Starvell payload shape is not known yet.
        """
        if not self.settings.market_my_lots_url:
            raise StarvellEndpointNotConfiguredError("MARKET_MY_LOTS_URL is not configured")

        payload, _ = await self._get_json(
            self.settings.market_my_lots_url,
            request_type="my_lots",
        )
        return [_parse_lot(item) for item in _find_dict_items(payload)]

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
        """TODO: fetch account information through the real safe GET endpoint.

        Configure MARKET_ACCOUNT_INFO_URL with the GET URL found in DevTools. The parser below
        only extracts common seller/user keys when the payload shape is known by the endpoint.
        """
        if not self.settings.market_account_info_url:
            raise StarvellEndpointNotConfiguredError("MARKET_ACCOUNT_INFO_URL is not configured")

        payload, _ = await self._get_json(
            self.settings.market_account_info_url,
            request_type="account_info",
        )
        return AccountInfo(
            seller_id=_find_first_string(
                payload,
                ("seller_id", "sellerId", "id", "user_id", "userId"),
            ),
            seller_username=_find_first_string(
                payload,
                ("seller_username", "sellerUsername", "username", "name", "login"),
            ),
            raw_payload=payload if isinstance(payload, dict) else None,
        )


def explain_http_status(status_code: int) -> str:
    if status_code == 401:
        return (
            "401: не авторизовано. Проверьте MARKET_SESSION_COOKIE "
            "или токен Starvell."
        )
    if status_code == 403:
        return (
            "403: доступ запрещен. "
            "Аккаунт вошел, но прав на этот раздел "
            "может не хватать."
        )
    if status_code == 429:
        return (
            "429: сайт ограничил частоту запросов. "
            "Подождите и уменьшите "
            "частоту проверок."
        )
    if status_code >= 500:
        return (
            f"{status_code}: ошибка на стороне Starvell. "
            "Попробуйте позже."
        )
    return f"{status_code}: Starvell вернул неожиданный HTTP-статус."


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, StarvellEndpointNotConfiguredError):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "Таймаут при подключении к Starvell."
    if isinstance(exc, httpx.RequestError):
        return (
            "Не удалось подключиться к Starvell. Проверьте сеть "
            "и MARKET_BASE_URL."
        )
    return f"Ошибка проверки подключения: {type(exc).__name__}"


def _find_first_string(payload: Any, keys: tuple[str, ...]) -> str | None:
    for value in _walk_values(payload, keys):
        if value is None:
            continue
        return str(value)
    return None


def _find_first_decimal(payload: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        try:
            return Decimal(str(payload[key]))
        except Exception:
            return None
    return None


def _find_first_bool(payload: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "active", "enabled"}:
                return True
            if normalized in {"false", "0", "no", "inactive", "disabled"}:
                return False
    return None


def _walk_values(payload: Any, keys: tuple[str, ...]):
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                yield payload[key]
        for value in payload.values():
            yield from _walk_values(value, keys)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_values(item, keys)


def _find_dict_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []

    for key in ("items", "lots", "data", "results", "listings"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _find_dict_items(value)
            if nested:
                return nested

    lot_keys = {"lot_id", "lotId", "listing_id", "listingId", "price", "active", "is_active"}
    return [payload] if lot_keys.intersection(payload) else []


def _parse_lot(payload: dict[str, Any]) -> MyLotSummary:
    return MyLotSummary(
        lot_id=_find_first_string(
            payload,
            ("lot_id", "lotId", "listing_id", "listingId", "id"),
        ),
        title=_find_first_string(payload, ("title", "name", "description")),
        position_amount=_find_first_int(
            payload,
            ("position_amount", "positionAmount", "amount", "robux"),
        ),
        price=_find_first_decimal(
            payload,
            ("price", "cost", "amount_price", "amountPrice"),
        ),
        is_active=_find_first_bool(
            payload,
            ("is_active", "isActive", "active", "enabled", "status"),
        ),
        raw_payload=payload,
    )


def _find_first_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        try:
            return int(payload[key])
        except (TypeError, ValueError):
            continue
    return None
