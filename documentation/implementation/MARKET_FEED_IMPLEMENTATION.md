# Unified Real-Time Market Feed — Implementation Reference

**Feature:** Hawk-Eye Radar — Rate Limit Fix & Real-Time Price Architecture  
**Date:** 2026-04-28  
**Status:** Complete  

---

## 1. Problem Statement

The hawk-eye-radar page was breaching Upstox API rate limits due to three compounding issues:

### Root Causes

| # | Issue | Impact |
|---|---|---|
| 1 | `useWatchlist` instantiated independently in `page.tsx` AND `DetailPane.tsx` | Two separate polling intervals with no shared cache |
| 2 | `getLtpQuote` called once per watchlist item per poll (no batching) | N REST calls per poll instead of 1 |
| 3 | Poll interval (3s) with cache TTL (5s) → effective fetch every 6s per item | High sustained request rate |

### Rate Limit Breach Analysis

Upstox limit: **2,000 requests / 30 minutes**

| Watchlist Items | DetailPane Open | Req/min | Req/30min | vs Limit |
|---|---|---|---|---|
| 4 | Yes | 80 | 2,400 | ❌ 20% over |
| 10 | Yes | 200 | 6,000 | ❌ 3× over |
| 20 | Yes | 400 | 12,000 | ❌ 6× over |

**Break-even:** Only ≤3 watchlist items with DetailPane open stayed within limits.

---

## 2. Solution Overview

Replace all REST LTP polling with a single persistent WebSocket connection to Upstox Market Data Feed V3 (`ltpc` mode), distributed via Redis pub/sub, consumed by a singleton React context.

### Architecture

```
Upstox Market Data Feed V3 WS (ltpc mode)
        ↓ protobuf binary, per-trade
MarketFeedService  ── throttle 250ms/instrument ──→  Redis "cai:market-feed:ltpc"
        (singleton, API process)                              ↓
                                               All API pods subscribe (future-proof)
                                                             ↓
                                        /api/v1/upstox/market-feed/ws
                                                             ↓ JSON ltpc ticks
                                              WatchlistProvider (singleton React context)
                                                             ↓
                                                  PriceFeed external store
                                                             ↓ useSyncExternalStore
                                        WatchlistCard · DetailPane · LivePriceBadge
```

### What Was Eliminated

| Before | After |
|---|---|
| N REST `getLtpQuote` calls per poll (watchlist) | **0** |
| Duplicate `useWatchlist` instance in `DetailPane` | **Removed** |
| Per-instrument tick stream WS in `DetailPane` | **Removed** |
| `TickStreamService` + `/upstox/ticks/ws` endpoint | **Deleted** |

### Rate Limit Impact

| Scenario | Before | After |
|---|---|---|
| 10 items, page only | 100 req/min | **0** |
| 10 items + DetailPane open | 200 req/min | **0** |
| 10 items, 30-min window | 6,000 req | **0** |
| Budget remaining (2,000/30min) | ~0 for LTP | **2,000 available** for candles, scan, etc. |

---

## 3. Throttling Strategy (Option C — Defense in Depth)

Two independent throttle layers prevent overload regardless of Upstox tick frequency:

**Layer 1 — Backend (250ms per instrument):**  
`MarketFeedService` discards ticks arriving faster than 250ms per instrument before publishing to Redis. High-frequency stocks (NIFTY50 components can produce 100s of trades/second) are safely rate-limited server-side. Configurable via `MARKET_FEED_THROTTLE_MS` env var.

**Layer 2 — Frontend (requestAnimationFrame batching):**  
`PriceFeed.scheduleFlush()` coalesces all `update()` calls within one animation frame (~16ms at 60fps) into a single listener notification pass. Multiple instruments ticking in the same frame cause exactly one React render cycle.

**Net result:** At most 4 ticks/sec reach Redis per instrument; React renders at most at display refresh rate.

---

## 4. Files Changed

### Backend

#### Created
| File | Purpose |
|---|---|
| `backend/app/services/market_feed.py` | `MarketFeedService` — owns Upstox WS connection, ref-counted subscriptions, throttle, Redis publish, mock GBM mode |
| `backend/app/schemas/market_feed.py` | Pydantic schemas for all WS message types (client commands + server events) |

