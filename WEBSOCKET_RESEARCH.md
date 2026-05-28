# Starvell WebSocket / SSE research

Date: 2026-05-28

## Goal

Find whether Starvell exposes a realtime market price event that can replace or accelerate
HTTP polling through `/api/offers/list-by-category`.

This document is research only. No repricer production flow, scheduler, price strategy,
proxy logic, or price write endpoint is changed by this research.

## Confirmed Socket.IO transport

Known Socket.IO endpoint:

```text
wss://starvell.com/socket.io/?EIO=4&transport=websocket
```

The browser and diagnostic scripts confirm:

```text
SID: received
ping/pong: works
transport: websocket
EIO: 4
```

## Namespaces found in frontend bundles

The public Next.js bundle contains this websocket namespace map:

```text
/chats
/user-notifications
/online
/user-presence
/viewed-offers
/orders
```

The frontend does not expose `/offers`, `/market`, `/prices`, or `/lots` in the same
namespace configuration. Prior raw namespace probes showed `/offers` is rejected as an
invalid namespace.

## Socket event names found in frontend bundles

The public bundle contains these Socket.IO event constants:

```text
message_created
messages_deleted
chat_read
balance_updated
purchase_update
sale_update
user_presence_update
typing
viewed_offer
telegram_linked
phone_linked
order_updated
order_dispute_updated
support_ticket_replied
support_ticket_status_updated
kyc_status_updated
```

Market-style event names searched for and not found in the decoded public JS:

```text
offer_updated
offers_updated
price_updated
price_update
market_update
market_updated
lot_updated
category_update
```

## `/viewed-offers`

The frontend subscribes to `/viewed-offers` and handles:

```text
viewed_offer
```

Observed handler shape:

```text
payload fields used by frontend: offer, buyerId
```

The diagnostics seen so far show that `viewed_offer` is not a market price stream:
it identifies viewed offers and buyer presence, but no reliable realtime price field
has been observed.

## `/orders`, `/chats`, `/user-presence`

Observed browser frames include subscription-like events:

```text
42/orders,["order_subscribe", {...}]
42/chats,["typing_subscribe", {...}]
42/user-presence,["watch", {...}]
```

These prove Starvell uses explicit Socket.IO subscriptions in some namespaces, but the
public bundles did not reveal an equivalent market price subscription.

`sale_update` and order events are user/account notifications, not public market price
updates for competitor offers.

## SSE / EventSource

Searches in the decoded public JS found no market SSE usage:

```text
EventSource: not found
text/event-stream: not found
offer/price stream endpoint: not found
```

Some transport/polyfill strings exist in dependencies, but not as Starvell market
application code.

## Main market source still used by frontend

The frontend market page requests:

```text
POST /api/offers/list-by-category
```

The request params include:

```text
subCategoryId
attributes
instantDelivery
autoDelivery
onlyOnlineUsers
keyword
numericRangeFilters
sortBy / sortDir
sortByPriceAndBumped
disableRecommendedOffers
withCompletionRates=true
```

This endpoint remains the only confirmed source that returns competitor offer ids,
prices, availability, seller info, badges, rating, reviews, and subcategory data.

## Conclusion

No confirmed realtime event with competitor price data has been found.

Current best source for repricing remains:

```text
POST /api/offers/list-by-category
```

Socket.IO can stay as optional diagnostics or future accelerator, but it should not be
used as the primary market source until Starvell provides a real market price event.

## What to ask Starvell owner

Ask for the exact public market update contract:

```text
namespace:
event name:
subscribe event:
subscribe payload:
payload fields:
does it include price?
does it include offer id / lot id?
does it include subCategoryId?
is it emitted for all price changes or only viewed offers?
does it require auth cookie/session?
```

Most useful answer would be a concrete example:

```text
client -> 42/viewed-offers,["subscribe", {"subCategoryId": 333}]
server -> 42/viewed-offers,["offer_price_updated", {"offerId": 222760, "price": "306.30", "subCategoryId": 333}]
```

