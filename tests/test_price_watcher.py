from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from scripts.price_watcher import (
    PriceWatcher,
    extract_next_build_id,
    extract_offer_price,
    fetch_top_competitor_offer_ids,
)


def test_price_watcher_extracts_next_build_id() -> None:
    html = (
        '<script id="__NEXT_DATA__" type="application/json">'
        '{"buildId":"build-123","props":{"pageProps":{}}}'
        "</script>"
    )

    assert extract_next_build_id(html) == "build-123"


def test_price_watcher_extracts_offer_price_by_offer_id() -> None:
    payload = {
        "props": {
            "pageProps": {
                "offer": {
                    "id": 222760,
                    "price": "306.30000",
                }
            }
        }
    }

    assert extract_offer_price(payload, offer_id="222760") == Decimal("306.30000")


@pytest.mark.asyncio
async def test_price_watcher_fetches_top_competitor_offer_ids() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/offers/list-by-category"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 1,
                        "price": "300",
                        "availability": 10,
                        "subCategory": {"id": 333, "name": "500 робуксов"},
                        "user": {"id": "own", "username": "me"},
                    },
                    {
                        "id": 2,
                        "price": "301",
                        "availability": 10,
                        "subCategory": {"id": 333, "name": "500 робуксов"},
                        "user": {"id": "seller-2", "username": "seller2"},
                    },
                    {
                        "id": 3,
                        "price": "302",
                        "availability": 10,
                        "subCategory": {"id": 333, "name": "500 робуксов"},
                        "user": {"id": "seller-3", "username": "seller3"},
                    },
                ]
            },
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        own_seller_id="own",
    )

    try:
        offer_ids = await fetch_top_competitor_offer_ids(
            client,
            settings=settings,
            position_amount=500,
            limit=2,
        )
    finally:
        await client.aclose()

    assert offer_ids == ("2", "3")


@pytest.mark.asyncio
async def test_price_watcher_publishes_json_event_payload(monkeypatch) -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int, str]] = []

        async def setex(self, key: str, ttl: int, value: str) -> None:
            self.calls.append((key, ttl, value))

    async def fake_fetch_price(offer_id: str) -> Decimal:
        assert offer_id == "222760"
        return Decimal("306.30")

    monkeypatch.setattr("scripts.price_watcher.time.time", lambda: 100.123)
    redis = FakeRedis()
    watcher = PriceWatcher(
        settings=Settings(_env_file=None),
        redis=redis,
        client=None,
    )
    watcher.last_prices[(500, "222760")] = Decimal("306.00")
    monkeypatch.setattr(watcher, "_fetch_offer_price_with_build_refresh", fake_fetch_price)

    await watcher._poll_offer(500, "222760")

    assert redis.calls
    key, ttl, value = redis.calls[0]
    assert key == "repricer:price_change_event:500"
    assert ttl == 5
    assert '"offer_id":"222760"' in value
    assert '"old_price":"306.00"' in value
    assert '"new_price":"306.30"' in value
    assert '"detected_at_ms":100123' in value