#### Modified
| File | Change |
|---|---|
| `backend/app/api/v1/upstox.py` | Added `/market-feed/ws` endpoint; removed `/ticks/ws` endpoint and all tick stream helpers; added `/ltp` REST endpoint |
| `backend/app/core/redis.py` | Added `RedisChannels.MARKET_FEED_LTPC = "cai:market-feed:ltpc"` |
| `backend/app/core/config.py` | Added `MARKET_FEED_THROTTLE_MS: int = Field(default=250, ge=100, le=5000)` |
| `backend/app/main.py` | Swapped `TickStreamService` → `MarketFeedService` in lifespan startup/shutdown |
| `backend/app/services/base_websocket.py` | Updated module docstring (stale reference to old class) |
| `backend/tests/api/test_contracts.py` | Replaced `test_stream_status_returns_shape` (tested deleted endpoint) with `test_market_feed_ws_rejects_unauthenticated` |

#### Deleted
| File | Reason |
|---|---|
| `backend/app/services/tick_stream.py` | Fully superseded by `MarketFeedService` |

### Frontend

#### Created
| File | Purpose |
|---|---|
| `frontend/src/lib/price-feed.ts` | `PriceFeed` class — framework-agnostic external store, `useSyncExternalStore`-compatible, RAF batching |
| `frontend/src/hooks/useLtp.ts` | `useLtp(instrumentKey)` hook — wraps `useSyncExternalStore` against `priceFeed` singleton |
| `frontend/src/contexts/WatchlistContext.tsx` | `WatchlistProvider` — singleton context owning WS connection, subscription ref-counting, all watchlist CRUD |

#### Modified
| File | Change |
|---|---|
| `frontend/src/hooks/useWatchlist.ts` | **Rewritten** — thin 3-line context consumer; all LTP polling code removed |
| `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx` | Removed: tick stream WS, REST LTP fetch effect, duplicate `useWatchlist` instantiation, `TickStreamStatus` type, `buildTickStreamUrl`, `getReconnectDelay`, 5 constants, `tickSocketRef`, `tickStreamStatus` state, `liveTick` state, `restPrice` state. Added: `useLtp` hook, `subscribeInstrument`/`unsubscribeInstrument` lifecycle effect |
| `frontend/src/app/hawk-eye-radar/components/WatchlistCard.tsx` | Removed `ltp`/`prevClose` props; now calls `useLtp(item.instrument_key)` internally |
| `frontend/src/app/hawk-eye-radar/page.tsx` | Removed `ltp={item.ltp}` and `prevClose={item.prevClose}` from `WatchlistCard` usage |
| `frontend/src/app/providers.tsx` | Added `WatchlistProvider` inside `AuthProvider`, wrapping `ChartPreferencesProvider` |

---

## 5. Backend Implementation Details

### 5.1 `MarketFeedService`

**Location:** `backend/app/services/market_feed.py`  
**Extends:** `BaseWebSocketService`

**Subscription lifecycle (ref-counting):**

```
subscribe_many(["KEY_A", "KEY_B"])
  → ref_counts["KEY_A"] = 1  (newly active → send sub frame to Upstox)
  → ref_counts["KEY_B"] = 1  (newly active → send sub frame to Upstox)

subscribe_many(["KEY_A"])  (DetailPane opens same instrument)
  → ref_counts["KEY_A"] = 2  (already active → no new Upstox sub frame)

unsubscribe_many(["KEY_A"])  (DetailPane closes)
  → ref_counts["KEY_A"] = 1  (still active → no Upstox unsub frame)

unsubscribe_many(["KEY_A"])  (WatchlistProvider cleanup)
  → ref_counts["KEY_A"] = 0  (deactivated → send unsub frame to Upstox)
```

**Upstox subscription frame (binary UTF-8 JSON):**
```json
{ "guid": "<uuid4>", "method": "sub",   "data": { "mode": "ltpc", "instrumentKeys": ["NSE_EQ|..."] } }
{ "guid": "<uuid4>", "method": "unsub", "data": { "instrumentKeys": ["NSE_EQ|..."] } }
```

**Tick processing pipeline:**
```
on_message(protobuf bytes)
  → FeedResponse.ParseFromString()
  → iterate feeds dict (instrument_key → Feed)
  → _extract_ltpc(feed) → (ltp, cp) | None
  → throttle check: now_ms - last_emitted[key] >= 250ms
  → asyncio.create_task(_publish(key, ltp, cp, ts))  ← fire-and-forget
```

**Mock mode (no `UPSTOX_ACCESS_TOKEN`):**  
- No Upstox WS connection is opened
- `subscribe_many` spawns a GBM asyncio task per newly activated instrument
- GBM task publishes to the same Redis channel at 250ms intervals
- `unsubscribe_many` cancels GBM tasks for deactivated instruments
- Frontend and WS endpoint are completely unaware — same Redis path

