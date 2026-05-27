# Performance tuning notes

## Safe fast worker benchmark

Date: 2026-05-27

These values were tested as runtime overrides for the worker. They are not hard defaults.

```env
REQUEST_MIN_DELAY_MS=300
REQUEST_JITTER_MS=200
FAST1_MIN_DELAY_MS=200
FAST1_JITTER_MS=100
MARKET_HTTP2_ENABLED=false
SCHEDULER_MAX_CONCURRENT_POSITIONS=1
ULTRA_FAST_MIN_INTERVAL_SECONDS=0.5
FAST1_MIN_INTERVAL_SECONDS=0.8
```

10-minute live result:

```text
requests/min: 263.0
price_updated/min: 34.4
skipped/min: 90.4
failed/min: 0.0
price_updated per 100 requests: 13.08
429 count: 0
effective limit reductions: 0
500R cycle_total_ms avg: 434.45
500R cycle_total_ms p95: 1106.09
parallel_fetch_enabled=true: 789 cycles
parallel_fetch_enabled=false: 459 cycles
```

If 429 appears with these values, raise:

```env
FAST1_MIN_DELAY_MS=250
```

and repeat the 10-minute check.

## Aggressive interval benchmark

Date: 2026-05-27

All modes below were tested with temporary compose overrides only. Safe defaults remain:

```env
REQUEST_MIN_DELAY_MS=300
REQUEST_JITTER_MS=200
MARKET_HTTP2_ENABLED=false
SCHEDULER_MAX_CONCURRENT_POSITIONS=1
```

| Mode | 429 | req/min | max req/60s | profile max/60s | updated/min | skipped/min | failed/min | updated/100 req | 500R avg/p95 ms | parallel true | own lot cache hit | verdict |
| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |
| baseline 299afc2, ultra=0.5, fast1 delay=200 | 0 | 263.0 | n/a | n/a | 34.4 | 90.4 | 0.0 | 13.08 | 434 / 1106 | 63.2% | 63.2% | safe baseline |
| ultra=0.4, fast1 delay=200, concurrency=1 | 0 | 271.2 | 302 | fast1 110, fast2 97, slow 100 | 50.2 | 39.1 | 0.0 | 18.49 | 1169 / 3014 | 62.2% | 62.4% | productive but borderline |
| ultra=0.3, fast1 delay=150, concurrency=1 | 0 | 264.6 | 295 | fast1 111, fast2 96, slow 97 | 42.7 | 60.6 | 0.0 | 16.14 | 1029 / 2819 | 64.1% | 64.1% | not useful enough |
| ultra=0.4, fast1 delay=200, concurrency=2 | 0 | 327.2 | 375 | fast1 122, fast2 134, slow 148 | 61.6 | 55.1 | 0.0 | 18.84 | 840 / 2627 | 73.7% | 74.0% | unsafe, exceeds cap |

Additional checks:

```text
rate_limiter wait count: 0 in all benchmark windows
limit_wait_ms avg/p95: 0/0 in all benchmark windows
backoff_after_429 count: 0 in all benchmark windows
proxy_connect_error count: 0 in all benchmark windows
```

Notes:

- `ultra=0.4` with concurrency 1 produced the best useful update rate without 429, but the computed sliding 60-second window briefly reached `302`, and fast1 reached `110/60s`. Treat it as a candidate for supervised testing, not a default.
- `ultra=0.3` with a lower fast1 delay did not improve useful throughput: updates dropped and skipped increased.
- `concurrency=2` improved raw throughput and updates but exceeded the account/profile safety envelope. Do not use it without a stricter predictive limiter.
- Stable 500R reaction under `0.5s` was not reached safely. Individual hot-cache cycles can be sub-second, but p95 is dominated by market latency and price-update context/write latency.
