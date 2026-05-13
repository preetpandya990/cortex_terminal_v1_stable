# Upstox API V3 Complete Reference

Last updated: May 11, 2026

## Scope
This document summarizes what Upstox API **V3** currently provides, based on official Upstox Developer API docs and announcements.

Important: Upstox still uses OAuth/token endpoints under v2-style auth flow for access tokens, while business/trading/data APIs are progressively available in V3.

## 1) Core V3 APIs Currently Documented

### 1.1 Orders V3 (Trading)
Base host used in examples: `https://api-hft.upstox.com`

1. Place Order V3
- Endpoint: `POST /v3/order/place`
- Purpose: place exchange orders, AMO support, order tagging, optional auto-slicing (`slice` flag), latency metadata in response.
- Sandbox: yes (documented as sandbox enabled)

2. Modify Order V3
- Endpoint: `PUT /v3/order/modify`
- Purpose: modify pending/open orders by `order_id`; latency metadata included.
- Sandbox: yes

3. Cancel Order V3
- Endpoint: `DELETE /v3/order/cancel?order_id=...`
- Purpose: cancel pending/open order; works for regular + AMO; latency metadata included.
- Sandbox: yes

### 1.2 Market Quote V3 (REST)
Base host used in examples: `https://api.upstox.com`

1. LTP Quotes V3
- Endpoint: `GET /v3/market-quote/ltp`
- Input: comma-separated `instrument_key`
- V3 additions called out: `ltq`, `volume`, `cp`

2. OHLC Quotes V3
- Endpoint: `GET /v3/market-quote/ohlc`
- V3 additions called out: `live_ohlc`, `prev_ohlc`, `volume`, `ts`

### 1.3 Historical Data V3

1. Historical Candle Data V3
- Endpoint: V3 historical candle API page
- Supports configurable units and intervals.
- Units noted: minutes, hours, days, weeks, months.

2. Intraday Candle Data V3
- Endpoint: V3 intraday candle API page
- Supports configurable intraday aggregation with broader interval controls.

### 1.4 Market Data Feed V3 (WebSocket)

1. Market Data Feed V3
- Transport: WebSocket (`wss://`)
- Payload format: Protobuf (decode using Upstox-provided Market Data V3 proto definition)
- Subscription modes documented: `ltpc`, `option_greeks`, `full`, `full_d30`
- Behavior: includes heartbeat (`ping` frames) when idle
- Notes: docs highlight explicit connection/subscription limits

2. Market Data Feed Authorize URL V3
- Endpoint page: authorize URL API for V3 feed
- Purpose: get one-time authorized redirect URI for socket connection

### 1.5 Funds & Margin V3

1. Get Funds and Margin V3
- Endpoint: `GET /v3/user/get-funds-and-margin`
- Effective date in announcement: April 10, 2026
- Key V3 change: no segment parameter; unified multi-segment response
- Detailed response buckets: `available_to_trade` and `unavailable_to_trade` with cash + pledge breakdowns

## 2) V3 Feature Improvements Announced

### 2.1 Enhanced candle flexibility
Announcement states expanded unit/interval options in V3 intraday/historical candle APIs.

### 2.2 Richer market quote payloads
V3 quote endpoints add data points like `ltq`, `volume`, `cp`, and live/previous OHLC structures.

### 2.3 Order latency metadata
Orders V3 responses include metadata latency fields for place/modify/cancel processing visibility.

## 3) Operational & Compliance Context (Critical)

### 3.1 Regulatory update live from April 1, 2026
Upstox announced regulatory changes for API/algo trading (community update dated March 31, 2026, effective April 1, 2026).

### 3.2 Static IP controls
Order APIs may reject calls unless static IP/whitelisting requirements are met (error examples include static IP restriction codes).

### 3.3 API market order / risk controls
Market order and market-protection behavior is constrained by exchange/regulatory policy and may produce specific validation errors.

