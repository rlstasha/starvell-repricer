from decimal import Decimal

import httpx
import pytest

from app.core.config import Settings
from app.market.client import StarvellClient, explain_http_status
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


def test_explain_http_status_returns_human_russian_text() -> None:
    assert "не авторизовано" in explain_http_status(401)
    assert "доступ запрещен" in explain_http_status(403)
    assert "ограничил частоту" in explain_http_status(429)
    assert "ошибка на стороне Starvell" in explain_http_status(500)
