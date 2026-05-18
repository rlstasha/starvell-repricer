import argparse
import asyncio
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import httpx

from app.core.config import get_settings
from app.core.network import mask_proxy_url

IP_CHECK_HOST = "api.ipify.org"
IP_CHECK_PORT = 80
IP_CHECK_URL = "https://api.ipify.org?format=text"
SECRET_URL_RE = re.compile(r"((?:https?|socks5)://)([^/@\s:]+)(?::([^/@\s]+))?@")


@dataclass(frozen=True)
class ProxyEndpoint:
    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


@dataclass(frozen=True)
class ConnectivityAttempt:
    connection: str
    handshake: str
    external_ip: str | None
    response: str
    exact_exception: str | None
    elapsed_ms: int
    httpx_response: str | None = None
    httpx_exception: str | None = None

    @property
    def ok(self) -> bool:
        return self.connection == "ok" and self.handshake == "ok" and bool(self.external_ip)


class Socks5DiagnosticError(RuntimeError):
    pass


class Socks5ConnectionError(Socks5DiagnosticError):
    pass


class Socks5HandshakeError(Socks5DiagnosticError):
    pass


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Starvell proxy connectivity.")
    parser.add_argument("--attempts", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    attempts = max(args.attempts, 1)
    settings = get_settings()
    any_failed = False

    for info in settings.worker_group_infos:
        proxy_url = settings.proxy_url_for_group(info.name)
        result = await check_profile(
            profile=info.name,
            proxy_url=proxy_url,
            attempts=attempts,
            timeout=args.timeout,
        )
        if result["stability"].startswith(("failing", "unstable")):
            any_failed = True
        print_profile_result(result)

    if any_failed:
        print("summary: proxy pool degraded")
        print("recommendation: replace unstable SOCKS5 proxies or ask provider to fix the pool.")
        return 2
    print("summary: all configured proxies are stable")
    return 0


async def check_profile(
    *,
    profile: str,
    proxy_url: str | None,
    attempts: int,
    timeout: float,
) -> dict:
    endpoint = parse_proxy_endpoint(proxy_url)
    attempt_results: list[ConnectivityAttempt] = []
    for _ in range(attempts):
        attempt_results.append(await check_proxy_once(proxy_url, endpoint, timeout=timeout))

    ok_count = sum(1 for item in attempt_results if item.ok)
    return {
        "profile": profile,
        "proxy_url": proxy_url,
        "endpoint": endpoint,
        "attempts": attempt_results,
        "stability": classify_stability(ok_count, attempts),
    }


async def check_proxy_once(
    proxy_url: str | None,
    endpoint: ProxyEndpoint | None,
    *,
    timeout: float,
) -> ConnectivityAttempt:
    start = time.perf_counter()
    if proxy_url and endpoint and endpoint.scheme.startswith("socks5"):
        raw_result = await _check_socks5_once(endpoint, timeout=timeout)
        httpx_response, httpx_exception = await _httpx_probe(proxy_url, timeout=timeout)
        return ConnectivityAttempt(
            connection=raw_result.connection,
            handshake=raw_result.handshake,
            external_ip=raw_result.external_ip,
            response=raw_result.response,
            exact_exception=raw_result.exact_exception,
            elapsed_ms=raw_result.elapsed_ms,
            httpx_response=httpx_response,
            httpx_exception=httpx_exception,
        )

    httpx_response, httpx_exception = await _httpx_probe(proxy_url, timeout=timeout)
    elapsed_ms = int((time.perf_counter() - start) * 1000)
    external_ip = httpx_response if httpx_response and _looks_like_ip(httpx_response) else None
    return ConnectivityAttempt(
        connection="ok" if external_ip else "failed",
        handshake="not_applicable",
        external_ip=external_ip,
        response=httpx_response or "no response",
        exact_exception=httpx_exception,
        elapsed_ms=elapsed_ms,
        httpx_response=httpx_response,
        httpx_exception=httpx_exception,
    )


async def _check_socks5_once(endpoint: ProxyEndpoint, *, timeout: float) -> ConnectivityAttempt:
    start = time.perf_counter()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    connection = "failed"
    handshake = "failed"
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint.host, endpoint.port),
                timeout=timeout,
            )
        except Exception as exc:
            raise Socks5ConnectionError(_exception_chain(exc)) from exc
        connection = "ok"

        await _socks5_greeting(reader, writer, endpoint, timeout=timeout)
        await _socks5_connect(reader, writer, IP_CHECK_HOST, IP_CHECK_PORT, timeout=timeout)
        handshake = "ok"

        writer.write(
            (
                "GET /?format=text HTTP/1.1\r\n"
                f"Host: {IP_CHECK_HOST}\r\n"
                "User-Agent: starvell-repricer-proxy-check/1.0\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii")
        )
        await writer.drain()
        payload = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        response = payload.decode("utf-8", errors="replace")
        status_line, body = _split_http_response(response)
        external_ip = body.strip().splitlines()[-1].strip() if body.strip() else None
        if not external_ip or not _looks_like_ip(external_ip):
            raise Socks5HandshakeError(f"unexpected HTTP response via proxy: {status_line}")
        return ConnectivityAttempt(
            connection=connection,
            handshake=handshake,
            external_ip=external_ip,
            response=status_line or "HTTP response received",
            exact_exception=None,
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    except Socks5DiagnosticError as exc:
        return ConnectivityAttempt(
            connection=connection,
            handshake=handshake,
            external_ip=None,
            response="no usable response",
            exact_exception=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.perf_counter() - start) * 1000),
        )
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


