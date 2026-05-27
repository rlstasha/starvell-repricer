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
