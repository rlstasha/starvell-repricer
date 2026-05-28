# Lightweight price watcher research

Date: 2026-05-28

## Goal

Explore whether a separate read-only watcher can detect competitor price changes faster
than the main repricer loop without changing price strategy or price write behavior.

This document is research only. No watcher is enabled in production.

## Candidate architecture

```text
price_watcher process
  -> read-only market/offer requests
  -> detect competitor price change
  -> Redis event: price_change_event:{position_amount}
  -> scheduler wakes the affected position early
  -> existing repricer cycle validates market and writes through StarvellClient
```

Safety rules:

```text
watcher never writes prices
watcher does not bypass min/max/step strategy
watcher does not call the price update endpoint
watcher should use a separate proxy/session only if explicitly configured
watcher must be disabled by default
```

## Lightweight endpoint search

Public frontend bundle search found these relevant read endpoints:

```text
POST /api/offers/list-by-category
GET/POST /api/offers/list
GET/POST /api/offers/list-my
Next.js data: /_next/data/{buildId}/offers/{offer_id}.json
```

No confirmed lightweight endpoint returning only a single offer price was found:

```text
/api/offers/{id}/price: not found
/api/offers/{id}/status: not found
/api/lots/{id}/price: not found
/api/prices/...: not found
```

## Practical options

### Option A: watcher uses `/api/offers/list-by-category`

Pros:

```text
known working endpoint
contains all competitor data needed for comparison
same source as browser market page
```

Cons:

```text
not lightweight
uses the same endpoint family that can trigger 429
needs its own careful limiter or separate proxy/account budget
```

### Option B: watcher uses Next.js offer JSON

Pros:

```text
GET request
can target a specific offer page
may be smaller than list-by-category in some cases
```

Cons:

```text
Next data appears when opening a specific offer, not as market realtime
may be stale or cache-shaped
requires knowing competitor offer ids in advance
does not cover new competitors appearing in category
```

### Option C: Starvell owner provides price-only endpoint or socket event

Pros:

```text
best latency
lowest request cost
can wake scheduler in 0.1-0.3 seconds
```

Cons:

```text
not currently found in public frontend
requires owner contract/documentation
```

## Recommended next step

Do not add a production watcher yet.

Ask Starvell owner whether one of these exists:

```text
GET /api/offers/{offer_id}/price
GET /api/lots/{lot_id}/price
POST /api/offers/prices
Socket.IO offer_price_updated event
SSE /api/offers/stream
```

If no lightweight source exists, the safest watcher design is:

```text
disabled by default
separate process
separate Redis wake-up key
strict read-only code
uses list-by-category with a small watched-position set
shares no price-write path
```

## Expected impact

With a real price-only source:

```text
500R wake-up latency: 0.1-0.3s possible
main repricer request budget: preserved
429 risk: low if watcher source is separate/lightweight
```

With only list-by-category:

```text
latency improvement: modest
429 risk: medium unless using separate proxy/session and conservative limiter
```