async def _socks5_greeting(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    endpoint: ProxyEndpoint,
    *,
    timeout: float,
) -> None:
    methods = [0x00]
    if endpoint.username is not None or endpoint.password is not None:
        methods.append(0x02)
    writer.write(bytes([0x05, len(methods), *methods]))
    await writer.drain()
    reply = await _read_exactly(reader, 2, timeout=timeout, stage="method selection")
    if len(reply) != 2 or reply[0] != 0x05:
        raise Socks5HandshakeError(f"Malformed reply during method selection: {reply.hex()}")
    method = reply[1]
    if method == 0xFF:
        raise Socks5HandshakeError("SOCKS5 proxy rejected all authentication methods")
    if method == 0x02:
        await _socks5_username_password_auth(reader, writer, endpoint, timeout=timeout)
    elif method != 0x00:
        raise Socks5HandshakeError(f"Unsupported SOCKS5 auth method: {method}")


async def _socks5_username_password_auth(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    endpoint: ProxyEndpoint,
    *,
    timeout: float,
) -> None:
    username = (endpoint.username or "").encode()
    password = (endpoint.password or "").encode()
    if len(username) > 255 or len(password) > 255:
        raise Socks5HandshakeError("SOCKS5 username/password is too long")
    writer.write(bytes([0x01, len(username)]) + username + bytes([len(password)]) + password)
    await writer.drain()
    reply = await _read_exactly(reader, 2, timeout=timeout, stage="username/password auth")
    if len(reply) != 2 or reply[0] != 0x01 or reply[1] != 0x00:
        raise Socks5HandshakeError("SOCKS5 username/password authentication failed")


async def _socks5_connect(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    *,
    timeout: float,
) -> None:
    target_host_bytes = target_host.encode("idna")
    writer.write(
        bytes([0x05, 0x01, 0x00, 0x03, len(target_host_bytes)])
        + target_host_bytes
        + target_port.to_bytes(2, "big")
    )
    await writer.drain()
    header = await _read_exactly(reader, 4, timeout=timeout, stage="connect reply")
    if len(header) != 4 or header[0] != 0x05:
        raise Socks5HandshakeError(f"Malformed reply during connect: {header.hex()}")
    if header[1] != 0x00:
        raise Socks5HandshakeError(f"SOCKS5 connect failed with code {header[1]}")
    atyp = header[3]
    if atyp == 0x01:
        await _read_exactly(reader, 4, timeout=timeout, stage="bind IPv4")
    elif atyp == 0x03:
        length = await _read_exactly(reader, 1, timeout=timeout, stage="bind domain length")
        await _read_exactly(reader, length[0], timeout=timeout, stage="bind domain")
    elif atyp == 0x04:
        await _read_exactly(reader, 16, timeout=timeout, stage="bind IPv6")
    else:
        raise Socks5HandshakeError(f"Malformed reply with unknown address type: {atyp}")
    await _read_exactly(reader, 2, timeout=timeout, stage="bind port")


