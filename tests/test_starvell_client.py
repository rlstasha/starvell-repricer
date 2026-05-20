import json
from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.market.client import (
    StarvellClient,
    explain_http_status,
    parse_starvell_lots_from_html,
    parse_starvell_market_offers,
    parse_starvell_market_offers_payload,
    parse_starvell_own_lot,
    safe_starvell_error_reason,
)
from app.market.exceptions import StarvellEndpointNotConfiguredError, StarvellWriteDisabledError
from app.repricer.rate_limiter import InMemoryFixedWindowRateLimiter


@pytest.mark.asyncio
async def test_check_connection_does_not_guess_unknown_endpoints() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_account_info_url="",
        market_my_lots_url="",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.check_connection()

    assert result.account_endpoint_configured is False
    assert result.lots_endpoint_configured is False
    assert result.authorized is None


@pytest.mark.asyncio
async def test_get_account_info_uses_safe_get_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/me"
        return httpx.Response(200, json={"seller_id": "4111", "username": "starvell_user"})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_account_info_url="/api/me",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        account = await starvell.get_account_info()

    assert account.seller_id == "4111"
    assert account.seller_username == "starvell_user"


@pytest.mark.asyncio
async def test_get_account_info_parses_profile_html() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/users/4111"
        return httpx.Response(
            200,
            content="""
            <html>
              <head><title>starvell_user - Starvell</title></head>
              <body><h1>starvell_user</h1></body>
            </html>
            """.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_account_info_url="https://starvell.example/users/4111",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        account = await starvell.get_account_info()

    assert account.seller_id == "4111"
    assert account.seller_username == "starvell_user"


@pytest.mark.asyncio
async def test_get_my_lots_uses_safe_get_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/my-lots"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "listing_id": "1999",
                        "title": "400 robux",
                        "robux": 400,
                        "price": "499",
                        "active": True,
                    }
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
        market_my_lots_url="/api/my-lots",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        lots = await starvell.get_my_lots()

    assert len(lots) == 1
    assert lots[0].lot_id == "1999"
    assert lots[0].position_amount == 400
    assert lots[0].price == Decimal("499")
    assert lots[0].is_active is True


def test_parse_starvell_lots_from_profile_html_fragment() -> None:
    html = """
    <section class="profile-offers">
      <article>
        <a href="/offers/1996"><span>80 робуксов</span></a>
        <div>наличие 927</div>
        <strong>92.67 ₽</strong>
      </article>
      <article>
        <a href="/offers/2012">22500 робуксов</a>
        <span>наличие 2</span>
        <span>25000,50 ₽</span>
      </article>
    </section>
    """

    lots = parse_starvell_lots_from_html(html, default_seller_id="4111")

    assert len(lots) == 2
    assert lots[0].lot_id == "1996"
    assert lots[0].title == "80 робуксов"
    assert lots[0].position_amount == 80
    assert lots[0].stock == 927
    assert lots[0].price == Decimal("92.67")
    assert lots[0].seller_id == "4111"
    assert lots[1].lot_id == "2012"
    assert lots[1].position_amount == 22500
    assert lots[1].price == Decimal("25000.50")


