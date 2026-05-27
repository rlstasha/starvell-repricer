from __future__ import annotations

import datetime as dt
import json
import math
import statistics
import sys
from collections import Counter, defaultdict, deque
from typing import Any


def main() -> None:
    objects = list(_iter_json_log_objects(sys.stdin))
    print(json.dumps(summarize(objects), ensure_ascii=False, indent=2, sort_keys=True))


def summarize(objects: list[dict[str, Any]]) -> dict[str, Any]:
    timestamps = [_parse_ts(item.get("timestamp")) for item in objects]
    timestamps = [item for item in timestamps if item is not None]
    start = min(timestamps, default=None)
    end = max(timestamps, default=None)
    minutes = max((end - start) / 60, 1e-9) if start and end else 5.0

    events = Counter(str(item.get("event")) for item in objects)
    requests: list[float] = []
    profile_requests: dict[str, list[float]] = defaultdict(list)
    status_codes: Counter[str] = Counter()
    request_types: Counter[str] = Counter()
    profile_counts: Counter[str] = Counter()
    wait_ms: list[float] = []
    proxy_errors_by_profile: Counter[str] = Counter()
    proxy_errors_by_request_type: Counter[str] = Counter()

    for item in objects:
        event = item.get("event")
        if event == "rate_limiter_wait" and item.get("wait_ms") is not None:
            try:
                wait_ms.append(float(item["wait_ms"]))
            except (TypeError, ValueError):
                pass
        if event == "starvell_http_request":
            timestamp = _parse_ts(item.get("timestamp"))
            if timestamp is not None:
                requests.append(timestamp)
                profile_requests[str(item.get("proxy_profile") or "unknown")].append(timestamp)
            request_types[str(item.get("request_type") or "unknown")] += 1
            profile_counts[str(item.get("proxy_profile") or "unknown")] += 1
            status_codes[str(item.get("status_code"))] += 1
        if event in {"starvell_proxy_transport_error", "starvell_http_transport_error"}:
            proxy_errors_by_profile[str(item.get("proxy_profile") or "unknown")] += 1
            proxy_errors_by_request_type[str(item.get("request_type") or "unknown")] += 1

    cycles = [item for item in objects if item.get("event") == "repricer_cycle_profile"]
    updates = [item for item in objects if item.get("event") == "price_updated"]
    skipped = [item for item in cycles if item.get("status") == "skipped"]
    failed = [
        item
        for item in objects
        if item.get("event") == "price_update_failed"
        or (item.get("event") == "repricer_cycle_profile" and item.get("status") not in {"success", "skipped"})
    ]

    parallel = Counter(str(item.get("parallel_fetch_enabled")) for item in cycles)
    cycle_by_position = _cycle_stats_by_position(cycles)
    diag500 = [item for item in objects if item.get("event") == "repricer_500_parallel_fetch_diagnostics"]
    diag500_parallel = Counter(str(item.get("parallel_fetch_enabled")) for item in diag500)
    diag500_false_reasons = Counter(
        str(item.get("parallel_fetch_disabled_reason"))
        for item in diag500
        if not item.get("parallel_fetch_enabled")
    )

    return {
        "minutes": round(minutes, 2),
        "requests_total": len(requests),
        "requests_per_min": round(len(requests) / minutes, 2),
        "max_requests_last_60s": _sliding_max(requests),
        "profile_requests_last_60s_max": {
            profile: _sliding_max(times)
            for profile, times in sorted(profile_requests.items())
        },
        "status_codes": dict(status_codes),
        "request_types": dict(request_types),
        "profile_requests": dict(profile_counts),
        "price_updated": len(updates),
        "price_updated_per_min": round(len(updates) / minutes, 2),
        "skipped": len(skipped),
        "skipped_per_min": round(len(skipped) / minutes, 2),
        "failed": len(failed),
        "failed_per_min": round(len(failed) / minutes, 2),
        "useful_updates_per_100_requests": (
            round(len(updates) / len(requests) * 100, 2)
            if requests
            else 0.0
        ),
        "cycle_profiles": len(cycles),
        "cycle_total_ms_by_position": cycle_by_position,
        "parallel_fetch": dict(parallel),
        "parallel_fetch_true_pct": (
            round(parallel["True"] / len(cycles) * 100, 2)
            if cycles
            else 0.0
        ),
        "own_lot_cache_hits": events["starvell_own_lot_cache_hit"],
        "own_lot_cache_hit_pct_of_cycles": (
            round(events["starvell_own_lot_cache_hit"] / len(cycles) * 100, 2)
            if cycles
            else 0.0
        ),
        "500_parallel_diagnostics": dict(diag500_parallel),
        "500_parallel_false_reasons": dict(diag500_false_reasons),
        "500_parallel_true_pct": (
            round(diag500_parallel["True"] / len(diag500) * 100, 2)
            if diag500
            else None
        ),
        "rate_limiter_wait_count": events["rate_limiter_wait"],
        "limit_wait_ms_avg": round(statistics.mean(wait_ms), 2) if wait_ms else 0.0,
        "limit_wait_ms_p95": round(_percentile(wait_ms, 95), 2) if wait_ms else 0.0,
        "backoff_after_429_count": sum(1 for item in objects if item.get("reason") == "backoff_after_429"),
        "proxy_transport_error_count": (
            events["starvell_proxy_transport_error"] + events["starvell_http_transport_error"]
        ),
        "proxy_transport_errors_by_profile": dict(proxy_errors_by_profile),
        "proxy_transport_errors_by_request_type": dict(proxy_errors_by_request_type),
        "proxy_connect_error_count": sum(1 for item in objects if "proxy_connect_error" in str(item).lower()),
        "rate_limited_count": sum(1 for item in objects if "rate_limited" in str(item).lower()),
        "price_update_failed_count": events["price_update_failed"],
    }


def _cycle_stats_by_position(cycles: list[dict[str, Any]]) -> dict[str, dict[str, float | int | None]]:
    values: dict[str, list[float]] = defaultdict(list)
    for item in cycles:
        position = item.get("position")
        if position is None or item.get("cycle_total_ms") is None:
            continue
        try:
            values[str(position)].append(float(item["cycle_total_ms"]))
        except (TypeError, ValueError):
            continue
    return {
        position: {
            "count": len(items),
            "avg": round(statistics.mean(items), 2),
            "p95": round(_percentile(items, 95), 2),
            "min": round(min(items), 2),
            "max": round(max(items), 2),
        }
        for position, items in sorted(values.items(), key=lambda pair: int(pair[0]))
    }


def _iter_json_log_objects(lines) -> Any:
    for line in lines:
        text = line.rstrip("\n")
        if "|" in text:
            text = text.split("|", 1)[1].strip()
        if not text.startswith("{"):
            continue
        try:
            yield json.loads(text)
        except json.JSONDecodeError:
            continue


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    items = sorted(values)
    if len(items) == 1:
        return items[0]
    k = (len(items) - 1) * percentile / 100
    floor = math.floor(k)
    ceil = math.ceil(k)
    if floor == ceil:
        return items[int(k)]
    return items[floor] * (ceil - k) + items[ceil] * (k - floor)


def _sliding_max(times: list[float], window_seconds: float = 60.0) -> int:
    items = sorted(times)
    queue: deque[float] = deque()
    maximum = 0
    for timestamp in items:
        queue.append(timestamp)
        while queue and timestamp - queue[0] > window_seconds:
            queue.popleft()
        maximum = max(maximum, len(queue))
    return maximum


if __name__ == "__main__":
    main()