async def _read_exactly(
    reader: asyncio.StreamReader,
    size: int,
    *,
    timeout: float,
    stage: str,
) -> bytes:
    try:
        return await asyncio.wait_for(reader.readexactly(size), timeout=timeout)
    except Exception as exc:
        raise Socks5HandshakeError(f"{stage}: {_exception_chain(exc)}") from exc


async def _httpx_probe(proxy_url: str | None, *, timeout: float) -> tuple[str | None, str | None]:
    kwargs = {"timeout": httpx.Timeout(timeout)}
    if proxy_url:
        kwargs["proxy"] = proxy_url
    try:
        async with httpx.AsyncClient(**kwargs) as client:
            response = await client.get(IP_CHECK_URL)
            response.raise_for_status()
            return response.text.strip(), None
    except Exception as exc:
        return None, _exception_chain(exc)


def parse_proxy_endpoint(proxy_url: str | None) -> ProxyEndpoint | None:
    if not proxy_url:
        return None
    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        raise ValueError("proxy URL has no host")
    if parsed.port is None:
        raise ValueError("proxy URL has no port")
    return ProxyEndpoint(
        scheme=parsed.scheme,
        host=parsed.hostname,
        port=parsed.port,
        username=unquote(parsed.username) if parsed.username is not None else None,
        password=unquote(parsed.password) if parsed.password is not None else None,
    )


def classify_stability(ok_count: int, attempts: int) -> str:
    if ok_count == attempts:
        return f"stable ({ok_count}/{attempts} ok)"
    if ok_count == 0:
        return f"failing ({ok_count}/{attempts} ok)"
    return f"unstable ({ok_count}/{attempts} ok)"


def print_profile_result(result: dict) -> None:
    endpoint = result["endpoint"]
    attempts: list[ConnectivityAttempt] = result["attempts"]
    latest = attempts[-1]
    print(f"profile: {result['profile']}")
    print(f"proxy: {mask_proxy_url(result['proxy_url'])}")
    print(f"host: {endpoint.host if endpoint else 'direct'}")
    print(f"port: {endpoint.port if endpoint else 'direct'}")
    print(f"connection: {latest.connection}")
    print(f"handshake: {latest.handshake}")
    print(f"external ip: {latest.external_ip or 'not detected'}")
    print(f"response: {latest.response}")
    print(f"exact exception: {latest.exact_exception or latest.httpx_exception or 'none'}")
    print(f"httpx response: {latest.httpx_response or 'none'}")
    print(f"httpx exception: {latest.httpx_exception or 'none'}")
    print(f"stability: {result['stability']}")
    print(f"diagnosis: {diagnose(attempts)}")
    print()


def diagnose(attempts: list[ConnectivityAttempt]) -> str:
    if all(item.ok for item in attempts):
        return "proxy stable"
    exceptions = " ".join(
        item.exact_exception or item.httpx_exception or ""
        for item in attempts
    ).lower()
    if any(item.ok for item in attempts) and (
        "malformed" in exceptions or "socks5" in exceptions or "socksio" in exceptions
    ):
        return "proxy provider unstable: intermittent SOCKS5 malformed replies"
    if "malformed" in exceptions or "socks5" in exceptions or "socksio" in exceptions:
        return "proxy provider failing: SOCKS5 handshake returns malformed replies"
    if any(item.ok for item in attempts):
        return "proxy provider unstable: intermittent connectivity"
    return "proxy provider failing"


def _exception_chain(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        text = str(current).strip()
        parts.append(
            _sanitize_diagnostic_text(
                f"{type(current).__module__}.{type(current).__name__}: {text}"
            )
        )
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


def _sanitize_diagnostic_text(text: str) -> str:
    return SECRET_URL_RE.sub(r"\1***:***@", text)


def _split_http_response(response: str) -> tuple[str, str]:
    status_line = response.splitlines()[0] if response.splitlines() else ""
    _, _, body = response.partition("\r\n\r\n")
    if not body:
        _, _, body = response.partition("\n\n")
    return status_line, body


def _looks_like_ip(value: str) -> bool:
    try:
        socket.getaddrinfo(value, None)
    except socket.gaierror:
        return False
    return bool(value.strip())


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