**Prometheus metrics:**
```
market_feed_subscriptions_active       Gauge   — currently subscribed instrument count
market_feed_ticks_received_total       Counter — all protobuf frames from Upstox
market_feed_ticks_emitted_total        Counter — frames passing the throttle filter
market_feed_ticks_discarded_total      Counter — frames dropped by throttle
market_feed_redis_publish_errors_total Counter — Redis publish failures
market_feed_upstox_reconnects_total    Counter — Upstox WS reconnection attempts
```

**Graceful shutdown:**
1. Cancel all GBM mock tasks
2. Send `unsub` frame for all active subscriptions
3. Close Upstox WS with code 1000
4. Clear `_ref_counts`, `_last_emitted`

### 5.2 WebSocket Endpoint `/upstox/market-feed/ws`

**Location:** `backend/app/api/v1/upstox.py`  
**Router:** `ws_router` (no auth dependency — auth handled in-handler via JWT query param)

**Connection lifecycle:**
```
1. websocket.accept()
2. Validate JWT from ?token=<jwt> query param
   → on failure: send ErrorEvent{AUTH_REQUIRED}, close(4001)
3. Create asyncio.Queue(maxsize=256)
4. Subscribe Redis pubsub to "cai:market-feed:ltpc"
5. Launch 3 background tasks:
   a. _redis_listener — filter ticks by subscribed_keys, enqueue
   b. _sender         — drain queue, send JSON frames
   c. _heartbeat      — enqueue ping every 30s
6. Main loop: read client frames, handle sub/unsub/pong commands
7. On disconnect (any reason):
   → cancel tasks a,b,c
   → drain queue
   → market_feed.unsubscribe_many(all_subscribed_keys)
   → pubsub.unsubscribe + pubsub.aclose()
```

**Backpressure:** Queue maxsize=256. On full: drop oldest tick (same pattern as `WebSocketManager`).

**Input validation:**
- `instrument_keys`: non-empty list, max 100 per message
- Each key must match `^[A-Z_]+\|[A-Z0-9_|]+$`
- Invalid input: send `ErrorEvent{INVALID_KEY}`, **do not close connection**

### 5.3 Redis Channel

**Channel:** `cai:market-feed:ltpc`  
**Pattern:** Consistent with all existing `cai:*` channels in `RedisChannels`  
**Delivery:** At-most-once (fire-and-forget pub/sub — not persisted)  
**Format:** JSON string, fields: `type`, `instrument_key`, `ltp`, `cp`, `ts`

### 5.4 Horizontal Scaling Design

Currently single `uvicorn --workers 1`. Redis pub/sub is used regardless so the architecture is correct when Kubernetes/ECS scaling is introduced:

- **Constraint:** Upstox allows only 2 WS connections per user. Only ONE pod should connect to Upstox Market Data Feed V3.
- **Solution:** `MarketFeedService` runs in whichever pod starts first; publishes to Redis. All pods subscribe from Redis and fan out to their local WS clients.
- **Consistent with:** Existing trade suggestions pattern (ML worker → Redis → API process).

---

## 6. Frontend Implementation Details

### 6.1 `PriceFeed` External Store

**Location:** `frontend/src/lib/price-feed.ts`  
**Pattern:** Framework-agnostic external store compatible with React 18 `useSyncExternalStore`

**Key design decisions:**
- Prices stored in a plain `Map` — no React state — so updates never directly trigger renders
- Per-instrument listener subscriptions — a component re-renders only when ITS instrument changes
- RAF batching: multiple `update()` calls in one animation frame → one notify pass → one React render cycle
- `queueMicrotask` fallback for SSR/test environments (no `requestAnimationFrame`)
- `evict(key)` removes price and cleans up listeners when instrument fully unsubscribed

**Module-level singleton:** `export const priceFeed = new PriceFeed()` — one instance for the entire app lifetime.

### 6.2 `useLtp` Hook

**Location:** `frontend/src/hooks/useLtp.ts`

```typescript
export function useLtp(instrumentKey: string): PriceSnapshot | null {
  return useSyncExternalStore(
    (callback) => priceFeed.subscribe(instrumentKey, callback),
    ()         => priceFeed.getSnapshot(instrumentKey),
    ()         => priceFeed.getServerSnapshot(instrumentKey),  // always null (SSR safe)
  )
}
```

`useSyncExternalStore` guarantees:
- **Concurrent Mode safe** — no tearing between renders
- **Strict Mode safe** — subscribe/unsubscribe called twice in dev; no leaks
- **SSR safe** — server snapshot returns null, hydration matches

