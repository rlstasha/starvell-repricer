from datetime import UTC, datetime

import pytest

from app.repricer.socket_listener import (
    SOCKET_STATUS_KEY,
    SocketStatus,
    extract_socket_event_refs,
    load_socket_status,
    sanitize,
)


class FakeRedis:
    def __init__(self, data: dict[str, str] | None = None):
        self.data = data or {}

    async def get(self, key: str):
        return self.data.get(key)


def test_extract_socket_event_refs_reads_offer_links_and_titles() -> None:
    refs = extract_socket_event_refs(
        {
            "href": "/offers/2000",
            "title": "500 робуксов",
            "nested": {"offer_id": "1996", "name": "80 robux"},
        }
    )

    assert refs.lot_ids == ("1996", "2000")
    assert refs.position_amounts == (80, 500)


def test_extract_socket_event_refs_accepts_offer_like_id() -> None:
    refs = extract_socket_event_refs({"id": 2004, "price": 123, "sellerId": 4111})

    assert refs.lot_ids == ("2004",)


def test_sanitize_hides_secrets() -> None:
    assert sanitize({"cookie": "secret", "payload": {"token": "secret", "value": 1}}) == {
        "cookie": "***",
        "payload": {"token": "***", "value": 1},
    }


@pytest.mark.asyncio
async def test_load_socket_status_from_redis() -> None:
    redis = FakeRedis(
        {
            SOCKET_STATUS_KEY: (
                '{"enabled":true,"connected":true,"namespace":"/viewed-offers",'
                '"last_event_at":"2026-05-21T00:00:00+00:00","events_seen":2,'
                '"fallback_active":false,"last_error":null,'
                '"updated_at":"2026-05-21T00:00:01+00:00"}'
            )
        }
    )

    status = await load_socket_status(redis)  # type: ignore[arg-type]

    assert isinstance(status, SocketStatus)
    assert status.enabled is True
    assert status.connected is True
    assert status.namespace == "/viewed-offers"
    assert status.last_event_at == datetime(2026, 5, 21, 0, 0, tzinfo=UTC)
    assert status.events_seen == 2
    assert status.fallback_active is False


@pytest.mark.asyncio
async def test_load_socket_status_defaults_when_missing() -> None:
    status = await load_socket_status(FakeRedis())  # type: ignore[arg-type]

    assert status.enabled is False
    assert status.connected is False
    assert status.fallback_active is True
