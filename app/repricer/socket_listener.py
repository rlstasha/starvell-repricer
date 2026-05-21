import asyncio
import json
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.core.config import Settings
from app.core.logging import get_logger

SOCKET_STATUS_KEY = "repricer:starvell-socket:status"
SOCKET_EVENTS_KEY = "repricer:starvell-socket:events"
SOCKET_EVENT_LIST_LIMIT = 100
SOCKET_SIGNAL_TTL_SECONDS = 300
SOCKET_LOT_SIGNAL_PREFIX = "repricer:starvell-socket:lot"
SOCKET_AMOUNT_SIGNAL_PREFIX = "repricer:starvell-socket:amount"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
SECRET_WORDS = ("cookie", "token", "session", "password", "csrf", "authorization")
LOT_ID_KEYS = {
    "lot_id",
    "lotid",
    "offer_id",
    "offerid",
    "listing_id",
    "listingid",
    "offer",
    "listing",
}
TITLE_KEYS = {"title", "name", "text", "description", "label"}
URL_KEYS = {"href", "url", "link", "path"}
OFFER_ID_RE = re.compile(r"/offers/(\d+)")
ROBUX_RE = re.compile(r"(?<!\d)(\d{2,6})\s*(?:робукс|robux)", re.IGNORECASE)

logger = get_logger(__name__)


@dataclass(frozen=True)
class SocketStatus:
    enabled: bool
    connected: bool
    namespace: str
    last_event_at: datetime | None
    events_seen: int
    fallback_active: bool
    last_error: str | None
    updated_at: datetime | None


@dataclass(frozen=True)
class SocketEventRefs:
    lot_ids: tuple[str, ...]
    position_amounts: tuple[int, ...]


def socket_lot_signal_key(lot_id: str) -> str:
    return f"{SOCKET_LOT_SIGNAL_PREFIX}:{lot_id}"


def socket_amount_signal_key(amount: int) -> str:
    return f"{SOCKET_AMOUNT_SIGNAL_PREFIX}:{amount}"


async def load_socket_status(redis: Redis) -> SocketStatus:
    raw = await redis.get(SOCKET_STATUS_KEY)
    if not raw:
        return SocketStatus(
            enabled=False,
            connected=False,
            namespace="/viewed-offers",
            last_event_at=None,
            events_seen=0,
            fallback_active=True,
            last_error=None,
            updated_at=None,
        )
    try:
        data = json.loads(raw)
    except ValueError:
        return SocketStatus(
            enabled=False,
            connected=False,
            namespace="/viewed-offers",
            last_event_at=None,
            events_seen=0,
            fallback_active=True,
            last_error="invalid_status_json",
            updated_at=None,
        )
    return SocketStatus(
        enabled=bool(data.get("enabled")),
        connected=bool(data.get("connected")),
        namespace=str(data.get("namespace") or "/viewed-offers"),
        last_event_at=_parse_datetime(data.get("last_event_at")),
        events_seen=int(data.get("events_seen") or 0),
        fallback_active=bool(data.get("fallback_active", True)),
        last_error=data.get("last_error") or None,
        updated_at=_parse_datetime(data.get("updated_at")),
    )


