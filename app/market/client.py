import html as html_module
import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings
from app.core.logging import get_logger
from app.core.network import mask_proxy_url
from app.market.exceptions import (
    StarvellEndpointNotConfiguredError,
    StarvellPayloadStyleError,
    StarvellWriteDisabledError,
)
from app.market.schemas import (
    AccountInfo,
    MarketOffer,
    MyLotSummary,
    OwnLot,
    PriceUpdateAttemptResult,
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


@dataclass(frozen=True)
class PriceUpdatePayloadCandidate:
    name: str
    payload: dict[str, Any]


class StarvellClient:
    """Single boundary for all Starvell/Statvell HTTP access.

    Read operations and price writes stay behind this boundary so auth, proxy selection,
    rate limiting, and secret masking remain consistent in one place.
    """

    def __init__(
        self,
        settings: Settings,
        rate_limiter: RateLimiter,
        http_client: httpx.AsyncClient | None = None,
        *,
        proxy_profile: str | None = None,
        proxy_url: str | None = None,
    ):
        self.settings = settings
        self.rate_limiter = rate_limiter
        self._http_client = http_client
        self._owns_http_client = http_client is None
        self.proxy_profile = proxy_profile
        self.proxy_url = proxy_url
        self.logger = get_logger(__name__)
        self._own_lot_cache: dict[str, tuple[float, OwnLot]] = {}
        self._price_update_context_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._request_counts: dict[str, int] = {"total": 0}

    async def __aenter__(self) -> "StarvellClient":
        await self._ensure_http_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._http_client and self._owns_http_client:
            await self._http_client.aclose()

    def _http_client_kwargs(self) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {
            "base_url": self.settings.market_base_url,
            "timeout": httpx.Timeout(30.0),
            "headers": self._default_headers(),
            "cookies": self._default_cookies(),
        }
        if self.proxy_url:
            client_kwargs["proxy"] = self.proxy_url
        return client_kwargs

    async def _ensure_http_client(self) -> None:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(**self._http_client_kwargs())

    async def _reset_owned_http_client(self) -> None:
        if not self._owns_http_client:
            return
        if self._http_client is not None:
            await self._http_client.aclose()
        self._http_client = httpx.AsyncClient(**self._http_client_kwargs())

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
        await self._ensure_http_client()
        if self._http_client is None:
            raise RuntimeError("StarvellClient HTTP client is not initialized")
        attempts = 2
        for attempt in range(1, attempts + 1):
            await self.rate_limiter.acquire()
            self._record_request_attempt(request_type)
            try:
                response = await self._http_client.request(method, url, **kwargs)
            except httpx.TimeoutException:
                if hasattr(self.rate_limiter, "apply_backoff"):
                    self.rate_limiter.apply_backoff("timeout")
                self.logger.warning(
                    "starvell_http_timeout",
                    method=method,
                    url=url,
                    request_type=request_type,
                    proxy_profile=self.proxy_profile or "direct",
                    proxy=mask_proxy_url(self.proxy_url),
                    attempt=attempt,
                    will_retry=attempt < attempts,
                )
                if attempt < attempts:
                    await self._reset_owned_http_client()
                    continue
                raise
            except httpx.TransportError as exc:
                self._apply_transport_backoff(exc)
                self.logger.warning(
                    "starvell_http_transport_error",
                    method=method,
                    url=url,
                    request_type=request_type,
                    reason=safe_starvell_error_reason(exc),
                    error_type=type(exc).__name__,
                    proxy_profile=self.proxy_profile or "direct",
                    proxy=mask_proxy_url(self.proxy_url),
                    attempt=attempt,
                    will_retry=attempt < attempts,
                )
                if attempt < attempts:
                    await self._reset_owned_http_client()
                    continue
                raise
            except Exception as exc:
                if not _looks_like_proxy_transport_error(exc):
                    raise
                self._apply_transport_backoff(exc)
                self.logger.warning(
                    "starvell_proxy_transport_error",
                    method=method,
                    url=url,
                    request_type=request_type,
                    reason=safe_starvell_error_reason(exc),
                    error_type=type(exc).__name__,
                    proxy_profile=self.proxy_profile or "direct",
                    proxy=mask_proxy_url(self.proxy_url),
                    attempt=attempt,
                    will_retry=attempt < attempts,
                )
                if attempt < attempts:
                    await self._reset_owned_http_client()
                    continue
                raise

            rate_limit_event = None
            if hasattr(self.rate_limiter, "record_response"):
                rate_limit_event = await self.rate_limiter.record_response(
                    response.status_code,
                    response.headers,
                    request_type=request_type,
                )
            elif response.status_code < 400 and hasattr(self.rate_limiter, "reset_backoff"):
                self.rate_limiter.reset_backoff()
            elif response.status_code in {403, 429} and hasattr(self.rate_limiter, "apply_backoff"):
                self.rate_limiter.apply_backoff(_error_kind_from_status(response.status_code))
            if rate_limit_event is not None:
                self.logger.warning(
                    "rate_limit_backoff_applied",
                    profile=self.proxy_profile or "direct",
                    position=None,
                    endpoint=url,
                    request_type=request_type,
                    old_effective_limit=rate_limit_event.old_effective_limit_per_minute,
                    new_effective_limit=rate_limit_event.new_effective_limit_per_minute,
                    reason=rate_limit_event.reason,
                    recovery_eta=rate_limit_event.recovery_eta_seconds,
                    retry_after=rate_limit_event.retry_after_seconds,
                    consecutive_429s=rate_limit_event.consecutive_429s,
                )
            self.logger.info(
                "starvell_http_request",
                method=method,
                url=url,
                request_type=request_type,
                status_code=response.status_code,
                proxy_profile=self.proxy_profile or "direct",
                proxy=mask_proxy_url(self.proxy_url),
            )
            response.raise_for_status()
            return response

        raise RuntimeError("Starvell request retry loop ended unexpectedly")

    def request_metrics_snapshot(self) -> dict[str, int]:
        return dict(self._request_counts)

    def _record_request_attempt(self, request_type: str) -> None:
        self._request_counts["total"] = self._request_counts.get("total", 0) + 1
        self._request_counts[request_type] = self._request_counts.get(request_type, 0) + 1

    def _apply_transport_backoff(self, exc: Exception) -> None:
        if hasattr(self.rate_limiter, "apply_backoff"):
            self.rate_limiter.apply_backoff("proxy" if self.proxy_url else "network")

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
        if cached_lot := self._cached_own_lot(lot_id):
            return cached_lot

        response = await self._request(
            "GET",
            f"/offers/{lot_id}",
            request_type="my_lot",
        )
        lot = parse_starvell_own_lot(
            response.text,
            position_amount=position_amount,
            lot_id=lot_id,
        )
        if lot is not None:
            self._remember_own_lot(lot)
        return lot

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
        *,
        allow_real_write: bool | None = None,
        payload_override: dict[str, Any] | None = None,
        payload_style: str | None = None,
        content_type: str | None = None,
        variant_name: str | None = None,
    ) -> UpdateResult:
        """Update an own lot price through the configured Starvell write endpoint."""
        self._ensure_price_write_allowed(lot_id, allow_real_write=allow_real_write)
        method = self.settings.market_update_lot_price_method
        url = _format_price_update_url(
            self.settings.market_update_lot_price_url,
            lot_id=str(lot_id),
            position_amount=position_amount,
        )
        style = payload_style or self.settings.market_update_price_payload_style
        context: dict[str, Any] | None = None
        if not payload_override and _price_update_style_needs_context(style):
            context = await self.get_price_update_context(position_amount, str(lot_id))
        payload = payload_override or _price_update_payload(
            new_price,
            style=style,
            lot_id=str(lot_id),
            position_amount=position_amount,
            context=context,
        )
        request_content_type = content_type or self.settings.market_update_price_content_type

        if self.settings.price_write_discovery:
            self.logger.info(
                "starvell_price_write_discovery",
                method=method,
                url=url,
                payload=_sanitize_payload(payload),
                payload_style=style,
                content_type=request_content_type,
                variant=variant_name,
                proxy_profile=self.proxy_profile or "direct",
                proxy=mask_proxy_url(self.proxy_url),
            )

        try:
            response = await self._send_price_update_request(
                method=method,
                url=url,
                payload=payload,
                content_type=request_content_type,
            )
        except httpx.HTTPStatusError as exc:
            if _should_forget_price_update_context(exc.response.status_code):
                self._forget_lot_caches(str(lot_id))
            debug_extra = (
                _debug_response_fields(exc.response, self.settings)
                if self.settings.price_write_discovery
                else {}
            )
            self.logger.warning(
                "price_update_failed",
                position=position_amount,
                lot_id=str(lot_id),
                new_price=str(new_price),
                reason=safe_starvell_error_reason(exc),
                status_code=exc.response.status_code,
                method=method,
                url=url,
                payload=_sanitize_payload(payload),
                content_type=request_content_type,
                variant=variant_name,
                proxy=self.proxy_profile or "direct",
                **debug_extra,
            )
            raise
        except Exception as exc:
            self.logger.warning(
                "price_update_failed",
                position=position_amount,
                lot_id=str(lot_id),
                new_price=str(new_price),
                reason=safe_starvell_error_reason(exc),
                status_code=None,
                method=method,
                url=url,
                payload=_sanitize_payload(payload),
                content_type=request_content_type,
                variant=variant_name,
                proxy=self.proxy_profile or "direct",
            )
            raise

        raw_payload: dict[str, Any] | None = None
        try:
            payload_response = response.json()
        except ValueError:
            payload_response = None
        if isinstance(payload_response, dict):
            raw_payload = payload_response

        self.logger.info(
            "price_updated",
            position=position_amount,
            lot_id=str(lot_id),
            new_price=str(new_price),
            method=method,
            url=url,
            payload_style=style,
            content_type=request_content_type,
            variant=variant_name,
            proxy=self.proxy_profile or "direct",
            status="success",
        )
        cached_lot = self._cached_own_lot(str(lot_id))
        self._remember_own_lot(
            OwnLot(
                position_amount=position_amount,
                price=new_price,
                lot_id=str(lot_id),
                raw_payload=cached_lot.raw_payload if cached_lot else None,
            )
        )
        return UpdateResult(success=True, raw_payload=raw_payload)

    async def debug_my_lot_price_update(
        self,
        position_amount: int,
        lot_id: str | None,
        new_price: Decimal,
        *,
        allow_real_write: bool | None = None,
    ) -> list[PriceUpdateAttemptResult]:
        """Try known Starvell price payload shapes and return safe request diagnostics."""
        self._ensure_price_write_allowed(lot_id, allow_real_write=allow_real_write)
        method = self.settings.market_update_lot_price_method
        url = _format_price_update_url(
            self.settings.market_update_lot_price_url,
            lot_id=str(lot_id),
            position_amount=position_amount,
        )
        context = await self.get_price_update_context(position_amount, str(lot_id))
        candidates = _price_update_payload_candidates(
            new_price,
            context=context,
            lot_id=str(lot_id),
            position_amount=position_amount,
        )
        results: list[PriceUpdateAttemptResult] = []

        for candidate in candidates:
            for request_content_type in ("json", "form"):
                try:
                    response = await self._send_price_update_request(
                        method=method,
                        url=url,
                        payload=candidate.payload,
                        content_type=request_content_type,
                    )
                except httpx.HTTPStatusError as exc:
                    attempt = _price_update_attempt_result(
                        candidate=candidate,
                        method=method,
                        url=url,
                        content_type=request_content_type,
                        response=exc.response,
                        settings=self.settings,
                        reason=safe_starvell_error_reason(exc),
                    )
                    self._log_price_update_attempt(attempt)
                    results.append(attempt)
                    if exc.response.status_code in {401, 403, 429} or exc.response.status_code >= 500:
                        return results
                    continue
                except Exception as exc:
                    attempt = PriceUpdateAttemptResult(
                        variant=candidate.name,
                        method=method,
                        url=url,
                        request_content_type=_request_content_type_label(request_content_type),
                        payload=_sanitize_payload(candidate.payload),
                        status_code=None,
                        success=False,
                        reason=safe_starvell_error_reason(exc),
                    )
                    self._log_price_update_attempt(attempt)
                    results.append(attempt)
                    return results

                attempt = _price_update_attempt_result(
                    candidate=candidate,
                    method=method,
                    url=url,
                    content_type=request_content_type,
                    response=response,
                    settings=self.settings,
                    reason=None,
                )
                self._log_price_update_attempt(attempt)
                results.append(attempt)
                if response.status_code < 400:
                    return results

        return results

    async def get_price_update_context(
        self,
        position_amount: int,
        lot_id: str,
    ) -> dict[str, Any]:
        """Pull non-secret offer fields from safe GET pages for diagnostic write payloads."""
        if cached_context := self._cached_price_update_context(lot_id):
            self._complete_price_update_context_defaults(cached_context, lot_id, position_amount)
            return cached_context

        context: dict[str, Any] = {}
        for path in (f"/offers/{lot_id}", f"/offers/edit/{lot_id}"):
            try:
                response = await self._request(
                    "GET",
                    path,
                    request_type="price_update_context",
                )
            except httpx.HTTPStatusError:
                continue
            except Exception:
                continue
            offer_payload = _extract_offer_payload_from_html(response.text, lot_id=lot_id)
            if offer_payload:
                context.update(_price_update_context_from_offer_payload(offer_payload))
                break

        self._complete_price_update_context_defaults(context, lot_id, position_amount)
        self._remember_price_update_context(lot_id, context)
        return context

    def _complete_price_update_context_defaults(
        self,
        context: dict[str, Any],
        lot_id: str,
        position_amount: int,
    ) -> None:
        context.setdefault("id", str(lot_id))
        context.setdefault("offer_id", str(lot_id))
        context.setdefault("offerId", str(lot_id))
        if self.settings.own_seller_id:
            context.setdefault("seller_id", self.settings.own_seller_id)
            context.setdefault("sellerId", self.settings.own_seller_id)
        if position_amount > 0:
            context.setdefault("position_amount", position_amount)
            context.setdefault("positionAmount", position_amount)
        if self.settings.market_csrf_token:
            context.setdefault("csrf", self.settings.market_csrf_token)
            context.setdefault("_csrf", self.settings.market_csrf_token)

    def _cached_own_lot(self, lot_id: str) -> OwnLot | None:
        ttl_seconds = self.settings.my_lot_state_cache_ttl_seconds
        if ttl_seconds <= 0:
            return None
        cached = self._own_lot_cache.get(str(lot_id))
        if cached is None:
            return None
        cached_at, lot = cached
        if time.monotonic() - cached_at > ttl_seconds:
            self._own_lot_cache.pop(str(lot_id), None)
            return None
        if lot.raw_payload:
            context = _price_update_context_from_offer_payload(lot.raw_payload)
            if context:
                self._remember_price_update_context(str(lot_id), context)
        return lot

    def _remember_own_lot(self, lot: OwnLot) -> None:
        if self.settings.my_lot_state_cache_ttl_seconds <= 0 or not lot.lot_id:
            return
        self._own_lot_cache[str(lot.lot_id)] = (time.monotonic(), lot)
        if lot.raw_payload:
            context = _price_update_context_from_offer_payload(lot.raw_payload)
            if context:
                self._remember_price_update_context(str(lot.lot_id), context)

    def _cached_price_update_context(self, lot_id: str) -> dict[str, Any] | None:
        ttl_seconds = self.settings.price_update_context_cache_ttl_seconds
        if ttl_seconds <= 0:
            return None
        cached = self._price_update_context_cache.get(str(lot_id))
        if cached is None:
            return None
        cached_at, context = cached
        if time.monotonic() - cached_at > ttl_seconds:
            self._price_update_context_cache.pop(str(lot_id), None)
            return None
        return dict(context)

    def _remember_price_update_context(self, lot_id: str, context: dict[str, Any]) -> None:
        if self.settings.price_update_context_cache_ttl_seconds <= 0:
            return
        self._price_update_context_cache[str(lot_id)] = (time.monotonic(), dict(context))

    def _forget_lot_caches(self, lot_id: str) -> None:
        self._own_lot_cache.pop(str(lot_id), None)
        self._price_update_context_cache.pop(str(lot_id), None)

    async def _send_price_update_request(
        self,
        *,
        method: str,
        url: str,
        payload: dict[str, Any],
        content_type: str,
    ) -> httpx.Response:
        normalized_content_type = content_type.strip().lower()
        if normalized_content_type == "form":
            return await self._request(
                method,
                url,
                request_type="price_update",
                data=_form_payload(payload),
            )
        return await self._request(
            method,
            url,
            request_type="price_update",
            json=payload,
        )

    def _ensure_price_write_allowed(
        self,
        lot_id: str | None,
        *,
        allow_real_write: bool | None,
    ) -> None:
        if not lot_id:
            raise StarvellEndpointNotConfiguredError("Не найден ID лота для изменения цены")

        real_write_allowed = (not self.settings.dry_run) if allow_real_write is None else allow_real_write
        if not real_write_allowed:
            raise StarvellWriteDisabledError(
                "Реальное изменение цены отключено: включен режим только анализа"
            )
        if not self.settings.enable_real_price_writes:
            raise StarvellWriteDisabledError(
                "Реальное изменение цены отключено: ENABLE_REAL_PRICE_WRITES=false"
            )
        if not self.settings.market_update_lot_price_url:
            raise StarvellEndpointNotConfiguredError(
                "Реальное изменение цены отключено: не настроен MARKET_UPDATE_LOT_PRICE_URL"
            )

    def _log_price_update_attempt(self, attempt: PriceUpdateAttemptResult) -> None:
        self.logger.warning(
            "price_update_debug_attempt",
            variant=attempt.variant,
            method=attempt.method,
            url=attempt.url,
            content_type=attempt.request_content_type,
            payload=attempt.payload,
            status_code=attempt.status_code,
            response_content_type=attempt.response_content_type,
            response_headers=attempt.response_headers,
            response_body=attempt.response_body,
            success=attempt.success,
            reason=attempt.reason,
            proxy=self.proxy_profile or "direct",
        )

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


