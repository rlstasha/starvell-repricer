"""Optional read-only competitor price watcher.

This process is intentionally separate from the repricer worker. It never writes
prices and never uses the repricer rate limiter; it only publishes short-lived
Redis hints that let the scheduler refresh a position immediately.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.market.client import (
    STARVELL_ROBUX_SUBCATEGORY_IDS,
    _extract_offer_payload_items,
    _market_offers_api_payload,
    parse_starvell_market_offers_payload,
    safe_starvell_error_reason,
)


NEXT_DATA_RE = re.compile(
    r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(?P<payload>.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
BUILD_ID_RE = re.compile(r'"buildId"\s*:\s*"(?P<build_id>[^"]+)"')
PRICE_KEYS = ("price", "cost", "amount_price", "amountPrice")
OFFER_ID_KEYS = ("id", "lot_id", "lotId", "offer_id", "offerId")
LOGGER = get_logger(__name__)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.price_watcher_enabled:
        LOGGER.info(
            "price_watcher_disabled",
            positions=list(settings.price_watcher_position_amounts),
            interval_ms=settings.price_watcher_interval_ms,
        )
        while True:
            await asyncio.sleep(3600)

    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with httpx.AsyncClient(
            base_url=settings.market_base_url,
            timeout=httpx.Timeout(5.0),
            headers=_headers(settings),
            cookies=_cookies(settings),
            limits=httpx.Limits(max_connections=30, max_keepalive_connections=10),
        ) as client:
            watcher = PriceWatcher(settings=settings, redis=redis, client=client)
            await watcher.run_forever()
    finally:
        await redis.aclose()


class PriceWatcher:
    def __init__(self, *, settings: Settings, redis: Redis, client: httpx.AsyncClient):
        self.settings = settings
        self.redis = redis
        self.client = client
        self.build_id: str | None = None
        self.competitor_ids: dict[int, tuple[str, ...]] = {}
        self.last_prices: dict[tuple[int, str], Decimal] = {}
        self.last_competitor_refresh_monotonic = 0.0
        self.logger = LOGGER

    async def run_forever(self) -> None:
        self.logger.info(
            "price_watcher_started",
            positions=list(self.settings.price_watcher_position_amounts),
            interval_ms=self.settings.price_watcher_interval_ms,
            top_competitors=self.settings.price_watcher_top_competitors,
            refresh_seconds=self.settings.price_watcher_competitor_refresh_seconds,
        )
        while True:
            try:
                await self._ensure_build_id()
                await self._refresh_competitors_if_needed(force=not self.competitor_ids)
                await self._poll_competitors_once()
            except Exception as exc:
                self.logger.warning(
                    "price_watcher_cycle_failed",
                    reason=safe_starvell_error_reason(exc),
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(self.settings.price_watcher_interval_ms / 1000)

    async def _ensure_build_id(self) -> str:
        if self.build_id:
            return self.build_id
        self.build_id = await fetch_next_build_id(self.client)
        self.logger.info("price_watcher_build_id_loaded", build_id=self.build_id)
        return self.build_id

    async def _refresh_competitors_if_needed(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if (
            not force
            and now - self.last_competitor_refresh_monotonic
            < self.settings.price_watcher_competitor_refresh_seconds
        ):
            return
        refreshed: dict[int, tuple[str, ...]] = {}
        for position_amount in self.settings.price_watcher_position_amounts:
            offer_ids = await fetch_top_competitor_offer_ids(
                self.client,
                settings=self.settings,
                position_amount=position_amount,
                limit=self.settings.price_watcher_top_competitors,
            )
            if offer_ids:
                refreshed[position_amount] = offer_ids
        self.competitor_ids = refreshed
        self.last_competitor_refresh_monotonic = now
        self.logger.info(
            "price_watcher_competitors_refreshed",
            positions={
                str(position_amount): list(offer_ids)
                for position_amount, offer_ids in sorted(refreshed.items())
            },
        )

    async def _poll_competitors_once(self) -> None:
        tasks = [
            self._poll_offer(position_amount, offer_id)
            for position_amount, offer_ids in self.competitor_ids.items()
            for offer_id in offer_ids
        ]
        if tasks:
            await asyncio.gather(*tasks)

    async def _poll_offer(self, position_amount: int, offer_id: str) -> None:
        price = await self._fetch_offer_price_with_build_refresh(offer_id)
        if price is None:
            return
        cache_key = (position_amount, offer_id)
        previous_price = self.last_prices.get(cache_key)
        self.last_prices[cache_key] = price
        if previous_price is None or previous_price == price:
            return
        redis_key = f"repricer:price_change_event:{position_amount}"
        detected_at_ms = int(time.time() * 1000)
        event_payload = {
            "source": "price_watcher",
            "position_amount": position_amount,
            "offer_id": offer_id,
            "old_price": str(previous_price),
            "new_price": str(price),
            "detected_at_ms": detected_at_ms,
        }
        await self.redis.setex(redis_key, 5, json.dumps(event_payload, separators=(",", ":")))
        self.logger.info(
            "price_watcher_price_change_detected",
            position_amount=position_amount,
            offer_id=offer_id,
            old_price=str(previous_price),
            new_price=str(price),
            detected_at_ms=detected_at_ms,
            redis_key=redis_key,
        )

    async def _fetch_offer_price_with_build_refresh(self, offer_id: str) -> Decimal | None:
        build_id = await self._ensure_build_id()
        status_code, payload = await fetch_offer_next_json(self.client, build_id, offer_id)
        if status_code == 404:
            self.build_id = await fetch_next_build_id(self.client)
            self.logger.info("price_watcher_build_id_reloaded", build_id=self.build_id)
            status_code, payload = await fetch_offer_next_json(self.client, self.build_id, offer_id)
        if status_code >= 400:
            self.logger.warning(
                "price_watcher_offer_fetch_failed",
                offer_id=offer_id,
                status_code=status_code,
            )
            return None
        return extract_offer_price(payload, offer_id=offer_id)


async def fetch_next_build_id(client: httpx.AsyncClient) -> str:
    response = await client.get("/")
    response.raise_for_status()
    build_id = extract_next_build_id(response.text)
    if not build_id:
        raise RuntimeError("Starvell Next.js buildId not found")
    return build_id


def extract_next_build_id(source_html: str) -> str | None:
    match = NEXT_DATA_RE.search(source_html)
    if match:
        payload_text = html.unescape(match.group("payload")).strip()
        try:
            payload = json.loads(payload_text)
        except ValueError:
            payload = {}
        if isinstance(payload, dict) and isinstance(payload.get("buildId"), str):
            return payload["buildId"]
    fallback_match = BUILD_ID_RE.search(source_html)
    return fallback_match.group("build_id") if fallback_match else None


async def fetch_top_competitor_offer_ids(
    client: httpx.AsyncClient,
    *,
    settings: Settings,
    position_amount: int,
    limit: int,
) -> tuple[str, ...]:
    if position_amount not in STARVELL_ROBUX_SUBCATEGORY_IDS:
        return ()
    payload = _market_offers_api_payload(
        position_amount=position_amount,
        limit=min(settings.market_offers_limit, max(limit + 3, 10)),
    )
    response = await client.post(settings.market_offers_api_url, json=payload)
    response.raise_for_status()
    raw_items = _extract_offer_payload_items(response.json())
    offers = parse_starvell_market_offers_payload(raw_items, position_amount=position_amount)
    offer_ids: list[str] = []
    for offer in offers:
        if _is_own_offer(settings, offer.seller_id, offer.seller_username):
            continue
        offer_id = _find_offer_id(offer.raw_payload)
        if not offer_id or offer_id in offer_ids:
            continue
        offer_ids.append(offer_id)
        if len(offer_ids) >= limit:
            break
    return tuple(offer_ids)


async def fetch_offer_next_json(
    client: httpx.AsyncClient,
    build_id: str,
    offer_id: str,
) -> tuple[int, Any]:
    response = await client.get(f"/_next/data/{build_id}/offers/{offer_id}.json")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def extract_offer_price(payload: Any, *, offer_id: str | None = None) -> Decimal | None:
    offer_payload = _find_offer_payload(payload, offer_id=offer_id)
    if offer_payload is None:
        offer_payload = payload if isinstance(payload, dict) else None
    if not isinstance(offer_payload, dict):
        return None
    return _find_decimal(offer_payload, PRICE_KEYS)


def _headers(settings: Settings) -> dict[str, str]:
    headers = {
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": settings.market_base_url.rstrip("/"),
        "Referer": settings.market_base_url.rstrip("/") + "/",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
        ),
    }
    if settings.market_csrf_token:
        headers["X-CSRF-Token"] = settings.market_csrf_token
    if settings.market_api_token:
        headers["Authorization"] = f"Bearer {settings.market_api_token}"
    return headers


def _cookies(settings: Settings) -> dict[str, str]:
    if not settings.market_session_cookie:
        return {}
    return {"session": settings.market_session_cookie}


def _is_own_offer(settings: Settings, seller_id: str | None, seller_username: str | None) -> bool:
    if settings.own_seller_id and seller_id == settings.own_seller_id:
        return True
    if settings.own_seller_username and seller_username == settings.own_seller_username:
        return True
    return False


def _find_offer_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in OFFER_ID_KEYS:
        value = payload.get(key)
        if value is not None:
            return str(value)
    return None


def _find_offer_payload(payload: Any, *, offer_id: str | None) -> dict[str, Any] | None:
    for item in _walk_dicts(payload):
        if offer_id is not None and _find_offer_id(item) == str(offer_id):
            return item
        if offer_id is None and _find_decimal(item, PRICE_KEYS) is not None:
            return item
    return None


def _walk_dicts(payload: Any) -> Iterable[dict[str, Any]]:
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _walk_dicts(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _walk_dicts(value)


def _find_decimal(payload: dict[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value).replace(",", "."))
        except (InvalidOperation, ValueError):
            continue
    return None


if __name__ == "__main__":
    asyncio.run(main())
