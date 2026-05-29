from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from scripts.price_watcher import (
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