def safe_starvell_error_reason(exc: Exception) -> str:
    if isinstance(exc, StarvellWriteDisabledError):
        return "real_price_writes_disabled"
    if isinstance(exc, StarvellPayloadStyleError):
        return "price_update_payload_unknown"
    if isinstance(exc, StarvellEndpointNotConfiguredError):
        return "price_update_endpoint_missing"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code == 401:
            return "unauthorized"
        if status_code == 403:
            return "forbidden"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "starvell_server_error"
        return f"http_{status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if _looks_like_proxy_transport_error(exc):
        normalized = _exception_chain_text(exc).lower()
        if "malformed reply" in normalized:
            return "proxy_malformed_reply"
        if isinstance(exc, httpx.ConnectError) or "connecterror" in normalized:
            return "proxy_connect_error"
        if isinstance(exc, httpx.ProxyError) or "proxy" in normalized or "socks" in normalized:
            return "proxy_error"
        return "network_error"
    reason = str(exc).strip()
    return reason or type(exc).__name__


def _looks_like_proxy_transport_error(exc: Exception) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    normalized = _exception_chain_text(exc).lower()
    markers = (
        "socks",
        "malformed reply",
        "proxy",
        "clientconnectorerror",
        "connecterror",
        "networkerror",
        "protocolerror",
        "server disconnected",
    )
    return any(marker in normalized for marker in markers)


