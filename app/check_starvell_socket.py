import argparse
import asyncio
import json
import os
import random
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import aiohttp

from app.core.config import get_settings

STARVELL_SOCKET_URL = "https://starvell.com"
SOCKET_IO_PATH = "socket.io"
STARVELL_BROWSER_NAMESPACES = (
    "/",
    "/chats",
    "/user-notifications",
    "/user-presence",
    "/viewed-offers",
    "/online",
)
DEFAULT_ORIGIN = "https://starvell.com"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0 Safari/537.36"
)
DEFAULT_LOT_IDS = ("1996", "1998", "1999", "2000")
SECRET_WORDS = ("cookie", "token", "session", "password", "csrf", "authorization")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Диагностика Starvell Socket.IO/WebSocket без изменения данных."
    )
    parser.add_argument("--namespace", default="/", help="Socket.IO namespace, например /viewed-offers")
    parser.add_argument(
        "--all-namespaces",
        action="store_true",
        help="Подключиться к /, /chats, /user-notifications, /user-presence, /viewed-offers, /online.",
    )
    parser.add_argument("--event", default="viewed-offers", help="Событие/комната для пробного emit")
    parser.add_argument(
        "--lot-ids",
        default=",".join(DEFAULT_LOT_IDS),
        help="Список lot_id для diagnostic subscribe payload, через запятую.",
    )
    parser.add_argument(
        "--category-ids",
        default="",
        help="Список category_id/subcategory_id для diagnostic subscribe payload, через запятую.",
    )
    parser.add_argument(
        "--probe-subscriptions",
        action="store_true",
        help="Пробовать viewed-offers/join/subscribe payload варианты.",
    )
    parser.add_argument("--sid", default="", help="Ручной Engine.IO SID для raw WebSocket fallback")
    parser.add_argument("--duration", type=float, default=60.0, help="Сколько секунд слушать события")
    parser.add_argument("--debug", action="store_true", help="Печатать подробные технические события")
    parser.add_argument("--auth-cookie", default="", help="Cookie для диагностики; значение не печатается")
    parser.add_argument("--origin", default=DEFAULT_ORIGIN, help="Origin header")
    parser.add_argument("--raw", action="store_true", help="Принудительно использовать raw Engine.IO client")
    parser.add_argument(
        "--no-probes",
        action="store_true",
        help="Не отправлять пробные viewed-offers/join/subscribe emit.",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    cookie = _socket_cookie(args.auth_cookie)
    headers = _browser_headers(origin=args.origin, cookie=cookie)

    print("Starvell Socket.IO diagnostics")
    print("Mode: read/listen only. Repricer logic is not touched.")
    print(f"URL: wss://starvell.com/{SOCKET_IO_PATH}/?EIO=4&transport=websocket")
    print(f"Namespaces: {', '.join(namespaces_for_args(args))}")
    print(f"Probe event: {args.event}")
    print(f"Probe subscriptions: {'yes' if args.probe_subscriptions else 'no'}")
    print(f"Cookie/auth: {'configured' if cookie else 'not configured'}")
    print()

    if args.raw:
        return await run_raw_client(args=args, headers=headers)

    try:
        import socketio
    except ImportError:
        print("python-socketio is not installed; falling back to raw aiohttp websocket.")
        return await run_raw_client(args=args, headers=headers)

    try:
        return await run_socketio_client(socketio=socketio, args=args, headers=headers)
    except Exception as exc:
        print(f"python-socketio failed: {_safe_error(exc)}")
        print("Falling back to raw aiohttp websocket.")
        return await run_raw_client(args=args, headers=headers)


async def run_socketio_client(*, socketio: Any, args: argparse.Namespace, headers: dict[str, str]) -> int:
    sio = socketio.AsyncClient(
        reconnection=True,
        reconnection_attempts=0,
        reconnection_delay=3,
        reconnection_delay_max=10,
        logger=False,
        engineio_logger=False,
    )
    connected_namespaces: set[str] = set()
    events_seen: list[str] = []

    @sio.event
    async def connect() -> None:
        connected_namespaces.add("/")
        print_event("connected", namespace="/", payload={"sid": getattr(sio, "sid", None)})

    @sio.event
    async def disconnect() -> None:
        print_event("disconnected", namespace="/", payload=None)

    @sio.event
    async def connect_error(data: Any) -> None:
        print_event("connect_error", namespace="/", payload=sanitize(data))

    @sio.on("*")
    async def catch_all(event: str, *data: Any) -> None:
        events_seen.append(event)
        print_event(event, namespace="/", payload=sanitize(data[0] if len(data) == 1 else data))

    namespaces = namespaces_for_args(args)
    connect_namespaces = namespaces
    for namespace in namespaces:
        if namespace != "/":
            register_namespace_handlers(sio, namespace, connected_namespaces, events_seen)

    deadline = time.monotonic() + max(args.duration, 1.0)
    while time.monotonic() < deadline:
        try:
            print("Connecting with python-socketio...")
            await sio.connect(
                STARVELL_SOCKET_URL,
                headers=headers,
                transports=["websocket"],
                socketio_path=SOCKET_IO_PATH,
                namespaces=connect_namespaces,
                wait_timeout=15,
            )
            print(f"SID: {getattr(sio, 'sid', 'unknown')}")
            if args.probe_subscriptions and not args.no_probes:
                await emit_probe_events(
                    sio,
                    namespaces=probe_namespaces(namespaces),
                    event=args.event,
                    lot_ids=parse_csv(args.lot_ids),
                    category_ids=parse_csv(args.category_ids),
                )
            await asyncio.sleep(max(deadline - time.monotonic(), 0.0))
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if time.monotonic() >= deadline:
                print(f"Final socketio error: {_safe_error(exc)}")
                break
            delay = random.uniform(3, 10)
            print(f"Socket.IO connection error: {_safe_error(exc)}")
            print(f"Reconnect in {delay:.1f}s")
            await asyncio.sleep(delay)
        finally:
            if sio.connected:
                await sio.disconnect()

    print_summary(
        connected=bool(connected_namespaces),
        connected_namespaces=sorted(connected_namespaces),
        events_seen=events_seen,
        expected_event=args.event,
    )
    return 0


def register_namespace_handlers(
    sio: Any,
    namespace: str,
    connected_namespaces: set[str],
    events_seen: list[str],
) -> None:
    async def namespace_connect() -> None:
        connected_namespaces.add(namespace)
        print_event("connected", namespace=namespace, payload={"namespace": namespace})

    async def namespace_disconnect() -> None:
        print_event("disconnected", namespace=namespace, payload={"namespace": namespace})

    async def namespace_catch_all(event: str, *data: Any) -> None:
        events_seen.append(event)
        print_event(event, namespace=namespace, payload=sanitize(data[0] if len(data) == 1 else data))

    sio.on("connect", handler=namespace_connect, namespace=namespace)
    sio.on("disconnect", handler=namespace_disconnect, namespace=namespace)
    sio.on("*", handler=namespace_catch_all, namespace=namespace)


async def emit_probe_events(
    sio: Any,
    *,
    namespaces: Sequence[str],
    event: str,
    lot_ids: Sequence[str],
    category_ids: Sequence[str],
) -> None:
    for target_namespace in namespaces:
        for event_name, payload in probe_payloads(
            event,
            lot_ids=lot_ids,
            category_ids=category_ids,
        ):
            try:
                await sio.emit(event_name, payload, namespace=target_namespace)
                print_event(
                    "probe_emit",
                    namespace=target_namespace,
                    payload={"event": event_name, "payload": payload},
                )
            except Exception as exc:
                print_event(
                    "probe_emit_failed",
                    namespace=target_namespace,
                    payload={"event": event_name, "reason": _safe_error(exc)},
                )


async def run_raw_client(*, args: argparse.Namespace, headers: dict[str, str]) -> int:
    events_seen: list[str] = []
    connected_namespaces: list[str] = []
    deadline = time.monotonic() + max(args.duration, 1.0)
    while time.monotonic() < deadline:
        try:
            sid = args.sid.strip()
            if sid:
                print(f"SID: {sid}")
            await raw_websocket_loop(
                args=args,
                headers=headers,
                sid=sid,
                deadline=deadline,
                events_seen=events_seen,
                connected_namespaces=connected_namespaces,
            )
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if time.monotonic() >= deadline:
                print(f"Final raw websocket error: {_safe_error(exc)}")
                break
            delay = random.uniform(3, 10)
            print(f"Raw websocket error: {_safe_error(exc)}")
            print(f"Reconnect in {delay:.1f}s")
            await asyncio.sleep(delay)

    print_summary(
        connected=bool(connected_namespaces),
        connected_namespaces=sorted(set(connected_namespaces)),
        events_seen=events_seen,
        expected_event=args.event,
    )
    return 0


async def fetch_engineio_sid(*, headers: dict[str, str]) -> str:
    url = f"{STARVELL_SOCKET_URL}/{SOCKET_IO_PATH}/?EIO=4&transport=polling"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
            text = await response.text()
            response.raise_for_status()
    packet_type, payload = split_engineio_packet(text)
    if packet_type != "0":
        raise RuntimeError(f"Unexpected polling open packet: {text[:80]}")
    data = json.loads(payload)
    sid = str(data.get("sid") or "")
    if not sid:
        raise RuntimeError("Engine.IO SID not found in polling response")
    print_event("engineio_open", namespace="/", payload=sanitize(data))
    return sid


async def raw_websocket_loop(
    *,
    args: argparse.Namespace,
    headers: dict[str, str],
    sid: str,
    deadline: float,
    events_seen: list[str],
    connected_namespaces: list[str],
) -> None:
    namespaces = namespaces_for_args(args)
    sid_suffix = f"&sid={sid}" if sid else ""
    ws_url = f"wss://starvell.com/{SOCKET_IO_PATH}/?EIO=4&transport=websocket{sid_suffix}"
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.ws_connect(
            ws_url,
            autoping=False,
            heartbeat=None,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as websocket:
            print_event("websocket_connected", namespace="/", payload={"sid": sid})
            namespace_connected = False
            if sid:
                await websocket.send_str("2probe")

            while time.monotonic() < deadline:
                timeout = max(min(deadline - time.monotonic(), 5.0), 0.1)
                try:
                    message = await websocket.receive(timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                if message.type == aiohttp.WSMsgType.TEXT:
                    frame = str(message.data)
                    if args.debug:
                        print_event("raw_message", namespace="/", payload=frame)
                    if frame.startswith("0"):
                        await handle_engineio_open_frame(frame)
                        if not sid and not namespace_connected:
                            await raw_connect_namespaces_and_probe(
                                websocket,
                                namespaces=namespaces,
                                event=args.event,
                                lot_ids=parse_csv(args.lot_ids),
                                category_ids=parse_csv(args.category_ids),
                                probe_subscriptions=args.probe_subscriptions,
                                no_probes=args.no_probes,
                            )
                            namespace_connected = True
                        continue
                    if frame == "3probe":
                        print_event("engineio_probe_pong", namespace="/", payload=None)
                        await websocket.send_str("5")
                        if not namespace_connected:
                            await raw_connect_namespaces_and_probe(
                                websocket,
                                namespaces=namespaces,
                                event=args.event,
                                lot_ids=parse_csv(args.lot_ids),
                                category_ids=parse_csv(args.category_ids),
                                probe_subscriptions=args.probe_subscriptions,
                                no_probes=args.no_probes,
                            )
                            namespace_connected = True
                        continue
                    await handle_raw_frame(
                        websocket,
                        frame=frame,
                        events_seen=events_seen,
                        connected_namespaces=connected_namespaces,
                    )
                elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    raise RuntimeError("WebSocket closed by server")
                elif message.type == aiohttp.WSMsgType.ERROR:
                    raise RuntimeError(f"WebSocket error: {websocket.exception()}")


async def raw_emit_probe_events(websocket: aiohttp.ClientWebSocketResponse, *, namespace: str, event: str) -> None:
    for event_name, payload in probe_payloads(event, lot_ids=DEFAULT_LOT_IDS, category_ids=()):
        frame = make_socketio_event_frame(event_name, payload, namespace=namespace)
        await websocket.send_str(frame)
        print_event("probe_emit", namespace=namespace, payload={"event": event_name, "payload": payload})


async def raw_connect_namespaces_and_probe(
    websocket: aiohttp.ClientWebSocketResponse,
    *,
    namespaces: Sequence[str],
    event: str,
    lot_ids: Sequence[str],
    category_ids: Sequence[str],
    probe_subscriptions: bool,
    no_probes: bool,
) -> None:
    for namespace in namespaces:
        await websocket.send_str(make_socketio_connect_frame(namespace))
    if probe_subscriptions and not no_probes:
        for namespace in probe_namespaces(namespaces):
            for event_name, payload in probe_payloads(
                event,
                lot_ids=lot_ids,
                category_ids=category_ids,
            ):
                frame = make_socketio_event_frame(event_name, payload, namespace=namespace)
                await websocket.send_str(frame)
                print_event(
                    "probe_emit",
                    namespace=namespace,
                    payload={"event": event_name, "payload": payload},
                )


async def handle_engineio_open_frame(frame: str) -> None:
    _, payload = split_engineio_packet(frame)
    try:
        data = json.loads(payload)
    except ValueError:
        data = payload
    print_event("engineio_open", namespace="/", payload=sanitize(data))


async def handle_raw_frame(
    websocket: aiohttp.ClientWebSocketResponse,
    *,
    frame: str,
    events_seen: list[str],
    connected_namespaces: list[str],
) -> None:
    if frame == "2":
        await websocket.send_str("3")
        print_event("engineio_pong", namespace="/", payload=None)
        return
    if frame == "40":
        connected_namespaces.append("/")
        print_event("namespace_connected", namespace="/", payload=None)
        return
    if frame.startswith("40") and frame != "40":
        namespace, payload = split_socketio_connect_frame(frame[2:])
        connected_namespaces.append(namespace)
        print_event("namespace_connected", namespace=namespace, payload=sanitize(payload))
        return
    if frame.startswith("42"):
        namespace, payload_text = split_socketio_event_frame(frame[2:])
        try:
            payload = json.loads(payload_text)
        except ValueError:
            payload = payload_text
        event_name = payload[0] if isinstance(payload, list) and payload else "unknown"
        events_seen.append(str(event_name))
        print_event(
            str(event_name),
            namespace=namespace,
            payload={"raw_payload": payload_text, "parsed": sanitize(payload)},
        )
        return
    print_event("engineio_frame", namespace="/", payload=frame)


def split_engineio_packet(text: str) -> tuple[str, str]:
    if not text:
        return "", ""
    return text[0], text[1:]


def split_socketio_event_frame(payload: str) -> tuple[str, str]:
    if payload.startswith("/"):
        namespace, _, payload_text = payload.partition(",")
        return namespace or "/", payload_text
    return "/", payload


def split_socketio_connect_frame(payload: str) -> tuple[str, Any]:
    if payload.startswith("/"):
        namespace, _, payload_text = payload.partition(",")
        namespace = namespace or "/"
    else:
        namespace = "/"
        payload_text = payload
    if payload_text:
        try:
            return namespace, json.loads(payload_text)
        except ValueError:
            return namespace, payload_text
    return namespace, None


def make_socketio_event_frame(event_name: str, payload: Any, *, namespace: str) -> str:
    packet = json.dumps([event_name, payload], ensure_ascii=False, separators=(",", ":"))
    namespace = _normalize_namespace(namespace)
    if namespace == "/":
        return f"42{packet}"
    return f"42{namespace},{packet}"


def make_socketio_connect_frame(namespace: str) -> str:
    namespace = _normalize_namespace(namespace)
    if namespace == "/":
        return "40"
    return f"40{namespace},"


def probe_payloads(
    event: str,
    *,
    lot_ids: Sequence[str],
    category_ids: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    lot_ids = [str(item) for item in lot_ids if str(item)]
    category_ids = [str(item) for item in category_ids if str(item)]
    return [
        (event, {}),
        (event, {"room": event}),
        ("join", {"room": event}),
        ("subscribe", {"room": event}),
        ("subscribe", {"channel": event}),
        ("subscribe", {"lot_ids": lot_ids}),
        ("subscribe", {"offer_ids": lot_ids}),
        ("subscribe", {"category_ids": category_ids}),
    ]


def namespaces_for_args(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all_namespaces", False):
        return list(STARVELL_BROWSER_NAMESPACES)
    return [_normalize_namespace(args.namespace)]


def probe_namespaces(namespaces: Sequence[str]) -> list[str]:
    normalized = [_normalize_namespace(namespace) for namespace in namespaces]
    result = [namespace for namespace in normalized if namespace == "/viewed-offers"]
    if "/" in normalized:
        result.append("/")
    return list(dict.fromkeys(result or normalized))


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def print_event(event: str, *, namespace: str, payload: Any) -> None:
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{timestamp}] namespace={namespace} event={event}")
    if payload is not None:
        print(json.dumps(sanitize(payload), ensure_ascii=False, indent=2, default=str))


def print_summary(
    *,
    connected: bool,
    connected_namespaces: list[str],
    events_seen: list[str],
    expected_event: str,
) -> None:
    print()
    print("Summary")
    print(f"- connected: {'yes' if connected else 'no'}")
    print(f"- namespaces: {', '.join(connected_namespaces) if connected_namespaces else 'not confirmed'}")
    print(f"- events seen: {', '.join(sorted(set(events_seen))) if events_seen else 'none'}")
    print(f"- {expected_event}: {'seen' if expected_event in events_seen else 'not seen yet'}")
    if expected_event not in events_seen:
        print("- If no events arrived, open lot/profile pages in browser while this script is running.")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if any(secret in key_text.lower() for secret in SECRET_WORDS):
                sanitized[key_text] = "***"
            else:
                sanitized[key_text] = sanitize(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item) for item in value]
    return value


def _socket_cookie(auth_cookie: str) -> str:
    if auth_cookie.strip():
        return auth_cookie.strip()
    env_cookie = os.getenv("MARKET_COOKIE", "").strip()
    if env_cookie:
        return env_cookie
    settings = get_settings()
    if not settings.market_session_cookie:
        return ""
    if "=" in settings.market_session_cookie or ";" in settings.market_session_cookie:
        return settings.market_session_cookie
    return f"session={settings.market_session_cookie}"


def _browser_headers(*, origin: str, cookie: str) -> dict[str, str]:
    headers = {
        "Origin": origin,
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _normalize_namespace(namespace: str) -> str:
    namespace = (namespace or "/").strip()
    if not namespace:
        return "/"
    if not namespace.startswith("/"):
        return f"/{namespace}"
    return namespace


def _safe_error(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}"
    for secret in SECRET_WORDS:
        text = text.replace(secret, "***")
    return text


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