### 6.3 `WatchlistContext`

**Location:** `frontend/src/contexts/WatchlistContext.tsx`

**Responsibilities:**
1. Watchlist CRUD (React Query fetch + mutations — unchanged from old `useWatchlist`)
2. Market-feed WS connection (single instance, auth-gated)
3. Subscription ref-counting (see diagram above)
4. `priceFeed.update()` on every incoming `ltpc` message

**Subscription management:**
- `subscribeInstrument(key)` — increments ref count; sends `sub` frame if 0→1
- `unsubscribeInstrument(key)` — decrements ref count; sends `unsub` frame + evicts price if 1→0
- On WS reconnect: re-sends `sub` for all keys with count > 0
- On auth lost: closes WS with code 1000, clears connection state

**WS connection implementation:**
- Direct `new WebSocket(url)` (not `useWebSocket` hook) — provider needs full lifecycle control without the hook's stateful re-render model
- Exponential backoff with ±25% jitter, max 30s
- Token passed as `?token=<jwt>` query param (required by backend endpoint)
- `shouldConnectRef` prevents reconnect after intentional disconnect (logout/unmount)

**Auto-subscribe watchlist items:**
```typescript
useEffect(() => {
  watchlistItems.forEach((item) => subscribeInstrument(item.instrument_key))
  return () => {
    watchlistItems.forEach((item) => unsubscribeInstrument(item.instrument_key))
  }
}, [watchlistItems])
```

### 6.4 `useWatchlist` Hook (Rewritten)

**Location:** `frontend/src/hooks/useWatchlist.ts`  
**Before:** ~200 lines — full state management, polling timers, per-instance LTP cache  
**After:** 3 lines — thin context consumer returning `useWatchlistContext()`

Public API is identical — all existing call sites (`page.tsx`, `WatchlistCard`, `DetailPane`) work without modification.

### 6.5 `DetailPane` Migration

**Removed:**
- `buildTickStreamUrl()` function
- `getReconnectDelay()` function  
- `TICK_STREAM_INTERVAL_MS`, `TICK_STREAM_MAX_RECONNECTS`, `TICK_STREAM_BASE_DELAY_MS`, `TICK_STREAM_MAX_DELAY_MS` constants
- `TickStreamStatus` type
- `tickSocketRef` ref
- `tickStreamStatus` state
- `liveTick` state
- `restPrice` state
- REST LTP fetch `useEffect` (fired once on instrument open)
- Tick stream WebSocket `useEffect` (full connect/reconnect/message loop)
- Standalone `useWatchlist()` call (was instantiating a second hook instance)
- `WS_BASE_URL` import (no longer needed)
- `UpstoxTickStreamMessage` type import (no longer needed)

**Added:**
```typescript
// Live price from market feed (zero REST calls)
const priceSnapshot = useLtp(instrument.instrument_key)
const displayPrice  = priceSnapshot?.ltp ?? null

// Subscribe this instrument while DetailPane is mounted
useEffect(() => {
  subscribeInstrument(instrument.instrument_key)
  return () => unsubscribeInstrument(instrument.instrument_key)
}, [instrument.instrument_key, subscribeInstrument, unsubscribeInstrument])

// liveTick for chart candle merge (same shape as before)
const liveTick: UpstoxLtpTick | null = priceSnapshot?.ltp != null
  ? { instrument_key: instrument.instrument_key, last_price: priceSnapshot.ltp, server_timestamp: String(priceSnapshot.ts) }
  : null

// Connection status from shared feed (no per-component WS state)
const isLive       = isMarketFeedConnected && getISTMarketSession() === "open"
const isConnecting = !isMarketFeedConnected && getISTMarketSession() === "open"
```

### 6.6 `WatchlistCard` Migration

**Before:** Received `ltp: number | null` and `prevClose: number | null` as props sourced from `item.ltp` / `item.prevClose` on the old `WatchlistItemWithPrice` type.

**After:** Calls `useLtp(item.instrument_key)` internally. Props `ltp` and `prevClose` removed from interface. Each card independently subscribes to its instrument's price — re-renders only when that instrument's price changes.

---

## 7. Message Protocol Reference

### Client → Backend WS

```json
{ "type": "sub",   "instrument_keys": ["NSE_EQ|INE002A01018", "NSE_EQ|INE669E01016"] }
{ "type": "unsub", "instrument_keys": ["NSE_EQ|INE002A01018"] }
{ "type": "pong" }
```