### 3.4 Access tier restrictions
Some error messages indicate order operations may require Uplink Business access for specific accounts.

## 4) Rate Limits (How to plan usage)
Upstox publishes rate limits under a dedicated page with separate buckets for:
1. order placement API family,
2. multi-order APIs,
3. standard API calls.

Design recommendation:
- enforce client-side per-second and burst throttling,
- isolate order and data buckets,
- add exponential backoff + idempotency discipline for retries.

## 5) Auth Model with V3 Usage
Even when using V3 resources:
1. obtain access token via Upstox authentication flow,
2. pass `Authorization: Bearer <token>`,
3. include required headers per endpoint (`Accept`, sometimes `Content-Type`, and version header where required by page examples).

## 6) Sandbox and SDK Availability
1. Sandbox is available and explicitly covers core order workflows including V3 variants.
2. Official SDK samples are provided for Python, Node.js, Java, and PHP across major V3 pages.

## 7) What V3 Does NOT Clearly Expose as Dedicated Fields
From currently visible quote docs:
1. No explicit dedicated field documented for `52_week_high` / `52_week_low` in V3 quote endpoints.
2. Typical approach is deriving 52-week range from historical candles.

## 8) Known V3 Endpoint Inventory (Quick Table)

| Area | API |
|---|---|
| Orders | Place Order V3 |
| Orders | Modify Order V3 |
| Orders | Cancel Order V3 |
| Market Quote | LTP Quotes V3 |
| Market Quote | OHLC Quotes V3 |
| Historical Data | Historical Candle Data V3 |
| Historical Data | Intraday Candle Data V3 |
| WebSocket | Market Data Feed V3 |
| WebSocket | Market Data Feed Authorize URL V3 |
| User Funds | Get Funds and Margin V3 |

## 9) Official Sources

1. Developer API index: https://upstox.com/developer/api-documentation/open-api/
2. Place Order V3: https://upstox.com/developer/api-documentation/v3/place-order
3. Modify Order V3: https://upstox.com/developer/api-documentation/v3/modify-order
4. Cancel Order V3: https://upstox.com/developer/api-documentation/v3/cancel-order/
5. LTP Quotes V3: https://upstox.com/developer/api-documentation/ltp-v3/
6. OHLC Quotes V3: https://upstox.com/developer/api-documentation/get-market-quote-ohlc-v3/
7. Historical Candle Data V3: https://upstox.com/developer/api-documentation/v3/get-historical-candle-data/
8. Intraday Candle Data V3: https://upstox.com/developer/api-documentation/v3/get-intra-day-candle-data
9. Market Data Feed V3: https://upstox.com/developer/api-documentation/v3/get-market-data-feed
10. Market Data Feed Authorize URL V3: https://upstox.com/developer/api-documentation/get-market-data-feed-authorize-v3
11. Get Funds and Margin V3: https://upstox.com/developer/api-documentation/get-funds-and-margin-v3
12. Funds V3 announcement: https://upstox.com/developer/api-documentation/announcements/get-funds-and-margin-v3
13. Candle APIs V3 announcement: https://upstox.com/developer/api-documentation/announcements/enhanced-historical-candle-data-apis-v3
14. Rate limits: https://upstox.com/developer/api-documentation/rate-limiting/
15. Regulatory update (community): https://community.upstox.com/t/important-update-regulatory-changes-for-api-and-algo-trading-are-now-live/14874

## 10) Implementation Checklist (Practical)
1. Complete OAuth/token flow and secure token refresh.
2. Validate account entitlements (Uplink/static IP/compliance) before order rollout.
3. Implement per-bucket throttling from published limits.
4. Implement V3 order APIs with robust error mapping and idempotent retry logic.
5. Build websocket client with protobuf decoding, reconnect, resubscribe, and mode switching.
6. Derive analytics fields (e.g., 52-week high/low) from V3 historical candles where not directly provided.
7. Run full sandbox validation before production.
