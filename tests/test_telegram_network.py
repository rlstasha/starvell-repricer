import socket

import pytest
from aiogram.client.session.aiohttp import AiohttpSession

from app.bot.telegram_network import IPv4AiohttpSession, TELEGRAM_API_HOST, build_telegram_session
from app.check_telegram_connectivity import _select_telegram_ipv4
from app.core.config import Settings


def test_build_telegram_session_forces_ipv4_by_default() -> None:
    settings = Settings(_env_file=None, telegram_api_ipv4="149.154.167.220")

    session = build_telegram_session(settings)

    assert isinstance(session, IPv4AiohttpSession)
    assert session._connector_init["family"] == socket.AF_INET
    assert "resolver" in session._connector_init


def test_build_telegram_session_can_use_default_aiogram_session() -> None:
    settings = Settings(_env_file=None, telegram_force_ipv4=False)

    session = build_telegram_session(settings)

    assert type(session) is AiohttpSession


@pytest.mark.asyncio
async def test_ipv4_resolver_uses_configured_telegram_ip() -> None:
    settings = Settings(_env_file=None, telegram_api_ipv4="149.154.167.220")
    session = build_telegram_session(settings)
    resolver = session._connector_init["resolver"]

    result = await resolver.resolve(TELEGRAM_API_HOST, 443)
    await resolver.close()

    assert result == [
        {
            "hostname": TELEGRAM_API_HOST,
            "host": "149.154.167.220",
            "port": 443,
            "family": socket.AF_INET,
            "proto": socket.IPPROTO_TCP,
            "flags": socket.AI_NUMERICHOST,
        }
    ]


def test_telegram_healthcheck_prefers_configured_ipv4(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_API_IPV4", "149.154.167.220")

    assert _select_telegram_ipv4() == "149.154.167.220"
