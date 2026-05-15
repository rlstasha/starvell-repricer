import html as html_module
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.market.exceptions import StarvellEndpointNotConfiguredError, StarvellWriteDisabledError
from app.market.schemas import (
    AccountInfo,
    MarketOffer,
    MyLotSummary,
    OwnLot,
    StarvellConnectionCheck,
    UpdateResult,
)
from app.repricer.rate_limiter import RateLimiter

KNOWN_STARVELL_LOT_IDS = frozenset(
    {
        "1996",
        "1998",
        "1999",
        "2000",
        "2002",
        "2003",
        "2004",
        "2005",
        "2006",
        "2007",
        "2008",
        "2009",
        "2010",
        "2011",
        "2012",
    }
)
KNOWN_STARVELL_ROBUX_AMOUNTS = frozenset(
    {80, 200, 400, 500, 800, 1000, 1200, 1700, 2000, 2100, 2500, 3600, 4500, 10000, 22500}
)
STARVELL_CATEGORY_ID = 40
STARVELL_ROBUX_SUBCATEGORY_IDS = {
    40: 64,
    80: 65,
    200: 66,
    400: 67,
    500: 333,
    800: 69,
    1000: 70,
    1200: 71,
    1700: 72,
    2000: 334,
    2100: 73,
    2500: 74,
    3600: 75,
    4500: 76,
    10000: 77,
    22500: 78,
}
_OFFER_REF_RE = re.compile(
    r"""
    (?:href\s*=\s*|["']href["']\s*:\s*)
    ["'](?P<href>(?:https?://[^"']+)?/offers/(?P<lot_id>\d+)(?:[/?#][^"']*)?)["']
    """,
    re.IGNORECASE | re.VERBOSE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_JSON_SCRIPT_RE = re.compile(
    r"<script[^>]+type=[\"']application/json[\"'][^>]*>(?P<payload>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_POSITION_AMOUNT_RE = re.compile(
    r"(?P<amount>\d+(?:[\s\u00a0]\d{3})*)\s*(?:робукс(?:ов|а)?|robux)",
    re.IGNORECASE,
)
_PRICE_RE = re.compile(
    r"(?P<price>\d+(?:[\s\u00a0]\d{3})*(?:[,.]\d+)?)\s*(?:₽|руб\.?|rub)",
    re.IGNORECASE,
)
_STOCK_RE = re.compile(
    r"(?:наличие|остаток|available|stock)\D{0,40}(?P<stock>\d+(?:[\s\u00a0]\d{3})*)",
    re.IGNORECASE,
)
_SELLER_ID_RE = re.compile(
    r"""(?:seller[_-]?id|sellerId)["'\s:=]+(?P<seller_id>\d+)""",
    re.IGNORECASE,
)
_USER_HREF_RE = re.compile(r"/users/(?P<seller_id>\d+)")
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(?P<title>.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_TAG_RE = re.compile(r"<h1[^>]*>(?P<title>.*?)</h1>", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class MarketOffersFetchResult:
    position_amount: int
    lot_id: str | None
    method: str
    url: str
    source: str
    raw_offer_count: int
    offers: list[MarketOffer]
    subcategory_id: int | None = None
    parser_rejected_count: int = 0


class StarvellClient:
    """Single boundary for all Starvell/Statvell HTTP access.

    Read operations use safe GET requests. Price writes stay behind this boundary and are
    deliberately blocked until a reviewed write integration is added.
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

    async def fetch_text(self, url: str, *, request_type: str) -> tuple[str, int, str]:
        """Fetch a Starvell page with a safe GET request for diagnostics/parsing."""
        response = await self._request("GET", url, request_type=request_type)
        return response.text, response.status_code, response.headers.get("content-type", "")

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
        """Fetch public market offers through the read-only Starvell market list."""
        result = await self.get_market_offers_result(position_amount, lot_id)
        return result.offers

    async def get_market_offers_result(
        self,
        position_amount: int,
        lot_id: str | None,
    ) -> MarketOffersFetchResult:
        """Fetch public market offers and keep diagnostics about the requested source."""
        subcategory_id = STARVELL_ROBUX_SUBCATEGORY_IDS.get(position_amount)
        if self.settings.market_offers_api_url and subcategory_id is not None:
            response = await self._request(
                "POST",
                self.settings.market_offers_api_url,
                request_type="market_offers",
                json=_market_offers_api_payload(
                    position_amount=position_amount,
                    limit=self.settings.market_offers_limit,
                ),
            )
            payload = response.json()
            raw_items = _extract_offer_payload_items(payload)
            offers = parse_starvell_market_offers_payload(
                raw_items,
                position_amount=position_amount,
            )
            result = MarketOffersFetchResult(
                position_amount=position_amount,
                lot_id=lot_id,
                method="POST",
                url=self.settings.market_offers_api_url,
                source="api_list_by_category",
                raw_offer_count=len(raw_items),
                offers=offers,
                subcategory_id=subcategory_id,
                parser_rejected_count=max(len(raw_items) - len(offers), 0),
            )
            self._log_empty_market_result(result)
            return result

        return await self._get_market_offers_from_category_html(position_amount, lot_id)

    async def _get_market_offers_from_category_html(
        self,
        position_amount: int,
        lot_id: str | None,
    ) -> MarketOffersFetchResult:
        response = await self._request(
            "GET",
            self.settings.market_offers_url,
            request_type="market_offers",
        )
        raw_items = _extract_market_offer_items_from_html(response.text)
        offers = parse_starvell_market_offers_payload(
            raw_items,
            position_amount=position_amount,
        )
        result = MarketOffersFetchResult(
            position_amount=position_amount,
            lot_id=lot_id,
            method="GET",
            url=self.settings.market_offers_url,
            source="category_html",
            raw_offer_count=len(raw_items),
            offers=offers,
            subcategory_id=STARVELL_ROBUX_SUBCATEGORY_IDS.get(position_amount),
            parser_rejected_count=max(len(raw_items) - len(offers), 0),
        )
        self._log_empty_market_result(result)
        return result

    def _log_empty_market_result(self, result: MarketOffersFetchResult) -> None:
        if result.offers:
            return
        self.logger.warning(
            "starvell_market_offers_not_found",
            position_amount=result.position_amount,
            lot_id=result.lot_id,
            method=result.method,
            url=result.url,
            subcategory_id=result.subcategory_id,
            raw_offer_count=result.raw_offer_count,
            parser_rejected_count=result.parser_rejected_count,
            source=result.source,
        )

    async def get_my_lot(self, position_amount: int, lot_id: str | None) -> OwnLot | None:
        """Fetch own lot page by lot_id using safe GET and parse current price."""
        if not lot_id:
            return None

        response = await self._request(
            "GET",
            f"/offers/{lot_id}",
            request_type="my_lot",
        )
        return parse_starvell_own_lot(
            response.text,
            position_amount=position_amount,
            lot_id=lot_id,
        )

    async def get_my_lots(self) -> list[MyLotSummary]:
        """Fetch own active lots through the configured safe GET endpoint."""
        if not self.settings.market_my_lots_url:
            raise StarvellEndpointNotConfiguredError("MARKET_MY_LOTS_URL is not configured")

        response = await self._request(
            "GET",
            self.settings.market_my_lots_url,
            request_type="my_lots",
        )
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type or _looks_like_json(response.text):
            try:
                payload = response.json()
            except ValueError:
                payload = None
            return [_parse_lot(item) for item in _find_dict_items(payload)]

        return parse_starvell_lots_from_html(
            response.text,
            default_seller_id=self.settings.own_seller_id,
        )

    async def update_my_lot_price(
        self,
        position_amount: int,
        lot_id: str | None,
        new_price: Decimal,
    ) -> UpdateResult:
        """Block real price writes until the Starvell write flow is explicitly reviewed."""
        await self.rate_limiter.acquire()
        self.logger.error(
            "starvell_price_update_blocked",
            position_amount=position_amount,
            lot_id=lot_id,
            new_price=str(new_price),
            reason="write endpoint is disabled",
        )
        raise StarvellWriteDisabledError("Starvell price writes are disabled in this build")

    async def get_account_info(self) -> AccountInfo:
        """Fetch account information through the configured safe GET endpoint."""
        if not self.settings.market_account_info_url:
            raise StarvellEndpointNotConfiguredError("MARKET_ACCOUNT_INFO_URL is not configured")

        response = await self._request(
            "GET",
            self.settings.market_account_info_url,
            request_type="account_info",
        )
        content_type = response.headers.get("content-type", "").lower()
        if "json" in content_type or _looks_like_json(response.text):
            try:
                payload = response.json()
            except ValueError:
                payload = None
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

        return AccountInfo(
            seller_id=(
                _extract_seller_id(response.text)
                or _extract_seller_id_from_url(str(response.url))
                or _extract_seller_id_from_url(self.settings.market_account_info_url)
                or self.settings.own_seller_id
            ),
            seller_username=_extract_profile_username(response.text),
            raw_payload=None,
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
            return Decimal(_normalize_decimal(str(payload[key])))
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

    for key in ("items", "lots", "data", "results", "listings", "offers", "userProfileOffers"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _find_dict_items(value)
            if nested:
                return nested

    lot_keys = {
        "lot_id",
        "lotId",
        "listing_id",
        "listingId",
        "offer_id",
        "offerId",
        "price",
        "active",
        "is_active",
    }
    return [payload] if lot_keys.intersection(payload) else []


def _parse_lot(payload: dict[str, Any]) -> MyLotSummary:
    return MyLotSummary(
        lot_id=_find_first_string(
            payload,
            ("lot_id", "lotId", "listing_id", "listingId", "offer_id", "offerId", "id"),
        ),
        title=_find_first_string(payload, ("title", "name", "description", "label")),
        position_amount=_find_first_int(
            payload,
            ("position_amount", "positionAmount", "robux_amount", "robuxAmount", "amount", "robux"),
        ),
        price=_find_first_decimal(
            payload,
            ("price", "cost", "amount_price", "amountPrice", "price_rub", "priceRub"),
        ),
        seller_id=_find_first_string(
            payload,
            (
                "seller_id",
                "sellerId",
                "seller_user_id",
                "sellerUserId",
                "user_id",
                "userId",
            ),
        ),
        stock=_find_first_int(
            payload,
            (
                "stock",
                "available",
                "availability",
                "quantity",
                "count",
                "in_stock",
                "inStock",
            ),
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


def _find_direct_string(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key in payload and payload[key] is not None:
            return str(payload[key])
    return None


def _find_direct_int(payload: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        try:
            return int(payload[key])
        except (TypeError, ValueError):
            continue
    return None


def _find_direct_decimal(payload: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key not in payload or payload[key] is None:
            continue
        try:
            return Decimal(_normalize_decimal(str(payload[key])))
        except Exception:
            continue
    return None


def parse_starvell_lots_from_html(
    html: str,
    *,
    default_seller_id: str | None = None,
) -> list[MyLotSummary]:
    """Parse active seller lots from the Starvell profile HTML page."""
    source_html = html_module.unescape(html).replace("\\/", "/")
    seller_id = default_seller_id or _extract_seller_id(source_html)
    lots_by_id: dict[str, MyLotSummary] = {}

    for item in _find_embedded_lot_items(source_html):
        parsed = _parse_lot(item)
        if not parsed.lot_id:
            continue
        normalized = MyLotSummary(
            lot_id=parsed.lot_id,
            title=parsed.title or _title_from_amount(parsed.position_amount),
            position_amount=parsed.position_amount,
            price=parsed.price,
            seller_id=parsed.seller_id or seller_id,
            stock=parsed.stock,
            is_active=parsed.is_active,
            raw_payload=parsed.raw_payload,
        )
        lots_by_id[parsed.lot_id] = _merge_lot_summary(
            lots_by_id.get(parsed.lot_id),
            normalized,
        )

    matches = list(_OFFER_REF_RE.finditer(source_html))
    for index, match in enumerate(matches):
        lot_id = match.group("lot_id")
        next_different_offer = _next_different_offer_start(
            matches,
            index,
            lot_id,
            len(source_html),
        )
        window_start = match.start()
        window_end = min(len(source_html), next_different_offer)
        window = source_html[window_start:window_end]
        window_text = _html_to_text(window)
        anchor_text = _extract_anchor_text(source_html, match.start(), match.end())

        title = _choose_title(anchor_text, window_text)
        position_amount = (
            _extract_position_amount(anchor_text)
            or _extract_position_amount(window_text)
        )
        price = _extract_price(window)
        stock = _extract_stock(window)
        parsed = MyLotSummary(
            lot_id=lot_id,
            title=title,
            position_amount=position_amount,
            price=price,
            seller_id=seller_id,
            stock=stock,
            is_active=True,
            raw_payload={
                "source": "html",
                "href": match.group("href"),
            },
        )
        lots_by_id[lot_id] = _merge_lot_summary(lots_by_id.get(lot_id), parsed)

    return list(lots_by_id.values())


def parse_starvell_market_offers(html: str, *, position_amount: int) -> list[MarketOffer]:
    return parse_starvell_market_offers_payload(
        _extract_market_offer_items_from_html(html),
        position_amount=position_amount,
    )


def parse_starvell_market_offers_payload(
    payload_offers: list[dict[str, Any]],
    *,
    position_amount: int,
) -> list[MarketOffer]:
    offers: list[MarketOffer] = []
    for payload in payload_offers:
        offer = _parse_market_offer(payload, position_amount=position_amount)
        if offer is not None:
            offers.append(offer)
    return offers


def parse_starvell_own_lot(
    html: str,
    *,
    position_amount: int,
    lot_id: str,
) -> OwnLot | None:
    page_props = _extract_next_page_props(html)
    payload = page_props.get("offer")
    if not isinstance(payload, dict):
        return None

    parsed_lot_id = _find_direct_string(payload, ("id", "lot_id", "lotId", "offer_id", "offerId"))
    if parsed_lot_id and parsed_lot_id != str(lot_id):
        return None

    parsed_amount = _extract_amount_from_offer_payload(payload)
    if parsed_amount is not None and parsed_amount != position_amount:
        return None

    price = _find_direct_decimal(payload, ("price", "cost", "amount_price", "amountPrice"))
    if price is None:
        return None

    return OwnLot(
        position_amount=position_amount,
        price=price,
        lot_id=str(lot_id),
        raw_payload=payload,
    )


def _parse_market_offer(
    payload: dict[str, Any],
    *,
    position_amount: int,
) -> MarketOffer | None:
    if _extract_amount_from_offer_payload(payload) != position_amount:
        return None

    price = _find_direct_decimal(payload, ("price", "cost", "amount_price", "amountPrice"))
    if price is None:
        return None

    user = payload.get("user")
    if not isinstance(user, dict):
        user = {}

    return MarketOffer(
        position_amount=position_amount,
        price=price,
        seller_id=_find_direct_string(user, ("id", "seller_id", "sellerId", "user_id", "userId")),
        seller_username=_find_direct_string(user, ("username", "name", "login")),
        rating=_find_direct_decimal(user, ("rating", "avgRating")),
        is_active=_is_offer_active(payload),
        raw_payload=payload,
    )


def _extract_amount_from_offer_payload(payload: dict[str, Any]) -> int | None:
    amount = _find_direct_int(
        payload,
        ("position_amount", "positionAmount", "robux_amount", "robuxAmount", "amount", "robux"),
    )
    if amount is not None:
        return amount

    sub_category = payload.get("subCategory")
    if isinstance(sub_category, dict):
        amount = _extract_position_amount(_find_direct_string(sub_category, ("name", "title")))
        if amount is not None:
            return amount

    title = _find_direct_string(payload, ("title", "name", "description", "label"))
    return _extract_position_amount(title)


def _is_offer_active(payload: dict[str, Any]) -> bool | None:
    active = _find_first_bool(payload, ("is_active", "isActive", "active", "enabled"))
    if active is not None:
        return active
    if payload.get("isHidden") is True:
        return False
    availability = _find_direct_int(payload, ("availability", "stock", "available", "quantity"))
    if availability is not None:
        return availability > 0
    return None


def _market_offers_api_payload(*, position_amount: int, limit: int) -> dict[str, Any]:
    subcategory_id = STARVELL_ROBUX_SUBCATEGORY_IDS[position_amount]
    return {
        "categoryId": STARVELL_CATEGORY_ID,
        "subCategoryId": subcategory_id,
        "onlyOnlineUsers": False,
        "attributes": [],
        "numericRangeFilters": [],
        "limit": limit,
        "offset": 0,
        "sortBy": "price",
        "sortDir": "ASC",
        "sortByPriceAndBumped": True,
        "withCompletionRates": True,
    }


def _extract_market_offer_items_from_html(html: str) -> list[dict[str, Any]]:
    page_props = _extract_next_page_props(html)
    payload_offers = page_props.get("offers")
    if not isinstance(payload_offers, list):
        return []
    return [item for item in payload_offers if isinstance(item, dict)]


def _extract_offer_payload_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "items", "offers", "results", "list"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_offer_payload_items(value)
            if nested:
                return nested
    return []


def _extract_next_page_props(html: str) -> dict[str, Any]:
    for match in _JSON_SCRIPT_RE.finditer(html):
        payload_text = html_module.unescape(match.group("payload")).strip()
        try:
            payload = json.loads(payload_text)
        except ValueError:
            continue

        if "pageProps" in payload and isinstance(payload["pageProps"], dict):
            return payload["pageProps"]

        page_props = (
            payload.get("props", {}).get("pageProps")
            if isinstance(payload, dict)
            else None
        )
        if isinstance(page_props, dict):
            return page_props
    return {}


def _find_embedded_lot_items(html: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in _JSON_SCRIPT_RE.finditer(html):
        payload_text = html_module.unescape(match.group("payload")).strip()
        try:
            payload = json.loads(payload_text)
        except ValueError:
            continue
        items.extend(_find_lot_like_dicts(payload))
    return items


def _find_lot_like_dicts(payload: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        if _is_lot_like_dict(payload):
            items.append(payload)
        for value in payload.values():
            items.extend(_find_lot_like_dicts(value))
    elif isinstance(payload, list):
        for item in payload:
            items.extend(_find_lot_like_dicts(item))
    return items


def _is_lot_like_dict(payload: dict[str, Any]) -> bool:
    lot_id = _find_direct_string(
        payload,
        ("lot_id", "lotId", "listing_id", "listingId", "offer_id", "offerId", "id"),
    )
    if lot_id in KNOWN_STARVELL_LOT_IDS:
        return True

    href = _find_direct_string(payload, ("href", "url", "path", "link"))
    if href and _OFFER_REF_RE.search(f'"href":"{href}"'):
        return True

    title = _find_direct_string(payload, ("title", "name", "description", "label"))
    amount = _find_direct_int(
        payload,
        ("position_amount", "positionAmount", "robux_amount", "robuxAmount", "amount", "robux"),
    )
    return bool(
        title
        and _extract_position_amount(title)
        and amount in KNOWN_STARVELL_ROBUX_AMOUNTS
        and _find_direct_decimal(payload, ("price", "cost", "price_rub", "priceRub"))
    )


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _next_different_offer_start(
    matches: list[re.Match[str]],
    index: int,
    lot_id: str,
    fallback: int,
) -> int:
    for later in matches[index + 1 :]:
        if later.group("lot_id") != lot_id:
            return later.start()
    return fallback


def _html_to_text(fragment: str) -> str:
    text = html_module.unescape(_TAG_RE.sub(" ", fragment))
    return " ".join(text.split())


def _extract_anchor_text(html: str, href_start: int, href_end: int) -> str | None:
    anchor_start = html.rfind("<a", max(0, href_start - 300), href_start + 1)
    anchor_end = html.find("</a>", href_end)
    if anchor_start == -1 or anchor_end == -1:
        return None
    return _html_to_text(html[anchor_start : anchor_end + len("</a>")]) or None


def _choose_title(anchor_text: str | None, window_text: str) -> str | None:
    if anchor_text and _extract_position_amount(anchor_text):
        return anchor_text
    amount_match = _POSITION_AMOUNT_RE.search(window_text)
    if amount_match:
        return amount_match.group(0).strip()
    return anchor_text


def _extract_position_amount(text: str | None) -> int | None:
    if not text:
        return None
    match = _POSITION_AMOUNT_RE.search(text)
    if not match:
        return None
    amount = _parse_int(match.group("amount"))
    if amount in KNOWN_STARVELL_ROBUX_AMOUNTS:
        return amount
    return amount


def _extract_price(text: str) -> Decimal | None:
    match = _PRICE_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(_normalize_decimal(match.group("price")))
    except Exception:
        return None


def _extract_stock(text: str) -> int | None:
    match = _STOCK_RE.search(text)
    if not match:
        return None
    return _parse_int(match.group("stock"))


def _extract_seller_id(html: str) -> str | None:
    match = _SELLER_ID_RE.search(html) or _USER_HREF_RE.search(html)
    if not match:
        return None
    return match.group("seller_id")


def _extract_seller_id_from_url(url: str) -> str | None:
    match = _USER_HREF_RE.search(url)
    if not match:
        return None
    return match.group("seller_id")


def _extract_profile_username(html: str) -> str | None:
    for pattern in (_H1_TAG_RE, _TITLE_TAG_RE):
        match = pattern.search(html)
        if not match:
            continue
        title = _clean_profile_title(_html_to_text(match.group("title")))
        if title:
            return title
    return None


def _clean_profile_title(title: str) -> str | None:
    title = " ".join(title.split()).strip()
    if not title:
        return None
    for separator in (" | ", " - ", " — "):
        if separator in title:
            parts = [part.strip() for part in title.split(separator) if part.strip()]
            for part in parts:
                if "starvell" not in part.lower() and part:
                    return part
    return title


def _parse_int(raw_value: str) -> int | None:
    normalized = raw_value.replace("\u00a0", "").replace(" ", "")
    try:
        return int(normalized)
    except (TypeError, ValueError):
        return None


def _normalize_decimal(raw_value: str) -> str:
    return raw_value.replace("\u00a0", "").replace(" ", "").replace(",", ".")


def _merge_lot_summary(
    existing: MyLotSummary | None,
    incoming: MyLotSummary,
) -> MyLotSummary:
    if existing is None:
        return incoming
    return MyLotSummary(
        lot_id=existing.lot_id or incoming.lot_id,
        title=existing.title or incoming.title,
        position_amount=existing.position_amount or incoming.position_amount,
        price=existing.price or incoming.price,
        seller_id=existing.seller_id or incoming.seller_id,
        stock=existing.stock or incoming.stock,
        is_active=existing.is_active if existing.is_active is not None else incoming.is_active,
        raw_payload=existing.raw_payload or incoming.raw_payload,
    )
