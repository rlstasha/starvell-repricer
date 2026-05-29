from __future__ import annotations

import socket
from typing import Any

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from aiogram.client.session.aiohttp import AiohttpSession

from app.core.config import Settings


TELEGRAM_API_HOST = "api.telegram.org"


class TelegramIPv4Resolver(AbstractResolver):
    """Resolve Telegram Bot API to IPv4, optionally using a pinned address."""

    def __init__(self, telegram_api_ipv4: str | None = None) -> None:
        self.telegram_api_ipv4 = (telegram_api_ipv4 or "").strip()
        self._default_resolver: DefaultResolver | None = None

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: int = socket.AF_INET,
    ) -> list[dict[str, Any]]:
        if host == TELEGRAM_API_HOST and self.telegram_api_ipv4:
            return [
                {
                    "hostname": host,
                    "host": self.telegram_api_ipv4,
                    "port": port,
                    "family": socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": socket.AI_NUMERICHOST,
                }
            ]
        if self._default_resolver is None:
            self._default_resolver = DefaultResolver()
        return await self._default_resolver.resolve(host, port, family=socket.AF_INET)

    async def close(self) -> None:
        if self._default_resolver is not None:
            await self._default_resolver.close()


class IPv4AiohttpSession(AiohttpSession):
    """Aiogram session that never opens IPv6 sockets for Telegram API calls."""

    def __init__(self, *, telegram_api_ipv4: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._connector_init["family"] = socket.AF_INET
        self._connector_init["resolver"] = TelegramIPv4Resolver(telegram_api_ipv4)


def build_telegram_session(settings: Settings) -> AiohttpSession:
    if not settings.telegram_force_ipv4:
        return AiohttpSession()
    return IPv4AiohttpSession(telegram_api_ipv4=settings.telegram_api_ipv4)
