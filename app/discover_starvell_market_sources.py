import argparse
import asyncio
import html
import json
import re
import statistics
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.check_starvell_market_source import (
    INTERESTING_HEADERS,
    extract_items,
    parse_csv,
)
from app.core.config import get_settings
from app.market.client import _market_offers_api_payload


INTERESTING_TERMS = (
    "offer",
    "offers",
    "product",
    "products",
    "item",
    "items",
    "lot",
    "lots",
    "category",
    "subcategory",
    "price",
    "view",
    "viewed",
    "fetch",
    "load",
    "details",
    "market",
    "live",
    "socket",
    "stream",
    "update",
    "watch",
    "subscribe",
)
MUTATING_TERMS = (
    "update",
    "delete",
    "remove",
    "create",
    "buy",
    "purchase",
    "order",
    "checkout",
    "payment",
    "send",
    "message",
)
SECRET_WORDS = ("cookie", "token", "session", "password", "csrf", "authorization", "proxy")
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


@dataclass(frozen=True)
class EndpointCandidate:
    method: str
    url: str
    source: str
    reason: str = ""


@dataclass
class DiscoverySummary:
    endpoints: list[EndpointCandidate] = field(default_factory=list)
    socket_events: list[str] = field(default_factory=list)
    eventsource_urls: list[str] = field(default_factory=list)
    next_build_id: str | None = None
    scripts: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    name: str
    method: str
    url: str
    status_code: int | None
    elapsed_ms: float
    response_size: int
    headers: dict[str, str]
    error: str = ""
    useful_hit: bool = False


@dataclass
class BenchmarkSummary:
    name: str
    method: str
    url: str
    requests: int
    avg_ms: float
    p95_ms: float
    response_size_avg: int
    errors: int
    status_codes: dict[int | str, int]
    cache_headers: dict[str, str]
    useful_hits: int


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only discovery for alternative Starvell market data sources."
    )
    parser.add_argument("--positions", default="500,800,1000")
    parser.add_argument("--offer-ids", default="2000,2002,2003")
    parser.add_argument("--bench-runs", type=int, default=3)
    parser.add_argument("--sleep", type=float, default=0.7)
    parser.add_argument("--max-js-assets", type=int, default=30)
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument("--proxy-profile", default="")
    parser.add_argument(
        "--auth-cookie",
        default="",
        help="Optional browser-exported cookie. Value is never printed.",
    )
    parser.add_argument(
        "--pages",
        default="",
        help="Extra comma-separated read-only pages to scan, e.g. /roblox/packages,/offers/2000.",
    )
    parser.add_argument(
        "--include-discovered-get",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Benchmark safe discovered GET candidates.",
    )
    parser.add_argument("--debug", action="store_true")
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    positions = parse_int_csv(args.positions)
    offer_ids = parse_csv(args.offer_ids)
    cookie = browser_or_env_cookie(args.auth_cookie, settings.market_session_cookie)
    proxy_url = settings.proxy_url_for_group(args.proxy_profile) if args.proxy_profile else None

    print("Starvell market source discovery")
    print("Mode: read-only diagnostics. Repricer flow is not changed.")
    print(f"Auth cookie: {'configured' if cookie else 'not configured'}")
    print(f"Proxy profile: {args.proxy_profile or 'direct'}")
    print(f"Positions: {', '.join(map(str, positions))}")
    print(f"Offer IDs: {', '.join(offer_ids) if offer_ids else 'none'}")
    print()

    headers = browser_headers(cookie=cookie, base_url=settings.market_base_url)
    async with httpx.AsyncClient(
        base_url=settings.market_base_url,
        headers=headers,
        timeout=30,
        proxy=proxy_url,
        follow_redirects=True,
    ) as client:
        discovery = await discover_from_pages(
            client,
            settings=settings,
            offer_ids=offer_ids,
            extra_pages=parse_csv(args.pages),
            max_js_assets=max(args.max_js_assets, 0),
            debug=args.debug,
        )
        print_discovery(discovery)

        sources = build_benchmark_sources(
            settings=settings,
            discovery=discovery,
            positions=positions,
            offer_ids=offer_ids,
            include_discovered_get=args.include_discovered_get,
            max_candidates=max(args.max_candidates, 0),
        )
        summaries = await benchmark_sources(
            client,
            sources=sources,
            runs=max(args.bench_runs, 1),
            sleep_seconds=max(args.sleep, 0),
        )

    print_benchmark_report(summaries)
    print_recommendation(summaries, discovery)
    return 0


