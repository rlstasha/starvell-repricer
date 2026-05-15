import asyncio

from app.core.config import get_settings
from app.core.network import mask_proxy_url, resolve_public_ip


async def amain() -> int:
    settings = get_settings()
    for info in settings.worker_group_infos:
        proxy_url = settings.proxy_url_for_group(info.name)
        public_ip = await resolve_public_ip(proxy_url=proxy_url)
        status = "ok" if public_ip else "error"
        if not proxy_url:
            status = "direct" if public_ip else "direct, ip check failed"

        print(f"proxy_{info.name}")
        print(f"IP: {public_ip or 'не удалось определить'}")
        print(f"positions: {','.join(str(amount) for amount in info.positions)}")
        print(f"limit: {info.request_limit_per_minute}/min")
        print(
            "frequency: "
            f"~{_frequency_seconds(info.request_limit_per_minute, len(info.positions))}"
        )
        print(f"status: {status}")
        print(f"proxy: {mask_proxy_url(proxy_url)}")
        print()

    return 0


def main() -> int:
    return asyncio.run(amain())


def _frequency_seconds(request_limit: int, position_count: int) -> str:
    if request_limit <= 0 or position_count <= 0:
        return "not available"
    return f"{60 * position_count / request_limit:.1f} sec"


if __name__ == "__main__":
    raise SystemExit(main())
