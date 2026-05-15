import asyncio

from app.core.config import get_settings
from app.core.network import resolve_public_ip


async def amain() -> int:
    settings = get_settings()
    proxy_url = settings.proxy_url_for_group()
    public_ip = await resolve_public_ip(
        settings.public_ip if proxy_url is None else None,
        proxy_url=proxy_url,
    )
    assigned_positions = ",".join(str(amount) for amount in settings.assigned_positions)

    print(f"Worker group: {settings.worker_group}")
    print(f"Public IP: {public_ip or 'не удалось определить'}")
    print(f"Assigned positions: {assigned_positions}")
    print(f"Limit: {settings.worker_request_limit_per_minute}/min")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