async def discover_from_pages(
    client: httpx.AsyncClient,
    *,
    settings: Any,
    offer_ids: Sequence[str],
    extra_pages: Sequence[str],
    max_js_assets: int,
    debug: bool,
) -> DiscoverySummary:
    summary = DiscoverySummary()
    pages = default_pages(settings=settings, offer_ids=offer_ids, extra_pages=extra_pages)
    seen_scripts: set[str] = set()

    for page in pages:
        result, text = await fetch_text(client, page)
        result.name = f"page:{page}"
        print_fetch_line(result)
        if result.error or result.status_code is None or result.status_code >= 400:
            continue
        page_discovery = extract_market_source_candidates(text, source=page)
        merge_discovery(summary, page_discovery)

        build_id = extract_next_build_id(text)
        if build_id and not summary.next_build_id:
            summary.next_build_id = build_id

        for script in extract_script_sources(text, base_url=settings.market_base_url):
            if script in seen_scripts:
                continue
            seen_scripts.add(script)
            summary.scripts.append(script)

    summary.scripts = list(dict.fromkeys(summary.scripts))
    for script_url in list(summary.scripts)[:max_js_assets]:
        result, text = await fetch_text(client, script_url)
        result.name = f"script:{short_url(script_url)}"
        print_fetch_line(result)
        if result.error or result.status_code is None or result.status_code >= 400:
            continue
        script_discovery = extract_market_source_candidates(text, source=script_url)
        merge_discovery(summary, script_discovery)
        if debug and script_discovery.endpoints:
            print(f"  script candidates: {len(script_discovery.endpoints)}")

    if summary.next_build_id:
        for offer_id in offer_ids:
            summary.endpoints.append(
                EndpointCandidate(
                    method="GET",
                    url=f"/_next/data/{summary.next_build_id}/offers/{offer_id}.json",
                    source="generated_next_data",
                    reason="offer_json_candidate",
                )
            )

    dedupe_discovery(summary)
    return summary


def default_pages(*, settings: Any, offer_ids: Sequence[str], extra_pages: Sequence[str]) -> list[str]:
    pages = [settings.market_offers_url or "/roblox/packages", "/roblox/packages"]
    pages.extend(f"/offers/{offer_id}" for offer_id in offer_ids if str(offer_id).strip())
    if settings.own_seller_id:
        pages.append(f"/users/{settings.own_seller_id}")
    pages.extend(extra_pages)
    return list(dict.fromkeys(normalize_relative_url(page) for page in pages if page))


def browser_headers(*, cookie: str, base_url: str) -> dict[str, str]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Origin": base_url.rstrip("/"),
        "Referer": f"{base_url.rstrip('/')}/roblox/packages",
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if cookie:
        headers["Cookie"] = cookie
    return headers


def browser_or_env_cookie(auth_cookie: str, session_cookie: str) -> str:
    if auth_cookie.strip():
        return auth_cookie.strip()
    cookie = session_cookie.strip()
    if not cookie:
        return ""
    if "=" in cookie or ";" in cookie:
        return cookie
    return f"session={cookie}"


async def fetch_once(
    client: httpx.AsyncClient,
    *,
    name: str,
    method: str,
    url: str,
    json_payload: dict[str, Any] | None = None,
) -> FetchResult:
    started = time.perf_counter()
    try:
        response = await client.request(method, url, json=json_payload)
        elapsed_ms = (time.perf_counter() - started) * 1000
        useful_hit = response.status_code < 400 and response_has_useful_market_data(response)
        return FetchResult(
            name=name,
            method=method,
            url=str(response.request.url),
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response_size=len(response.content),
            headers=interesting_response_headers(response.headers),
            useful_hit=useful_hit,
        )
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return FetchResult(
            name=name,
            method=method,
            url=url,
            status_code=None,
            elapsed_ms=elapsed_ms,
            response_size=0,
            headers={},
            error=safe_error(exc),
        )


