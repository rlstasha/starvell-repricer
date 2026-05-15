import httpx


async def resolve_public_ip(configured_ip: str | None = None) -> str | None:
    if configured_ip:
        return configured_ip.strip() or None

    endpoints = (
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        for url in endpoints:
            try:
                response = await client.get(url)
                response.raise_for_status()
            except httpx.HTTPError:
                continue
            value = response.text.strip()
            if value:
                return value
    return None
