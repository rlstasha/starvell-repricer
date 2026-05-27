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

## Graduated account-limit probe

Date: 2026-05-27

Goal: find whether `GLOBAL_REQUEST_LIMIT_PER_MINUTE=300` is only an artificial ceiling.
Each step uses temporary overrides and stops when 429, limiter waits, write failures, proxy errors,
or falling useful throughput appears.

| Step | GLOBAL/ACCOUNT | concurrency | ultra | fast1 delay | TTL | 429 | max req/60s | updated/min | skipped/min | wait count | backoff 429 | useful/100 req | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 350 | 350 | 2 | 0.4 | 200ms | 10s | 0 | 385 | 59.9 | 73.1 | 1578 | 0 | 17.51 | stop: limiter is already waiting |

Result:

```text
real_safe_ceiling: keep GLOBAL/ACCOUNT at 300 for now
reason: first graduated step had no 429, but limiter waits were high and profile windows exceeded 100/min
next higher steps tested: no, stopped by safety rule
```

The `350` step did not prove Starvell rejects 350/min, but it did prove the current worker shape
cannot use it cleanly: the limiter starts pacing heavily and profile windows go above the intended
per-proxy envelope. Raising the global/account limit without a better predictive limiter is not safe.

## Own-lot cache TTL probe

Date: 2026-05-27

Temporary override:

```env
GLOBAL_REQUEST_LIMIT_PER_MINUTE=300
ACCOUNT_EFFECTIVE_LIMIT_PER_MINUTE=300
SCHEDULER_MAX_CONCURRENT_POSITIONS=1
ULTRA_FAST_MIN_INTERVAL_SECONDS=0.4
FAST1_MIN_INTERVAL_SECONDS=0.8
FAST1_MIN_DELAY_MS=200
FAST1_JITTER_MS=100
MY_LOT_STATE_CACHE_TTL_SECONDS=30
```

5-minute result:

```text
429 count: 0
rate_limiter_wait_count: 0
price_update_failed_count: 0
max requests/60s: 295
requests/min: 268.0
price_updated/min: 52.0
skipped/min: 56.9
useful_updates_per_100_requests: 19.42
500R cycle_total_ms avg/p95: 514 / 1919 ms
parallel_fetch true: 91.7%
500R parallel_fetch true: 94.1%
500R false reasons: cache_empty=1, cache_expired=6
own_lot_cache_hit: 92.1%
```

Recommendation:

```text
best useful candidate: keep GLOBAL/ACCOUNT=300, concurrency=1, fast1 delay=200ms, ultra=0.4, own-lot TTL=30s
do not make it default without confirmation
```

Why: TTL 30 raises `parallel_fetch=true` from the previous 62-74% range to 90%+ without increasing
global/account limits or causing 429. This improves useful work per request more safely than raising
the global limit.

## Soft-cap-off Starvell ceiling probe

Date: 2026-05-27

Goal: check whether the previous `350` step was stopped by Starvell or by our own proactive limiter.
These steps used temporary overrides only:

```env
RATE_LIMITER_SOFT_CAP_ENABLED=false
SCHEDULER_MAX_CONCURRENT_POSITIONS=2
ULTRA_FAST_MIN_INTERVAL_SECONDS=0.4
FAST1_MIN_INTERVAL_SECONDS=0.8
FAST1_MIN_DELAY_MS=200
FAST1_JITTER_MS=100
MY_LOT_STATE_CACHE_TTL_SECONDS=30
```

| Step | GLOBAL/ACCOUNT | 429 | rate_limited | max req/60s | req/min | updated/min | skipped/min | wait count | backoff 429 | write failures | proxy errors | useful/100 req | 500R parallel | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 400 | 400 | 0 | 0 | 383 | 343.5 | 68.9 | 64.4 | 0 | 0 | 0 | 0 | 20.05 | 99.0% | best useful probe |
| 500 | 500 | 0 | 0 | 355 | 315.3 | 56.6 | 82.5 | 0 | 0 | 0 | 0 | 17.95 | 99.0% | less useful |
| 600 | 600 | 0 | 0 | 349 | 289.4 | 55.0 | 62.5 | 0 | 0 | 0 | 1 | 19.00 | 98.6% | stopped by proxy error |

Result:

```text
starvell_real_ceiling: not reached in this probe
highest observed clean 60-second window without 429: 383 requests
recommended production ceiling: keep GLOBAL/ACCOUNT at 300 until predictive limiter is improved
best temporary benchmark config: GLOBAL/ACCOUNT=400 with soft cap off, but only for supervised tests
```

Interpretation:

- Disabling the proactive soft cap proved the earlier wait count was internal limiter pressure, not a Starvell 429.
- Starvell did not return 429 up to the observed `383 req/60s` window.
- Higher configured limits did not automatically increase useful throughput: `500` and `600` were worse than `400`.
- `600` hit a proxy transport error, so it is not safe.
- Keep `RATE_LIMITER_SOFT_CAP_ENABLED=true` by default. Use `false` only for controlled ceiling probes.