def _exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(
            f"{type(current).__module__}.{type(current).__name__}: {str(current)}"
        )
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _format_price_update_url(
    template: str,
    *,
    lot_id: str,
    position_amount: int,
) -> str:
    lot_id_part = quote(str(lot_id), safe="")
    position_part = quote(str(position_amount), safe="")
    return (
        template.strip()
        .replace("{lot_id}", lot_id_part)
        .replace("{listing_id}", lot_id_part)
        .replace("{position_amount}", position_part)
    )


def _price_update_payload(
    price: Decimal,
    *,
    style: str,
    lot_id: str | None = None,
    position_amount: int | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = style.strip().lower()
    if normalized == "partial_update":
        return _partial_update_payload(price, context=context or {})
    if normalized == "bulk":
        return _bulk_update_payload(price, lot_id=lot_id, position_amount=position_amount)

    field_by_style = {
        "auto": "price",
        "price": "price",
        "amount": "amount",
        "cost": "cost",
        "offer_price": "offer_price",
        "new_price": "new_price",
        "price_value": "price_value",
    }
    string_field_by_style = {
        "price_string": "price",
        "amount_string": "amount",
    }
    if normalized in string_field_by_style:
        return {string_field_by_style[normalized]: _string_price_value(price)}

    field_name = field_by_style.get(normalized)
    if field_name is None:
        raise StarvellPayloadStyleError(
            "Неизвестный payload изменения цены. Нужно посмотреть Network."
        )
    return {field_name: _json_price_value(price)}


def _price_update_style_needs_context(style: str) -> bool:
    return style.strip().lower() == "partial_update"


def _should_forget_price_update_context(status_code: int) -> bool:
    return status_code in {400, 404, 409, 422}


def _partial_update_payload(price: Decimal, *, context: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "availability": _context_int(context, ("availability", "stock", "quantity")),
        "price": _string_price_value(price),
        "minOrderCurrencyAmount": _context_optional_number(
            context,
            ("minOrderCurrencyAmount", "min_order_currency_amount", "minOrderAmount"),
        ),
        "isActive": _context_bool(context, ("isActive", "is_active", "active"), default=True),
    }
    if _context_bool(context, ("instantDelivery", "instant_delivery"), default=False):
        payload["availability"] = None
    if payload["availability"] is None:
        payload.pop("availability")
    return payload


def _bulk_update_payload(
    price: Decimal,
    *,
    lot_id: str | None,
    position_amount: int | None,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "lot_id": _numeric_string_or_text(lot_id),
        "price": _json_price_value(price),
    }
    if position_amount:
        item["position_amount"] = position_amount
    return {"offers": [item]}


def _json_price_value(price: Decimal) -> int | float:
    normalized = price.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral_value():
        return int(normalized)
    return float(normalized)


def _string_price_value(price: Decimal) -> str:
    normalized = price.quantize(Decimal("0.01"))
    if normalized == normalized.to_integral_value():
        return str(int(normalized))
    return format(normalized, "f")


def _price_update_payload_candidates(
    price: Decimal,
    *,
    context: dict[str, Any] | None = None,
    lot_id: str | None = None,
    position_amount: int | None = None,
) -> list[PriceUpdatePayloadCandidate]:
    base_payloads = [
        ("price_number", {"price": _json_price_value(price)}),
        ("amount_number", {"amount": _json_price_value(price)}),
        ("cost_number", {"cost": _json_price_value(price)}),
        ("offer_price_number", {"offer_price": _json_price_value(price)}),
        ("new_price_number", {"new_price": _json_price_value(price)}),
        ("price_value_number", {"price_value": _json_price_value(price)}),
        ("price_string", {"price": _string_price_value(price)}),
        ("amount_string", {"amount": _string_price_value(price)}),
    ]
    clean_context = _clean_price_update_context(context or {})
    candidates: list[PriceUpdatePayloadCandidate] = []
    if clean_context:
        candidates.append(
            PriceUpdatePayloadCandidate(
                name="partial_update_from_my_offers_page",
                payload=_partial_update_payload(price, context=clean_context),
            )
        )
    candidates.append(
        PriceUpdatePayloadCandidate(
            name="bulk_offers_array",
            payload=_bulk_update_payload(
                price,
                lot_id=lot_id,
                position_amount=position_amount,
            ),
        )
    )
    candidates.extend(
        PriceUpdatePayloadCandidate(name=name, payload=payload)
        for name, payload in base_payloads
    )
    if clean_context:
        for name, payload in base_payloads:
            candidates.append(
                PriceUpdatePayloadCandidate(
                    name=f"{name}_with_offer_context",
                    payload={**clean_context, **payload},
                )
            )
    return candidates


def _clean_price_update_context(context: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "offer_id",
        "offerId",
        "currency",
        "category_id",
        "categoryId",
        "sub_category_id",
        "subCategoryId",
        "seller_id",
        "sellerId",
        "availability",
        "stock",
        "quantity",
        "isActive",
        "is_active",
        "active",
        "instantDelivery",
        "instant_delivery",
        "minOrderCurrencyAmount",
        "min_order_currency_amount",
        "minOrderAmount",
        "csrf",
        "_csrf",
    }
    clean: dict[str, Any] = {}
    for key in allowed_keys:
        value = context.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (str, int, float, bool)):
            clean[key] = value
    return clean