class StarvellSocketListener:
    def __init__(self, *, settings: Settings, redis: Redis):
        self.settings = settings
        self.redis = redis
        self.namespace = settings.starvell_socket_namespace
        self.events_seen = 0
        self.last_event_at: datetime | None = None
        self.idle_logged = False

    async def run_forever(self) -> None:
        if not self.settings.starvell_socket_enabled:
            await self._write_status(
                connected=False,
                fallback_active=True,
                last_error=None,
            )
            logger.info(
                "starvell_socket_disabled",
                namespace=self.namespace,
                fallback_active=True,
            )
            return

        while True:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = _safe_error(exc)
                await self._write_status(
                    connected=False,
                    fallback_active=True,
                    last_error=error,
                )
                delay = random.uniform(
                    self.settings.starvell_socket_reconnect_min_seconds,
                    self.settings.starvell_socket_reconnect_max_seconds,
                )
                logger.warning(
                    "starvell_socket_reconnect",
                    namespace=self.namespace,
                    error=error,
                    reconnect_in_seconds=round(delay, 2),
                )
                await asyncio.sleep(delay)

    async def _run_once(self) -> None:
        import socketio

        sio = socketio.AsyncClient(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )

        @sio.event(namespace=self.namespace)
        async def connect() -> None:
            self.idle_logged = False
            await self._write_status(
                connected=True,
                fallback_active=self._fallback_active(),
                last_error=None,
            )
            logger.info("starvell_socket_connected", namespace=self.namespace)

        @sio.event(namespace=self.namespace)
        async def disconnect() -> None:
            await self._write_status(
                connected=False,
                fallback_active=True,
                last_error=None,
            )
            logger.warning("starvell_socket_disconnected", namespace=self.namespace)

        @sio.on("*", namespace=self.namespace)
        async def catch_all(event: str, *data: Any) -> None:
            payload = data[0] if len(data) == 1 else data
            await self._handle_event(event, payload)

        await sio.connect(
            self.settings.market_base_url.rstrip("/"),
            headers=self._headers(),
            transports=["websocket"],
            socketio_path="socket.io",
            namespaces=[self.namespace],
            wait_timeout=15,
        )
        try:
            while sio.connected:
                await self._check_idle()
                await asyncio.sleep(5)
        finally:
            if sio.connected:
                await sio.disconnect()

    async def _handle_event(self, event: str, payload: Any) -> None:
        self.events_seen += 1
        self.last_event_at = datetime.now(UTC)
        self.idle_logged = False
        refs = extract_socket_event_refs(payload)
        event_data = {
            "event": event,
            "namespace": self.namespace,
            "received_at": self.last_event_at.isoformat(),
            "lot_ids": list(refs.lot_ids),
            "position_amounts": list(refs.position_amounts),
            "payload": sanitize(payload),
        }
        await self.redis.rpush(SOCKET_EVENTS_KEY, json.dumps(event_data, ensure_ascii=False, default=str))
        await self.redis.ltrim(SOCKET_EVENTS_KEY, -SOCKET_EVENT_LIST_LIMIT, -1)
        signal_value = json.dumps(
            {
                "event": event,
                "received_at": self.last_event_at.isoformat(),
            },
            ensure_ascii=False,
        )
        for lot_id in refs.lot_ids:
            await self.redis.setex(
                socket_lot_signal_key(lot_id),
                SOCKET_SIGNAL_TTL_SECONDS,
                signal_value,
            )
        for amount in refs.position_amounts:
            await self.redis.setex(
                socket_amount_signal_key(amount),
                SOCKET_SIGNAL_TTL_SECONDS,
                signal_value,
            )
        await self._write_status(
            connected=True,
            fallback_active=False,
            last_error=None,
        )
        logger.info(
            "starvell_socket_event_received",
            namespace=self.namespace,
            event=event,
            lot_ids=list(refs.lot_ids),
            position_amounts=list(refs.position_amounts),
            events_seen=self.events_seen,
        )

    async def _check_idle(self) -> None:
        fallback_active = self._fallback_active()
        await self._write_status(
            connected=True,
            fallback_active=fallback_active,
            last_error=None,
        )
        if fallback_active and not self.idle_logged:
            self.idle_logged = True
            logger.info(
                "starvell_socket_idle_fallback",
                namespace=self.namespace,
                idle_timeout_seconds=self.settings.starvell_socket_idle_timeout_seconds,
            )

    def _fallback_active(self) -> bool:
        if self.last_event_at is None:
            return True
        idle_seconds = (datetime.now(UTC) - self.last_event_at).total_seconds()
        return idle_seconds >= self.settings.starvell_socket_idle_timeout_seconds

    async def _write_status(
        self,
        *,
        connected: bool,
        fallback_active: bool,
        last_error: str | None,
    ) -> None:
        now = datetime.now(UTC)
        payload = {
            "enabled": self.settings.starvell_socket_enabled,
            "connected": connected,
            "namespace": self.namespace,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "events_seen": self.events_seen,
            "fallback_active": fallback_active,
            "last_error": last_error,
            "updated_at": now.isoformat(),
        }
        await self.redis.set(SOCKET_STATUS_KEY, json.dumps(payload, ensure_ascii=False))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Origin": self.settings.market_base_url.rstrip("/"),
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }
        cookie = _socket_cookie(self.settings.market_session_cookie)
        if cookie:
            headers["Cookie"] = cookie
        return headers


