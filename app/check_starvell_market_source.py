import argparse
import asyncio
import json
import re
import time
from collections.abc import Sequence
from typing import Any

import httpx

from app.core.config import get_settings
from app.market.client import (
    STARVELL_CATEGORY_ID,
    STARVELL_ROBUX_SUBCATEGORY_IDS,
    _market_offers_api_payload,
)


INTERESTING_HEADERS = (
    "etag",
    "last-modified",
    "cache-control",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "content-type",
    "content-length",
    "cf-cache-status",
    "server",
    "date",
)
TARGET_AMOUNTS = (40, 80, 200, 400, 500, 800, 1000, 1200, 1700, 2000, 2100, 2500, 3600, 4500, 10000, 22500)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for Starvell /api/offers/list-by-category."
    )
    parser.add_argument("--position", type=int, default=500)
    parser.add_argument("--full", action="store_true", help="Run payload and batching probes.")
    parser.add_argument("--sleep", type=float, default=1.2, help="Pause between probes.")
    parser.add_argument("--limits", default="5,10,20,30,50,100")
    parser.add_argument("--offsets", default="0,150,300,450")
    parser.add_argument(
        "--proxy-profile",
        default="",
        help="Optional proxy profile to match worker traffic: fast_1, fast_2, slow.",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    proxy_url = settings.proxy_url_for_group(args.proxy_profile) if args.proxy_profile else None
    cookies = {"session": settings.market_session_cookie} if settings.market_session_cookie else {}
    headers = {
        "Origin": "https://starvell.com",
        "Referer": "https://starvell.com/roblox/packages",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
        ),
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }

    print("Starvell market source diagnostics")
    print("Mode: read-only POST diagnostics. Cookies/tokens/proxy passwords are not printed.")
    print(f"Endpoint: {settings.market_offers_api_url}")
    print(f"Position: {args.position}")
    print(f"Proxy profile: {args.proxy_profile or 'direct'}")
    print()

    async with httpx.AsyncClient(
        base_url=settings.market_base_url,
        timeout=30,
        headers=headers,
        cookies=cookies,
        proxy=proxy_url,
    ) as client:
        baseline = await run_probe(
            client,
            name=f"baseline_{args.position}",
            payload=_market_offers_api_payload(
                position_amount=args.position,
                limit=settings.market_offers_limit,
            ),
        )
        print_header_report(baseline.response)
        await run_conditional_probe(client, baseline.response, baseline.payload, sleep_seconds=args.sleep)

        if args.full:
            await run_payload_probes(client, args)

    return 0


class ProbeResult:
    def __init__(self, *, name: str, payload: dict[str, Any], response: httpx.Response, elapsed_ms: float):
        self.name = name
        self.payload = payload
        self.response = response
        self.elapsed_ms = elapsed_ms


async def run_probe(client: httpx.AsyncClient, *, name: str, payload: dict[str, Any]) -> ProbeResult:
    started = time.perf_counter()
    response = await client.post("/api/offers/list-by-category", json=payload)
    elapsed_ms = (time.perf_counter() - started) * 1000
    data = safe_json(response)
    items = extract_items(data)
    amounts = unique_amounts(items)
    print(
        f"{name}: status={response.status_code} ms={elapsed_ms:.1f} "
        f"items={len(items)} amounts={amounts[:20]}"
    )
    if isinstance(data, dict) and data.get("message"):
        print(f"{name}: message={data['message']}")
    if items:
        print(f"{name}: item_keys={sorted(items[0].keys())}")
    return ProbeResult(name=name, payload=payload, response=response, elapsed_ms=elapsed_ms)


def print_header_report(response: httpx.Response) -> None:
    print("Headers:")
    found = False
    for header in INTERESTING_HEADERS:
        value = response.headers.get(header)
        if value is None:
            continue
        found = True
        print(f"- {header}: {value}")
    if not found:
        print("- none of the tracked cache/rate-limit headers were returned")
    print()