def test_parse_starvell_lots_from_next_data_html() -> None:
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "bff": {
            "userProfileOffers": [
              {
                "id": 1996,
                "name": "80 робуксов",
                "amount": 80,
                "availability": 927,
                "price": "92.67",
                "sellerId": 4111
              }
            ]
          }
        }
      }
    }
    </script>
    """

    lots = parse_starvell_lots_from_html(html)

    assert len(lots) == 1
    assert lots[0].lot_id == "1996"
    assert lots[0].title == "80 робуксов"
    assert lots[0].position_amount == 80
    assert lots[0].stock == 927
    assert lots[0].price == Decimal("92.67")
    assert lots[0].seller_id == "4111"


@pytest.mark.asyncio
async def test_get_my_lots_parses_html_with_safe_get() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/users/4111"
        return httpx.Response(
            200,
            content="""
            <html>
              <body>
                <a href="/offers/1998">200 робуксов</a>
                <span>наличие 16</span>
                <span>220 ₽</span>
              </body>
            </html>
            """.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_my_lots_url="https://starvell.example/users/4111",
        own_seller_id="4111",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        lots = await starvell.get_my_lots()

    assert len(lots) == 1
    assert lots[0].lot_id == "1998"
    assert lots[0].title == "200 робуксов"
    assert lots[0].position_amount == 200
    assert lots[0].stock == 16
    assert lots[0].price == Decimal("220")
    assert lots[0].seller_id == "4111"


def test_parse_starvell_market_offers_from_category_html() -> None:
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "offers": [
            {
              "id": 213225,
              "type": "LOT",
              "price": "75.70000",
              "availability": 120,
              "subCategory": {"id": 65, "name": "80 робуксов"},
              "user": {
                "id": 70238,
                "username": "psorias",
                "rating": 5,
                "reviewsCount": 448
              }
            },
            {
              "id": 1999,
              "type": "LOT",
              "price": "334.30000",
              "availability": 7799,
              "subCategory": {"id": 67, "name": "400 робуксов"},
              "user": {
                "id": 4111,
                "username": "zoomplex",
                "rating": 5
              }
            }
          ]
        }
      }
    }
    </script>
    """

    offers = parse_starvell_market_offers(html, position_amount=80)

    assert len(offers) == 1
    assert offers[0].position_amount == 80
    assert offers[0].price == Decimal("75.70000")
    assert offers[0].seller_id == "70238"
    assert offers[0].seller_username == "psorias"
    assert offers[0].rating == Decimal("5")
    assert offers[0].is_active is True


def test_parse_starvell_market_offers_from_api_payload() -> None:
    payload = [
        {
            "id": 153402,
            "type": "LOT",
            "price": "551.80000",
            "availability": 50000,
            "subCategory": {"id": 69, "name": "800 робуксов"},
            "user": {"id": 132166, "username": "bob2minion", "rating": 5},
        },
        {
            "id": 213225,
            "type": "LOT",
            "price": "73.90000",
            "availability": 20,
            "subCategory": {"id": 65, "name": "80 робуксов"},
            "user": {"id": 70238, "username": "psorias", "rating": 5},
        },
    ]

    offers = parse_starvell_market_offers_payload(payload, position_amount=800)

    assert len(offers) == 1
    assert offers[0].position_amount == 800
    assert offers[0].price == Decimal("551.80000")
    assert offers[0].seller_id == "132166"
    assert offers[0].seller_username == "bob2minion"