async def fetch_text(client: httpx.AsyncClient, url: str) -> tuple[FetchResult, str]:
    started = time.perf_counter()
    try:
        response = await client.get(url)
        elapsed_ms = (time.perf_counter() - started) * 1000
        text = response.text
        result = FetchResult(
            name=f"GET {url}",
            method="GET",
            url=str(response.request.url),
            status_code=response.status_code,
            elapsed_ms=elapsed_ms,
            response_size=len(response.content),
            headers=interesting_response_headers(response.headers),
            useful_hit=response_has_useful_market_data(response),
        )
        return result, text
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        return (
            FetchResult(
                name=f"GET {url}",
                method="GET",
                url=url,
                status_code=None,
                elapsed_ms=elapsed_ms,
                response_size=0,
                headers={},
                error=safe_error(exc),
            ),
            "",
        )


async def benchmark_sources(
    client: httpx.AsyncClient,
    *,
    sources: Sequence[EndpointCandidate],
    runs: int,
    sleep_seconds: float,
) -> list[BenchmarkSummary]:
    summaries: list[BenchmarkSummary] = []
    print()
    print("Benchmarks")
    for source in sources:
        results: list[FetchResult] = []
        for index in range(runs):
            if index > 0 and sleep_seconds:
                await asyncio.sleep(sleep_seconds)
            json_payload = None
            url = source.url
            if source.method == "POST" and source.reason.startswith("list_by_category:"):
                position = int(source.reason.rsplit(":", 1)[-1])
                json_payload = _market_offers_api_payload(position_amount=position, limit=get_settings().market_offers_limit)
            result = await fetch_once(
                client,
                name=source.reason or source.url,
                method=source.method,
                url=url,
                json_payload=json_payload,
            )
            results.append(result)
            print_fetch_line(result)
        summaries.append(summarize_results(source, results))
    return summaries


def build_benchmark_sources(
    *,
    settings: Any,
    discovery: DiscoverySummary,
    positions: Sequence[int],
    offer_ids: Sequence[str],
    include_discovered_get: bool,
    max_candidates: int,
) -> list[EndpointCandidate]:
    sources: list[EndpointCandidate] = []
    for position in positions:
        sources.append(
            EndpointCandidate(
                method="POST",
                url=settings.market_offers_api_url,
                source="known_read_only_api",
                reason=f"list_by_category:{position}",
            )
        )
    for offer_id in offer_ids:
        sources.append(
            EndpointCandidate(
                method="GET",
                url=f"/offers/{offer_id}",
                source="offer_page",
                reason=f"offer_page:{offer_id}",
            )
        )
    if discovery.next_build_id:
        for offer_id in offer_ids:
            sources.append(
                EndpointCandidate(
                    method="GET",
                    url=f"/_next/data/{discovery.next_build_id}/offers/{offer_id}.json",
                    source="next_data",
                    reason=f"next_offer_json:{offer_id}",
                )
            )
    if include_discovered_get:
        for candidate in safe_discovered_get_candidates(discovery.endpoints)[:max_candidates]:
            sources.append(candidate)
    return dedupe_candidates(sources)


def summarize_results(source: EndpointCandidate, results: Sequence[FetchResult]) -> BenchmarkSummary:
    elapsed = [item.elapsed_ms for item in results if not item.error]
    sizes = [item.response_size for item in results if not item.error]
    status_counts: dict[int | str, int] = defaultdict(int)
    cache_headers: dict[str, str] = {}
    for item in results:
        status_counts[item.status_code if item.status_code is not None else "error"] += 1
        for key, value in item.headers.items():
            cache_headers.setdefault(key, value)
    return BenchmarkSummary(
        name=source.reason or source.url,
        method=source.method,
        url=source.url,
        requests=len(results),
        avg_ms=statistics.fmean(elapsed) if elapsed else 0.0,
        p95_ms=percentile(elapsed, 95),
        response_size_avg=round(statistics.fmean(sizes)) if sizes else 0,
        errors=sum(1 for item in results if item.error or (item.status_code is not None and item.status_code >= 400)),
        status_codes=dict(status_counts),
        cache_headers=cache_headers,
        useful_hits=sum(1 for item in results if item.useful_hit),
    )