**Validation:** `instrument_keys` max 100 per message; each key must match `^[A-Z_]+\|[A-Z0-9_|]+$`.

### Backend WS → Client

```json
{ "type": "ltpc",         "instrument_key": "NSE_EQ|INE002A01018", "ltp": 2851.50, "cp": 2840.00, "ts": 1704067200500 }
{ "type": "subscribed",   "instrument_keys": ["NSE_EQ|INE002A01018"], "ts": 1704067200500 }
{ "type": "unsubscribed", "instrument_keys": ["NSE_EQ|INE002A01018"], "ts": 1704067200500 }
{ "type": "ping",         "ts": 1704067200500 }
{ "type": "error",        "code": "INVALID_KEY",     "message": "Invalid instrument key format: ..." }
{ "type": "error",        "code": "AUTH_REQUIRED",   "message": "Missing token" }
{ "type": "error",        "code": "INVALID_MESSAGE", "message": "Message must be valid JSON" }
```

**Error behavior:** Validation errors send `ErrorEvent` and **keep the connection open**. Auth errors close with WebSocket code 4001.

### Redis Channel Payload

```json
{ "type": "ltpc", "instrument_key": "NSE_EQ|INE002A01018", "ltp": 2851.50, "cp": 2840.00, "ts": 1704067200500 }
```

Fields: `ltp` = last traded price; `cp` = previous session close (change % = `(ltp-cp)/cp*100`); `ts` = server timestamp ms since epoch.

---

## 8. Configuration

| Setting | Default | Range | Description |
|---|---|---|---|
| `MARKET_FEED_THROTTLE_MS` | `250` | 100–5000 | Per-instrument tick throttle (ms) before Redis publish |

Set in `.env`:
```bash
MARKET_FEED_THROTTLE_MS=250
```

---

## 9. Monitoring & Alerting

### Prometheus Metrics Added

| Metric | Type | Description |
|---|---|---|
| `market_feed_subscriptions_active` | Gauge | Currently subscribed instrument count |
| `market_feed_ticks_received_total` | Counter | All protobuf frames from Upstox |
| `market_feed_ticks_emitted_total` | Counter | Frames passing the throttle |
| `market_feed_ticks_discarded_total` | Counter | Frames dropped by throttle |
| `market_feed_redis_publish_errors_total` | Counter | Redis publish failures |
| `market_feed_upstox_reconnects_total` | Counter | Upstox WS reconnections |

### Recommended Alert Rules

```yaml
# Upstox feed reconnecting frequently
- alert: MarketFeedUpstoxReconnecting
  expr: rate(market_feed_upstox_reconnects_total[5m]) > 0.5
  severity: warning

# Redis publish failures
- alert: MarketFeedRedisPublishErrors
  expr: rate(market_feed_redis_publish_errors_total[1m]) > 0
  severity: warning

# Feed appears silent (subscriptions active but no ticks) — possible silent failure
- alert: MarketFeedSilent
  expr: rate(market_feed_ticks_emitted_total[5m]) == 0 and market_feed_subscriptions_active > 0
  severity: critical
```

### Health Indicators (Frontend)

`isMarketFeedConnected` is exposed from `WatchlistContext` and drives the connection status indicator on the hawk-eye-radar page:
- **Green** — connected, market open
- **Amber** — reconnecting / disconnected during market hours
- **Grey** — market closed or user not authenticated

---

## 10. Development / Mock Mode

When `UPSTOX_ACCESS_TOKEN` is not set in the environment (local development):

1. `MarketFeedService` skips the Upstox WS connection entirely
2. `subscribe_many(keys)` spawns a GBM asyncio task per key
3. GBM tasks publish synthetic ticks to Redis at 250ms intervals:
   - Starting price: ₹1,000
   - Volatility: 0.02% per tick (σ = 0.0002)
   - Mock `cp` = starting price
4. Frontend receives ticks through identical WS path — no code differences

**To verify mock mode is active:** Check server logs on startup:
```
INFO  MarketFeedService ready (throttle=250ms)
```
If the Upstox WS connection is attempted, you'll see:
```
INFO  MarketFeedService connected to WebSocket
```

---

## 11. Related Documents

- `MARKET_FEED_PLAN.md` (project root) — full implementation plan with dependency order
- `documentation/api/MARKET_FEED_WEBSOCKET_API.md` — WebSocket protocol quick reference
- `documentation/architecture/MARKET_FEED_ARCHITECTURE.md` — architecture diagrams and design decisions
- `documentation/testing/MARKET_FEED_TESTING_GUIDE.md` — testing and verification checklist