def _form_payload(payload: dict[str, Any]) -> dict[str, str]:
    form: dict[str, str] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, bool):
            form[key] = "true" if value else "false"
        else:
            form[key] = str(value)
    return form


def _request_content_type_label(content_type: str) -> str:
    if content_type.strip().lower() == "form":
        return "application/x-www-form-urlencoded"
    return "application/json"


def _extract_offer_payload_from_html(html: str, *, lot_id: str) -> dict[str, Any] | None:
    page_props = _extract_next_page_props(html)
    offer = page_props.get("offer")
    if isinstance(offer, dict) and _payload_matches_lot_id(offer, lot_id):
        return offer

    for item in _find_embedded_lot_items(html_module.unescape(html).replace("\\/", "/")):
        if _payload_matches_lot_id(item, lot_id):
            return item
    return None


def _payload_matches_lot_id(payload: dict[str, Any], lot_id: str) -> bool:
    parsed_lot_id = _find_direct_string(
        payload,
        ("id", "lot_id", "lotId", "listing_id", "listingId", "offer_id", "offerId"),
    )
    return parsed_lot_id is None or str(parsed_lot_id) == str(lot_id)


def _price_update_context_from_offer_payload(payload: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    lot_id = _find_direct_string(
        payload,
        ("id", "lot_id", "lotId", "listing_id", "listingId", "offer_id", "offerId"),
    )
    if lot_id:
        context["id"] = lot_id
        context["offer_id"] = lot_id
        context["offerId"] = lot_id

    _set_context_value(context, "currency", _find_direct_string(payload, ("currency",)))
    _set_context_value(context, "isActive", _find_first_bool(payload, ("isActive", "is_active", "active")))
    _set_context_value(
        context,
        "instantDelivery",
        _find_first_bool(payload, ("instantDelivery", "instant_delivery")),
    )
    _set_context_value(
        context,
        "minOrderCurrencyAmount",
        _find_direct_decimal(
            payload,
            ("minOrderCurrencyAmount", "min_order_currency_amount", "minOrderAmount"),
        ),
    )
    _set_context_value(
        context,
        "availability",
        _find_direct_int(payload, ("availability", "stock", "quantity")),
    )
    _set_context_value(context, "stock", _find_direct_int(payload, ("stock",)))
    _set_context_value(context, "quantity", _find_direct_int(payload, ("quantity",)))

    category_id = _find_direct_string(payload, ("category_id", "categoryId"))
    category = payload.get("category")
    if not category_id and isinstance(category, dict):
        category_id = _find_direct_string(category, ("id", "category_id", "categoryId"))
    if category_id:
        context["category_id"] = category_id
        context["categoryId"] = category_id

    sub_category_id = _find_direct_string(
        payload,
        ("sub_category_id", "subCategoryId", "subcategory_id", "subcategoryId"),
    )
    sub_category = payload.get("subCategory") or payload.get("subcategory")
    if not sub_category_id and isinstance(sub_category, dict):
        sub_category_id = _find_direct_string(
            sub_category,
            ("id", "sub_category_id", "subCategoryId", "subcategory_id", "subcategoryId"),
        )
    if sub_category_id:
        context["sub_category_id"] = sub_category_id
        context["subCategoryId"] = sub_category_id

    seller_id = _find_direct_string(
        payload,
        ("seller_id", "sellerId", "seller_user_id", "sellerUserId", "user_id", "userId"),
    )
    user = payload.get("user")
    if not seller_id and isinstance(user, dict):
        seller_id = _find_direct_string(
            user,
            ("id", "seller_id", "sellerId", "user_id", "userId"),
        )
    if seller_id:
        context["seller_id"] = seller_id
        context["sellerId"] = seller_id

    return context


def _set_context_value(context: dict[str, Any], key: str, value: Any) -> None:
    if value is not None and value != "":
        context[key] = value


def _context_int(context: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = context.get(key)
        if value is None or value == "":
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _context_bool(context: dict[str, Any], keys: tuple[str, ...], *, default: bool) -> bool:
    for key in keys:
        value = context.get(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "1", "yes", "active", "enabled"}:
                return True
            if normalized in {"false", "0", "no", "inactive", "disabled"}:
                return False
        if isinstance(value, int):
            return value != 0
    return default


def _context_optional_number(context: dict[str, Any], keys: tuple[str, ...]) -> int | float | None:
    for key in keys:
        value = context.get(key)
        if value is None or value == "":
            continue
        try:
            decimal_value = Decimal(str(value))
        except Exception:
            continue
        return _json_price_value(decimal_value)
    return None


def _numeric_string_or_text(value: str | None) -> int | str | None:
    if value is None:
        return None
    text = str(value)
    return int(text) if text.isdigit() else text


def _price_update_attempt_result(
    *,
    candidate: PriceUpdatePayloadCandidate,
    method: str,
    url: str,
    content_type: str,
    response: httpx.Response,
    settings: Settings,
    reason: str | None,
) -> PriceUpdateAttemptResult:
    return PriceUpdateAttemptResult(
        variant=candidate.name,
        method=method,
        url=url,
        request_content_type=_request_content_type_label(content_type),
        payload=_sanitize_payload(candidate.payload),
        status_code=response.status_code,
        response_content_type=response.headers.get("content-type", ""),
        response_headers=_safe_response_headers(response.headers),
        response_body=_safe_response_body(response.text, settings),
        success=response.status_code < 400,
        reason=reason,
    )


def _debug_response_fields(response: httpx.Response, settings: Settings) -> dict[str, Any]:
    return {
        "response_content_type": response.headers.get("content-type", ""),
        "response_headers": _safe_response_headers(response.headers),
        "response_body": _safe_response_body(response.text, settings),
    }


def _safe_response_headers(headers: httpx.Headers) -> dict[str, str]:
    unsafe_names = {
        "set-cookie",
        "cookie",
        "authorization",
        "proxy-authorization",
        "x-csrf-token",
        "csrf-token",
    }
    safe: dict[str, str] = {}
    for key, value in headers.items():
        normalized = key.lower()
        if normalized in unsafe_names or any(part in normalized for part in ("token", "secret")):
            safe[key] = "***"
        else:
            safe[key] = value
    return safe


def _safe_response_body(text: str, settings: Settings, *, max_chars: int = 20000) -> str:
    sanitized = _sanitize_text(text, settings)
    if len(sanitized) <= max_chars:
        return sanitized
    return sanitized[:max_chars] + "\n... [response body truncated]"


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: _sanitize_value(key, value)
        for key, value in payload.items()
    }


def _sanitize_value(key: str, value: Any) -> Any:
    lowered = key.lower()
    if any(marker in lowered for marker in ("cookie", "session", "csrf", "token", "secret", "password")):
        return "***"
    if isinstance(value, dict):
        return _sanitize_payload(value)
    if isinstance(value, list):
        return [_sanitize_value(key, item) for item in value]
    return value


def _sanitize_text(text: str, settings: Settings) -> str:
    sanitized = text
    secret_values = [
        settings.market_session_cookie,
        settings.market_csrf_token,
        settings.market_api_token,
    ]
    for secret in secret_values:
        if secret:
            sanitized = sanitized.replace(secret, "***")
    return sanitized


def _error_kind_from_status(status_code: int) -> str | None:
    if status_code == 429:
        return "429"
    if status_code == 403:
        return "403"
    if status_code == 401:
        return "401"
    if status_code >= 500:
        return "5xx"
    return None


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