def response_has_useful_market_data(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            payload = response.json()
        except ValueError:
            return False
        items = extract_items(payload)
        if items:
            return True
        text = json.dumps(payload, ensure_ascii=False)[:10000].casefold()
        return any(term in text for term in ("price", "offer", "subcategory", "robux", "робук"))
    text = response.text[:20000].casefold()
    return any(term in text for term in ("price", "offer", "subcategory", "robux", "робук", "__next_data__"))


def extract_market_source_candidates(text: str, *, source: str) -> DiscoverySummary:
    discovered = DiscoverySummary()
    decoded = html.unescape(text)
    discovered.next_build_id = extract_next_build_id(decoded)
    discovered.scripts = extract_script_sources(decoded, base_url="https://starvell.com")

    for match in re.finditer(
        r"(?P<method>fetch|axios\.(?:get|post|put|patch|delete))\(\s*([\"'`])(?P<url>[^\"'`]+)\2",
        decoded,
        flags=re.IGNORECASE,
    ):
        method = match.group("method").split(".")[-1].upper()
        if method == "FETCH":
            method = infer_fetch_method(decoded[match.end() : match.end() + 220])
        url = match.group("url")
        if is_interesting_url(url):
            discovered.endpoints.append(
                EndpointCandidate(method=method, url=normalize_relative_url(url), source=source, reason="fetch_or_axios")
            )

    for url in extract_quoted_urls(decoded):
        if is_interesting_url(url):
            discovered.endpoints.append(
                EndpointCandidate(method="GET", url=normalize_relative_url(url), source=source, reason="quoted_url")
            )

    for match in re.finditer(
        r"(?:new\s+)?EventSource\(\s*([\"'`])(?P<url>[^\"'`]+)\1",
        decoded,
        flags=re.IGNORECASE,
    ):
        url = normalize_relative_url(match.group("url"))
        discovered.eventsource_urls.append(url)
        discovered.endpoints.append(EndpointCandidate(method="GET", url=url, source=source, reason="eventsource"))

    for match in re.finditer(
        r"\.(?:emit|on)\(\s*([\"'`])(?P<event>[A-Za-z0-9_.:/-]+)\1",
        decoded,
        flags=re.IGNORECASE,
    ):
        event = match.group("event")
        if any(term in event.casefold() for term in INTERESTING_TERMS):
            discovered.socket_events.append(event)

    for match in re.finditer(r"\bio\(\s*([\"'`])(?P<namespace>/[A-Za-z0-9_/-]+)\1", decoded):
        namespace = match.group("namespace")
        if any(term in namespace.casefold() for term in INTERESTING_TERMS):
            discovered.socket_events.append(f"namespace:{namespace}")

    dedupe_discovery(discovered)
    return discovered


def extract_next_build_id(text: str) -> str | None:
    match = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(?P<payload>.*?)</script>',
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if match:
        try:
            payload = json.loads(html.unescape(match.group("payload")))
            build_id = payload.get("buildId")
            if isinstance(build_id, str) and build_id:
                return build_id
        except ValueError:
            pass
    match = re.search(r'"buildId"\s*:\s*"(?P<build_id>[^"]+)"', text)
    if match:
        return match.group("build_id")
    return None


def extract_script_sources(text: str, *, base_url: str) -> list[str]:
    scripts: list[str] = []
    for match in re.finditer(r"<script[^>]+\bsrc=[\"'](?P<src>[^\"']+)[\"']", text, flags=re.IGNORECASE):
        src = match.group("src")
        absolute = urljoin(base_url.rstrip("/") + "/", src)
        parsed = urlparse(absolute)
        if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
            continue
        if "/_next/static/" not in parsed.path:
            continue
        scripts.append(parsed.path + (f"?{parsed.query}" if parsed.query else ""))
    return list(dict.fromkeys(scripts))


def extract_quoted_urls(text: str) -> list[str]:
    urls: list[str] = []
    pattern = r"([\"'`])(?P<url>(?:https://starvell\.com)?/(?:api|_next/data|_next/static|offers|roblox|users|market|socket\.io)[^\"'`\\\s]*)\1"
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        urls.append(match.group("url"))
    return urls


def infer_fetch_method(trailing_text: str) -> str:
    match = re.search(r"\bmethod\s*:\s*([\"'`])(?P<method>GET|POST|PUT|PATCH|DELETE)\1", trailing_text, re.IGNORECASE)
    if match:
        return match.group("method").upper()
    return "GET"


def is_interesting_url(url: str) -> bool:
    lowered = url.casefold()
    if lowered.startswith(("http://", "https://")) and "starvell.com" not in lowered:
        return False
    return any(term in lowered for term in INTERESTING_TERMS) or lowered.startswith(("/api/", "/_next/data/"))


def safe_discovered_get_candidates(candidates: Sequence[EndpointCandidate]) -> list[EndpointCandidate]:
    safe: list[EndpointCandidate] = []
    for candidate in dedupe_candidates(candidates):
        if candidate.method != "GET":
            continue
        path = normalize_relative_url(candidate.url)
        lowered = path.casefold()
        if any(marker in lowered for marker in ("{", "}", "[", "]", "${", ":id")):
            continue
        if lowered.startswith("/api/") and any(term in lowered for term in MUTATING_TERMS):
            continue
        if lowered.startswith("/socket.io"):
            continue
        if lowered.startswith("/_next/static"):
            continue
        safe.append(EndpointCandidate(method="GET", url=path, source=candidate.source, reason=candidate.reason))
    return safe


def merge_discovery(target: DiscoverySummary, source: DiscoverySummary) -> None:
    target.endpoints.extend(source.endpoints)
    target.socket_events.extend(source.socket_events)
    target.eventsource_urls.extend(source.eventsource_urls)
    target.scripts.extend(source.scripts)
    if source.next_build_id and not target.next_build_id:
        target.next_build_id = source.next_build_id


def dedupe_discovery(summary: DiscoverySummary) -> None:
    summary.endpoints = dedupe_candidates(summary.endpoints)
    summary.socket_events = list(dict.fromkeys(summary.socket_events))
    summary.eventsource_urls = list(dict.fromkeys(summary.eventsource_urls))
    summary.scripts = list(dict.fromkeys(summary.scripts))


def dedupe_candidates(candidates: Sequence[EndpointCandidate]) -> list[EndpointCandidate]:
    seen: set[tuple[str, str]] = set()
    result: list[EndpointCandidate] = []
    for candidate in candidates:
        key = (candidate.method.upper(), normalize_relative_url(candidate.url))
        if key in seen:
            continue
        seen.add(key)
        result.append(
            EndpointCandidate(
                method=key[0],
                url=key[1],
                source=candidate.source,
                reason=candidate.reason,
            )
        )
    return result


def normalize_relative_url(url: str) -> str:
    url = html.unescape(str(url).strip())
    if not url:
        return "/"
    if url.startswith("https://starvell.com"):
        parsed = urlparse(url)
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if url.startswith("//starvell.com"):
        parsed = urlparse(f"https:{url}")
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    if not url.startswith("/"):
        return f"/{url}"
    return url


def parse_int_csv(value: str) -> list[int]:
    result: list[int] = []
    for item in parse_csv(value):
        try:
            result.append(int(item))
        except ValueError:
            continue
    return result


def percentile(values: Sequence[float], percent: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = round((percent / 100) * (len(ordered) - 1))
    return ordered[min(max(index, 0), len(ordered) - 1)]


def interesting_response_headers(headers: httpx.Headers) -> dict[str, str]:
    result: dict[str, str] = {}
    for header in INTERESTING_HEADERS:
        value = headers.get(header)
        if value is not None:
            result[header] = value
    return result


def print_fetch_line(result: FetchResult) -> None:
    status = result.status_code if result.status_code is not None else "error"
    suffix = f" error={result.error}" if result.error else ""
    print(
        f"market_source_request name={result.name} method={result.method} "
        f"status={status} ms={result.elapsed_ms:.1f} size={result.response_size}{suffix}"
    )


def print_discovery(summary: DiscoverySummary) -> None:
    print()
    print("Discovery")
    print(f"- next_build_id: {summary.next_build_id or 'not found'}")
    print(f"- js_assets: {len(summary.scripts)}")
    print(f"- endpoint_candidates: {len(summary.endpoints)}")
    print(f"- socket_events: {', '.join(summary.socket_events[:30]) if summary.socket_events else 'none'}")
    print(f"- eventsource_urls: {', '.join(summary.eventsource_urls[:10]) if summary.eventsource_urls else 'none'}")
    print("Top endpoint candidates:")
    for candidate in summary.endpoints[:40]:
        print(f"  {candidate.method} {candidate.url} source={short_url(candidate.source)} reason={candidate.reason}")


def print_benchmark_report(summaries: Sequence[BenchmarkSummary]) -> None:
    print()
    print("Benchmark summary")
    for summary in summaries:
        headers = ", ".join(f"{key}={value}" for key, value in summary.cache_headers.items()) or "none"
        print(
            f"- {summary.name}: method={summary.method} url={summary.url} "
            f"requests={summary.requests} avg={summary.avg_ms:.1f}ms p95={summary.p95_ms:.1f}ms "
            f"size_avg={summary.response_size_avg} errors={summary.errors} "
            f"useful_hits={summary.useful_hits}/{summary.requests} statuses={summary.status_codes} "
            f"cache_or_rate_headers={mask_secret_text(headers)}"
        )


def print_recommendation(summaries: Sequence[BenchmarkSummary], discovery: DiscoverySummary) -> None:
    useful = [item for item in summaries if item.useful_hits and item.errors == 0]
    useful.sort(key=lambda item: (item.p95_ms, item.response_size_avg))
    print()
    print("Recommendation")
    if useful:
        best = useful[0]
        print(f"- best_safe_source: {best.name} ({best.method} {best.url})")
        print(f"- p95_ms: {best.p95_ms:.1f}")
        print(f"- response_size_avg: {best.response_size_avg}")
    else:
        print("- best_safe_source: not found in this run")
    if any("viewed" in event or "offer" in event or "price" in event for event in discovery.socket_events):
        print("- socket_candidate: yes, inspect with app.check_starvell_socket before production use")
    else:
        print("- socket_candidate: no offer/price realtime event found in JS scan")
    print("- production_flow: unchanged")


def benchmark_summary_to_dict(summary: BenchmarkSummary) -> dict[str, Any]:
    return {
        "name": summary.name,
        "method": summary.method,
        "url": summary.url,
        "requests": summary.requests,
        "avg_ms": summary.avg_ms,
        "p95_ms": summary.p95_ms,
        "response_size_avg": summary.response_size_avg,
        "errors": summary.errors,
        "status_codes": summary.status_codes,
        "cache_headers": summary.cache_headers,
        "useful_hits": summary.useful_hits,
    }


def mask_secret_text(text: str) -> str:
    masked = str(text)
    masked = re.sub(r"(?i)(authorization\s*[=:]\s*)Bearer\s+[^,;\s]+", r"\1***", masked)
    masked = re.sub(
        r"(?i)(cookie|authorization|csrf|token|session|password|proxy)(\s*[=:]\s*)[^,;\s]+",
        r"\1\2***",
        masked,
    )
    masked = re.sub(r"(?i)(https?://)([^:/@\s]+):([^/@\s]+)@", r"\1***:***@", masked)
    return masked


def safe_error(exc: Exception) -> str:
    return mask_secret_text(f"{type(exc).__name__}: {exc}")


def short_url(url: str) -> str:
    text = normalize_relative_url(url)
    if len(text) <= 96:
        return text
    return f"{text[:92]}..."


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
