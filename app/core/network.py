from urllib.parse import urlsplit, urlunsplit

import httpx


async def resolve_public_ip(
    configured_ip: str | None = None,
    *,
    proxy_url: str | None = None,
) -> str | None:
    if configured_ip:
        return configured_ip.strip() or None

    endpoints = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    )
    client_kwargs = {"timeout": 5.0}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    try:
        client = httpx.AsyncClient(**client_kwargs)
    except Exception:
        return None

    async with client:
        for url in endpoints:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except Exception:
                continue
            value = response.text.strip()
            if value:
                return value
    return None


def mask_proxy_url(proxy_url: str | None) -> str:
    if not proxy_url:
        return "direct"

    try:
        parsed = urlsplit(proxy_url)
    except ValueError:
        return "invalid-proxy-url"

    host = parsed.hostname or ""
    if not host:
        return f"{parsed.scheme}://***"

    port = f":{parsed.port}" if parsed.port else ""
    credentials = "***:***@" if parsed.username or parsed.password else ""
    netloc = f"{credentials}{host}{port}"
    return urlunsplit((parsed.scheme, netloc, "", "", ""))