def test_parse_starvell_own_lot_from_offer_html() -> None:
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "pageProps": {
        "offer": {
          "id": 1996,
          "type": "LOT",
          "price": "79.30000",
          "availability": 2926,
          "subCategory": {"id": 65, "name": "80 робуксов"},
          "userId": 4111
        },
        "seller": {"id": 4111, "username": "zoomplex"}
      }
    }
    </script>
    """

    lot = parse_starvell_own_lot(html, position_amount=80, lot_id="1996")

    assert lot is not None
    assert lot.position_amount == 80
    assert lot.lot_id == "1996"
    assert lot.price == Decimal("79.30000")


@pytest.mark.asyncio
async def test_get_market_offers_uses_read_only_api_category_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/offers/list-by-category"
        payload = json.loads(request.content)
        assert payload["categoryId"] == 40
        assert payload["subCategoryId"] == 65
        return httpx.Response(
            200,
            json=[
                {
                    "id": 213225,
                    "price": "75.70000",
                    "availability": 120,
                    "subCategory": {"name": "80 робуксов"},
                    "user": {"id": 70238, "username": "psorias", "rating": 5},
                }
            ],
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_offers_api_url="/api/offers/list-by-category",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.get_market_offers_result(80, "1996")

    assert result.method == "POST"
    assert result.url == "/api/offers/list-by-category"
    assert result.raw_offer_count == 1
    assert len(result.offers) == 1
    assert result.offers[0].price == Decimal("75.70000")


@pytest.mark.asyncio
async def test_get_my_lot_uses_safe_get_offer_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/offers/1996"
        return httpx.Response(
            200,
            content="""
            <script id="__NEXT_DATA__" type="application/json">
            {
              "props": {
                "pageProps": {
                  "offer": {
                    "id": 1996,
                    "price": "79.30000",
                    "availability": 2926,
                    "subCategory": {"name": "80 робуксов"}
                  }
                }
              }
            }
            </script>
            """.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        lot = await starvell.get_my_lot(80, "1996")

    assert lot is not None
    assert lot.price == Decimal("79.30000")


@pytest.mark.asyncio
async def test_get_my_lot_reuses_short_lived_cache() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.path == "/offers/1996"
        return httpx.Response(
            200,
            content="""
            <script id="__NEXT_DATA__" type="application/json">
            {
              "props": {
                "pageProps": {
                  "offer": {
                    "id": 1996,
                    "price": "79.30000",
                    "availability": 2926,
                    "subCategory": {"name": "80 робуксов"}
                  }
                }
              }
            }
            </script>
            """.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        my_lot_state_cache_ttl_seconds=10,
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        first = await starvell.get_my_lot(80, "1996")
        second = await starvell.get_my_lot(80, "1996")

    assert first is not None
    assert second is not None
    assert second.price == Decimal("79.30000")
    assert [request.url.path for request in requests] == ["/offers/1996"]


@pytest.mark.asyncio
async def test_successful_price_update_refreshes_my_lot_cache() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == "/api/offers/2000/price"
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/price",
        market_update_price_payload_style="price",
        my_lot_state_cache_ttl_seconds=10,
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )
        lot = await starvell.get_my_lot(500, "2000")

    assert lot is not None
    assert lot.price == Decimal("123")
    assert [request.url.path for request in requests] == ["/api/offers/2000/price"]


@pytest.mark.asyncio
async def test_update_my_lot_price_blocks_when_real_writes_disabled() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=False,
        market_update_lot_price_url="/api/offers/{lot_id}/price",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        with pytest.raises(StarvellWriteDisabledError):
            await starvell.update_my_lot_price(500, "2000", Decimal("123"), allow_real_write=True)


@pytest.mark.asyncio
async def test_update_my_lot_price_requires_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected request: {request.url}")

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        with pytest.raises(StarvellEndpointNotConfiguredError):
            await starvell.update_my_lot_price(500, "2000", Decimal("123"), allow_real_write=True)


@pytest.mark.asyncio
async def test_update_my_lot_price_uses_configured_write_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/offers/2000/price"
        payload = json.loads(request.content)
        assert payload == {"price": 123.45}
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/price",
        market_update_lot_price_method="PATCH",
        market_update_price_payload_style="price",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123.45"),
            allow_real_write=True,
        )

    assert result.success is True
    assert result.raw_payload == {"ok": True}


@pytest.mark.asyncio
async def test_update_my_lot_price_can_send_form_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/offers/2000/update"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        assert request.content == b"offer_price=123"
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/update",
        market_update_price_payload_style="offer_price",
        market_update_price_content_type="form",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    assert result.success is True


@pytest.mark.asyncio
async def test_update_my_lot_price_can_send_partial_update_payload_from_offer_page() -> None:
    requests: list[httpx.Request] = []
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "offer": {
            "id": 2000,
            "availability": 927,
            "price": "339.90",
            "minOrderCurrencyAmount": null,
            "isActive": true,
            "instantDelivery": false
          }
        }
      }
    }
    </script>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        assert request.method == "POST"
        assert request.url.path == "/api/offers/2000/partial-update"
        payload = json.loads(request.content)
        assert payload == {
            "availability": 927,
            "price": "123",
            "minOrderCurrencyAmount": None,
            "isActive": True,
        }
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/partial-update",
        market_update_price_payload_style="partial_update",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    assert result.success is True
    assert [request.method for request in requests] == ["GET", "GET", "POST"]


@pytest.mark.asyncio
async def test_partial_update_reuses_context_from_cached_my_lot() -> None:
    requests: list[httpx.Request] = []
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "offer": {
            "id": 2000,
            "availability": 927,
            "price": "339.90",
            "minOrderCurrencyAmount": null,
            "isActive": true,
            "instantDelivery": false,
            "subCategory": {"name": "500 робуксов"}
          }
        }
      }
    }
    </script>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.path == "/offers/2000"
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        assert request.method == "POST"
        assert request.url.path == "/api/offers/2000/partial-update"
        payload = json.loads(request.content)
        assert payload == {
            "availability": 927,
            "price": "123",
            "minOrderCurrencyAmount": None,
            "isActive": True,
        }
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/partial-update",
        market_update_price_payload_style="partial_update",
        my_lot_state_cache_ttl_seconds=10,
        price_update_context_cache_ttl_seconds=2,
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        await starvell.get_my_lot(500, "2000")
        result = await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    assert result.success is True
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", "/offers/2000"),
        ("POST", "/api/offers/2000/partial-update"),
    ]


@pytest.mark.asyncio
async def test_update_my_lot_price_can_send_bulk_payload() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/offers/bulk-update"
        payload = json.loads(request.content)
        assert payload == {
            "offers": [
                {
                    "lot_id": 2000,
                    "price": 123,
                    "position_amount": 500,
                }
            ]
        }
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/bulk-update",
        market_update_price_payload_style="bulk",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        result = await starvell.update_my_lot_price(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    assert result.success is True


@pytest.mark.asyncio
async def test_debug_price_update_tries_payloads_content_types_and_context() -> None:
    requests: list[httpx.Request] = []
    html = """
    <script id="__NEXT_DATA__" type="application/json">
    {
      "props": {
        "pageProps": {
          "offer": {
            "id": 2000,
            "availability": 927,
            "currency": "RUB",
            "category": {"id": 40},
            "subCategory": {"id": 333},
            "user": {"id": 4111}
          }
        }
      }
    }
    </script>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, text=html, headers={"content-type": "text/html"})

        if (
            request.headers["content-type"].startswith("application/x-www-form-urlencoded")
            and b"availability=927" in request.content
            and b"price=123" in request.content
            and b"isActive=true" in request.content
        ):
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(
            400,
            text='{"message":"validation failed"}',
            headers={"content-type": "application/json"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/update",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        attempts = await starvell.debug_my_lot_price_update(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    write_requests = [request for request in requests if request.method == "POST"]
    assert any(
        request.headers["content-type"].startswith("application/json")
        for request in write_requests
    )
    assert any(
        request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        for request in write_requests
    )
    assert attempts[-1].success is True
    assert attempts[-1].variant == "partial_update_from_my_offers_page"
    assert attempts[-1].request_content_type == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_debug_price_update_sanitizes_response_headers_and_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text="<html></html>")
        return httpx.Response(
            400,
            text="session=secret-session validation failed",
            headers={"set-cookie": "session=secret-session", "content-type": "text/plain"},
        )

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(
        _env_file=None,
        market_base_url="https://starvell.example",
        market_session_cookie="secret-session",
        enable_real_price_writes=True,
        market_update_lot_price_url="/api/offers/{lot_id}/update",
    )

    async with StarvellClient(settings, InMemoryFixedWindowRateLimiter(), client) as starvell:
        attempts = await starvell.debug_my_lot_price_update(
            500,
            "2000",
            Decimal("123"),
            allow_real_write=True,
        )

    assert attempts[0].response_headers["set-cookie"] == "***"
    assert "secret-session" not in (attempts[0].response_body or "")
    assert "***" in (attempts[0].response_body or "")


def test_explain_http_status_returns_human_russian_text() -> None:
    assert "не авторизовано" in explain_http_status(401)
    assert "доступ запрещен" in explain_http_status(403)
    assert "ограничил частоту" in explain_http_status(429)
    assert "ошибка на стороне Starvell" in explain_http_status(500)


def test_safe_error_reason_classifies_socks_malformed_reply() -> None:
    exc = httpx.ProxyError("socksio.exceptions.ProtocolError: Malformed reply")

    assert safe_starvell_error_reason(exc) == "proxy_malformed_reply"


@pytest.mark.asyncio
async def test_starvell_client_applies_proxy_backoff_on_transport_error() -> None:
    class RecordingLimiter:
        def __init__(self) -> None:
            self.backoffs: list[str | None] = []

        async def acquire(self) -> None:
            return None

        def apply_backoff(self, error_kind: str | None = None) -> float:
            self.backoffs.append(error_kind)
            return 1.0

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ProxyError("Malformed reply")

    client = httpx.AsyncClient(
        base_url="https://starvell.example",
        transport=httpx.MockTransport(handler),
    )
    settings = Settings(_env_file=None, market_base_url="https://starvell.example")
    limiter = RecordingLimiter()

    async with StarvellClient(
        settings,
        limiter,
        client,
        proxy_profile="fast_1",
        proxy_url="socks5://login:password@1.2.3.4:1080",
    ) as starvell:
        with pytest.raises(httpx.ProxyError):
            await starvell.fetch_text("/api/test", request_type="test")

    assert limiter.backoffs == ["proxy", "proxy"]
