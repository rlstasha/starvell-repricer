from __future__ import annotations

import argparse
import os
import socket
import ssl
import sys
import time
from pathlib import Path


TELEGRAM_API_HOST = "api.telegram.org"
DEFAULT_HEARTBEAT_FILE = "/tmp/starvell_bot_polling_heartbeat"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Telegram API over IPv4.")
    parser.add_argument("--heartbeat-file", default=DEFAULT_HEARTBEAT_FILE)
    parser.add_argument("--max-heartbeat-age", type=float, default=90.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    heartbeat_error = _check_heartbeat(args.heartbeat_file, args.max_heartbeat_age)
    if heartbeat_error:
        print(heartbeat_error, file=sys.stderr)
        return 1

    try:
        status_line, ip = _check_telegram_ipv4(args.timeout)
    except Exception as exc:
        print(f"telegram_ipv4_unavailable: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"telegram_ipv4_ok ip={ip} status={status_line}")
    return 0


def _check_heartbeat(path: str, max_age_seconds: float) -> str | None:
    heartbeat = Path(path)
    if not heartbeat.exists():
        return f"polling_heartbeat_missing path={path}"
    age = time.time() - heartbeat.stat().st_mtime
    if age > max_age_seconds:
        return f"polling_heartbeat_stale age_seconds={age:.1f}"
    return None


def _check_telegram_ipv4(timeout_seconds: float) -> tuple[str, str]:
    ip = _select_telegram_ipv4()
    context = ssl.create_default_context()
    with socket.create_connection((ip, 443), timeout=timeout_seconds) as raw_socket:
        with context.wrap_socket(raw_socket, server_hostname=TELEGRAM_API_HOST) as tls_socket:
            tls_socket.settimeout(timeout_seconds)
            tls_socket.sendall(
                (
                    "HEAD / HTTP/1.1\r\n"
                    f"Host: {TELEGRAM_API_HOST}\r\n"
                    "Connection: close\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            response = tls_socket.recv(1024)
    status_line = response.splitlines()[0].decode("ascii", errors="replace")
    if not status_line.startswith("HTTP/"):
        raise RuntimeError(f"unexpected response: {status_line!r}")
    status_code = int(status_line.split()[1])
    if not 200 <= status_code < 400:
        raise RuntimeError(f"unexpected status: {status_line}")
    return status_line, ip


def _select_telegram_ipv4() -> str:
    configured_ip = os.getenv("TELEGRAM_API_IPV4", "").strip()
    if configured_ip:
        socket.inet_aton(configured_ip)
        return configured_ip

    infos = socket.getaddrinfo(
        TELEGRAM_API_HOST,
        443,
        family=socket.AF_INET,
        type=socket.SOCK_STREAM,
    )
    if not infos:
        raise RuntimeError("no IPv4 address returned for api.telegram.org")
    return str(infos[0][4][0])


if __name__ == "__main__":
    raise SystemExit(main())