async def run_conditional_probe(
    client: httpx.AsyncClient,
    response: httpx.Response,
    payload: dict[str, Any],
    *,
    sleep_seconds: float,
) -> None:
    validators: dict[str, str] = {}
    if response.headers.get("etag"):
        validators["If-None-Match"] = response.headers["etag"]
    if response.headers.get("last-modified"):
        validators["If-Modified-Since"] = response.headers["last-modified"]

    await asyncio.sleep(sleep_seconds)
    if validators:
        started = time.perf_counter()
        response = await client.post("/api/offers/list-by-category", json=payload, headers=validators)
        elapsed_ms = (time.perf_counter() - started) * 1000
        print(f"Conditional validators sent: {validators}")
        print(f"Conditional status: {response.status_code} ms={elapsed_ms:.1f}")
        return

    forced_headers = {
        "If-None-Match": '"starvell-diagnostic-probe"',
        "If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT",
    }
    started = time.perf_counter()
    forced = await client.post("/api/offers/list-by-category", json=payload, headers=forced_headers)
    elapsed_ms = (time.perf_counter() - started) * 1000
    print(
        "conditional_forced: "
        f"status={forced.status_code} ms={elapsed_ms:.1f} "
        f"content_type={forced.headers.get('content-type')}"
    )
    print("conditional_support: no 304 support observed" if forced.status_code != 304 else "conditional_support: 304")
    print()


async def run_payload_probes(client: httpx.AsyncClient, args: argparse.Namespace) -> None:
    limits = [int(item) for item in parse_csv(args.limits) if item.isdigit()]
    offsets = [int(item) for item in parse_csv(args.offsets) if item.isdigit()]

    print("Payload limit probes:")
    for limit in limits:
        await asyncio.sleep(args.sleep)
        await run_probe(
            client,
            name=f"limit_{limit}",
            payload=_market_offers_api_payload(position_amount=args.position, limit=limit),
        )

    print()
    print("Batching probes:")
    base_payload = {
        "categoryId": STARVELL_CATEGORY_ID,
        "attributes": [],
        "numericRangeFilters": [],
        "offset": 0,
        "sortBy": "price",
        "sortDir": "ASC",
        "sortByPriceAndBumped": True,
        "withCompletionRates": True,
    }
    await asyncio.sleep(args.sleep)
    await run_probe(
        client,
        name="multi_subCategoryId_array",
        payload={
            **base_payload,
            "subCategoryId": [
                STARVELL_ROBUX_SUBCATEGORY_IDS[500],
                STARVELL_ROBUX_SUBCATEGORY_IDS[800],
            ],
            "limit": 100,
        },
    )
    await asyncio.sleep(args.sleep)
    await run_probe(
        client,
        name="multi_subCategoryIds_array",
        payload={
            **base_payload,
            "subCategoryIds": [
                STARVELL_ROBUX_SUBCATEGORY_IDS[500],
                STARVELL_ROBUX_SUBCATEGORY_IDS[800],
            ],
            "limit": 100,
        },
    )

    seen: set[int] = set()
    print()
    print("Category-wide offset probes:")
    for offset in offsets:
        await asyncio.sleep(args.sleep)
        result = await run_probe(
            client,
            name=f"category_wide_offset_{offset}",
            payload={**base_payload, "limit": 150, "offset": offset},
        )
        seen.update(unique_amounts(extract_items(safe_json(result.response))))
    missing = sorted(set(TARGET_AMOUNTS) - seen)
    print(f"category_wide_target_seen={sorted(seen & set(TARGET_AMOUNTS))}")
    print(f"category_wide_target_missing={missing}")
    print()

    await asyncio.sleep(args.sleep)
    await run_probe(
        client,
        name="fields_minimal_probe",
        payload={
            **_market_offers_api_payload(position_amount=args.position, limit=100),
            "fields": ["id", "price", "availability", "subCategory", "user"],
        },
    )


def safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def extract_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "items", "offers", "results", "list"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                nested = extract_items(value)
                if nested:
                    return nested
    return []


def unique_amounts(items: Sequence[dict[str, Any]]) -> list[int]:
    seen: list[int] = []
    for item in items:
        subcategory = item.get("subCategory")
        name = subcategory.get("name", "") if isinstance(subcategory, dict) else ""
        match = re.search(r"(\d+)", str(name))
        if not match:
            continue
        amount = int(match.group(1))
        if amount not in seen:
            seen.append(amount)
    return seen


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