def extract_socket_event_refs(payload: Any) -> SocketEventRefs:
    lot_ids: set[str] = set()
    amounts: set[int] = set()
    _extract_refs(payload, lot_ids=lot_ids, amounts=amounts, parent_key=None)
    return SocketEventRefs(
        lot_ids=tuple(sorted(lot_ids)),
        position_amounts=tuple(sorted(amounts)),
    )


def sanitize(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(secret in key_text.lower() for secret in SECRET_WORDS):
                result[key_text] = "***"
            else:
                result[key_text] = sanitize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def _extract_refs(
    value: Any,
    *,
    lot_ids: set[str],
    amounts: set[int],
    parent_key: str | None,
) -> None:
    if isinstance(value, Mapping):
        dict_keys = {_normalize_key(key) for key in value}
        looks_like_offer = bool(dict_keys & {"price", "sellerid", "seller_id", "title", "name"})
        for key, item in value.items():
            key_text = _normalize_key(key)
            if key_text in LOT_ID_KEYS or (key_text == "id" and looks_like_offer):
                _add_lot_ids(item, lot_ids)
            if key_text in TITLE_KEYS or key_text in URL_KEYS:
                _extract_string_refs(str(item), lot_ids=lot_ids, amounts=amounts)
            _extract_refs(item, lot_ids=lot_ids, amounts=amounts, parent_key=key_text)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _extract_refs(item, lot_ids=lot_ids, amounts=amounts, parent_key=parent_key)
        return
    if isinstance(value, str):
        _extract_string_refs(value, lot_ids=lot_ids, amounts=amounts)
        if parent_key and parent_key in LOT_ID_KEYS:
            _add_lot_ids(value, lot_ids)
        return
    if isinstance(value, int) and parent_key and parent_key in LOT_ID_KEYS:
        _add_lot_ids(value, lot_ids)


def _extract_string_refs(text: str, *, lot_ids: set[str], amounts: set[int]) -> None:
    for match in OFFER_ID_RE.finditer(text):
        lot_ids.add(match.group(1))
    for match in ROBUX_RE.finditer(text):
        amount = int(match.group(1))
        if amount > 0:
            amounts.add(amount)


def _add_lot_ids(value: Any, lot_ids: set[str]) -> None:
    for item in _iter_scalar_values(value):
        text = str(item).strip()
        if text.isdigit():
            lot_ids.add(text)
        else:
            for match in OFFER_ID_RE.finditer(text):
                lot_ids.add(match.group(1))


def _iter_scalar_values(value: Any) -> Iterable[Any]:
    if isinstance(value, Mapping):
        yield from value.values()
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_scalar_values(item)
        return
    yield value


def _socket_cookie(session_cookie: str) -> str:
    cookie = session_cookie.strip()
    if not cookie:
        return ""
    if "=" in cookie or ";" in cookie:
        return cookie
    return f"session={cookie}"


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in SECRET_WORDS:
        text = text.replace(secret, "***")
    return text


def _normalize_key(value: Any) -> str:
    return str(value).replace("-", "_").lower()
