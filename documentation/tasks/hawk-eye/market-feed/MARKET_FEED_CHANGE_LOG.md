# Market Feed Change Log

**Feature:** Unified Real-Time Market Feed  
**Date:** 2026-04-28  
**Author:** Claude (Sonnet 4.6)  

---

## Summary

Replaced the polling-based LTP REST architecture with a unified WebSocket-based real-time market data feed. Eliminated all REST LTP polling, the per-instrument tick stream, and the duplicate `useWatchlist` hook instance. Rate limit usage for LTP data reduced from thousands of requests per 30 minutes to zero.

---

## File-by-File Change Log

---

### CREATED: `backend/app/services/market_feed.py`

**New class: `MarketFeedService(BaseWebSocketService)`**

Replaces `TickStreamService`. Connects to Upstox Market Data Feed V3 via WebSocket and fans decoded price ticks out to all frontend clients via Redis pub/sub.

Key additions over old `TickStreamService`:
- `_ref_counts: dict[str, int]` — per-instrument subscriber ref counting
- `_ref_lock: asyncio.Lock` — guards all subscription mutations
- `_last_emitted: dict[str, float]` — per-instrument throttle timestamps
- `subscribe_many(keys)` / `unsubscribe_many(keys)` — dynamic subscription management with ref counting
- `_publish(key, ltp, cp, ts)` — fire-and-forget Redis publish via `asyncio.create_task`
- `_mock_producer(key)` — GBM synthetic tick generator for development (publishes to same Redis channel)
- `_start_mock_task(key)` / cancellation in `unsubscribe_many`
- `_ensure_connected()` — lazy WS connection start on first subscriber
- All 6 Prometheus metrics counters/gauges
- `start()` / `stop()` lifecycle methods matching existing service pattern
- Module-level `get_market_feed_service(request)` FastAPI dependency

**Protobuf helper: `_extract_ltpc(feed)`**

Replaces `_extract_ltp` and `_feed_to_dict` from old `tick_stream.py`. Returns `(ltp, cp)` tuple instead of full dict — only the fields needed for the ltpc feed.

---

### CREATED: `backend/app/schemas/market_feed.py`

All Pydantic v2 models for the market-feed WebSocket protocol.

**Client → Server:**
- `SubCommand` — `{type:"sub", instrument_keys:[...]}`
- `UnsubCommand` — `{type:"unsub", instrument_keys:[...]}`

**Server → Client:**
- `LtpcMessage` — `{type:"ltpc", instrument_key, ltp, cp, ts}`
- `SubscribedEvent` — `{type:"subscribed", instrument_keys, ts}`
- `UnsubscribedEvent` — `{type:"unsubscribed", instrument_keys, ts}`
- `HeartbeatEvent` — `{type:"ping", ts}`
- `ErrorEvent` — `{type:"error", code, message}`

**Shared validator:** `_validate_instrument_keys` — non-empty, ≤100 items, each matching `^[A-Z_]+\|[A-Z0-9_|]+$`

---

### MODIFIED: `backend/app/api/v1/upstox.py`

**Added imports:**
- `BackgroundTasks` from fastapi
- `JWTError, jwt` from jose
- `Redis` from redis.asyncio
- `RedisChannels` from app.core.redis
- `get_redis` from app.core.redis
- All `market_feed` schema types
- `MarketFeedService, get_market_feed_service` from app.services.market_feed

**Removed imports:**
- `TickStreamService, get_tick_service` from app.services.tick_stream
- `StreamStatusResponse` from app.schemas.upstox (schema still exists; endpoint removed)

**Removed endpoints:**
- `POST /stream/start` — replaced by WS `sub` command
- `POST /stream/stop` — replaced by WS disconnect
- `GET /stream/status` — replaced by Prometheus metrics
- `GET /ticks/ws` (WebSocket) — replaced by `/market-feed/ws`

**Removed functions:**
- `_run_live_stream()` — old tick stream bridge
- `_run_mock_stream()` — old GBM mock
- Constants: `_HEARTBEAT_INTERVAL_S`, `_MOCK_TICK_VOLATILITY`

**Added endpoint:**
- `GET /ltp` (REST) — single-instrument LTP with Redis cache (TTL from `CACHE_TTL_LIVE_QUOTE`)

**Added endpoint:**
- `GET /market-feed/ws` (WebSocket) — full implementation with:
  - `_verify_ws_token(token)` — JWT decode helper
  - `_redis_listener(pubsub, queue, subscribed_keys)` — Redis subscribe, filter, enqueue
  - `_sender(websocket, queue)` — drain queue, send JSON
  - `_heartbeat(queue)` — enqueue ping every 30s
  - `websocket_market_feed(...)` — main handler with auth, command loop, cleanup

**Removed `from __future__ import annotations`** — was not in original file; caused Pydantic forward-ref resolution issues with FastAPI.

---

### MODIFIED: `backend/app/core/redis.py`

**Added to `RedisChannels`:**
```python
MARKET_FEED_LTPC = "cai:market-feed:ltpc"
```

With full docstring explaining payload format and usage. Inserted before `CORRELATIONS_COMPLETED` to maintain logical grouping.

---

### MODIFIED: `backend/app/core/config.py`

**Added field in `Settings`:**
```python
MARKET_FEED_THROTTLE_MS: int = Field(
    default=250,
    ge=100,
    le=5000,
    description="Per-instrument tick throttle (ms) before publishing to Redis",
)
```

Placed in a new `# ── Market Feed ──` section between Redis and Rate Limiting.

---

### MODIFIED: `backend/app/main.py`

**Changed import:**
```python
# Before:
from app.services.tick_stream import TickStreamService

# After:
from app.services.market_feed import MarketFeedService
```

**Changed lifespan startup block:**
```python
# Before:
tick_service = TickStreamService(upstox_client=upstox_client)
app.state.tick_service = tick_service

# After:
from app.core.redis import get_redis
market_feed_service = MarketFeedService(
    upstox_client=upstox_client,
    redis=get_redis(),
    throttle_ms=settings.MARKET_FEED_THROTTLE_MS,
)
await market_feed_service.start()
app.state.market_feed_service = market_feed_service
```

**Changed lifespan shutdown block:**
```python
# Before:
await tick_service.disconnect()

# After:
await market_feed_service.stop()
```

---

### MODIFIED: `backend/app/services/base_websocket.py`

**Changed module docstring only** — removed stale reference to `TickStreamService` in the description. Logic unchanged.

---

### MODIFIED: `backend/tests/api/test_contracts.py`

**Replaced test `test_stream_status_returns_shape`** (tested a deleted endpoint `/stream/status`) with **`test_market_feed_ws_rejects_unauthenticated`** (verifies the new endpoint closes unauthenticated connections with code 4001).

---

### DELETED: `backend/app/services/tick_stream.py`

Full contents superseded by `MarketFeedService`:
- `_extract_ltp()` → replaced by `_extract_ltpc()` in `market_feed.py`
- `_feed_to_dict()` → not needed (ltpc mode only extracts ltp+cp)
- `TickStreamService` → replaced by `MarketFeedService`
- `get_tick_service()` → replaced by `get_market_feed_service()` in `market_feed.py`

---

### CREATED: `frontend/src/lib/price-feed.ts`

**New class: `PriceFeed`**

Framework-agnostic external store for live instrument prices. Module-level singleton `priceFeed` exported.

```typescript
class PriceFeed {
  update(instrumentKey, ltp, cp, ts)        // called by WatchlistProvider
  subscribe(instrumentKey, callback)         // useSyncExternalStore subscribe arg
  getSnapshot(instrumentKey)                 // useSyncExternalStore getSnapshot arg
  getServerSnapshot(instrumentKey)           // useSyncExternalStore serverSnapshot arg
  evict(instrumentKey)                       // cleanup on unsubscribe
  private scheduleFlush()                    // RAF batching
  private notifyAll()                        // notify all listeners
}
export type PriceSnapshot = { ltp, cp, ts }
export const priceFeed = new PriceFeed()
```

RAF batching: `flushScheduled` flag ensures at most one `requestAnimationFrame` per render cycle. Falls back to `queueMicrotask` in SSR/test environments.

---

### CREATED: `frontend/src/hooks/useLtp.ts`

**New hook: `useLtp(instrumentKey)`**

```typescript
export function useLtp(instrumentKey: string): PriceSnapshot | null {
  return useSyncExternalStore(
    (callback) => priceFeed.subscribe(instrumentKey, callback),
    ()         => priceFeed.getSnapshot(instrumentKey),
    ()         => priceFeed.getServerSnapshot(instrumentKey),
  )
}
```

Three-argument form of `useSyncExternalStore` — server snapshot argument makes it SSR-safe.

---

### CREATED: `frontend/src/contexts/WatchlistContext.tsx`

**New context: `WatchlistContext`**  
**New provider: `WatchlistProvider`**  
**New hook: `useWatchlistContext()`**

Exported `WatchlistContextValue` interface. Manages:
- React Query watchlist fetch + optimistic reorder mutation (moved from `useWatchlist`)
- add/remove/reorder mutations (moved from `useWatchlist`)
- Market-feed WebSocket connection (new)
- Subscription ref-counting (new)
- `priceFeed.update()` on every `ltpc` message (new)

WebSocket implementation: raw `new WebSocket(url)` with manual reconnect (not `useWebSocket` hook) because the provider needs full lifecycle control independent of React render cycles.

Auth: `?token=${encodeURIComponent(accessToken)}` appended to WS URL. Connection gated on `isAuthenticated && isAuthReady`. Reconnect uses exponential backoff (base 1s, max 30s, ±25% jitter).

---

### MODIFIED (REWRITTEN): `frontend/src/hooks/useWatchlist.ts`

**Before:** ~200 lines  
**After:** 15 lines

```typescript
import { useWatchlistContext } from '@/contexts/WatchlistContext'

export function useWatchlist() {
  return useWatchlistContext()
}

export type { WatchlistItem, WatchlistItemCreate } from '@/lib/api'
```

All LTP polling logic, `setInterval`, `ltpCacheRef`, `fetchLTP`, `fetchAllLTPs`, `WatchlistItemWithPrice` type, `LTPCache` interface deleted. Public API preserved — all call sites unchanged.

---

### MODIFIED: `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx`

**Imports removed:**
- `WS_BASE_URL` from `@/lib/api`
- `UpstoxTickStreamMessage` from `@/types/upstox`

**Imports added:**
- `useLtp` from `@/hooks/useLtp`

**Constants removed:**
- `TICK_STREAM_INTERVAL_MS`
- `TICK_STREAM_MAX_RECONNECTS`
- `TICK_STREAM_BASE_DELAY_MS`
- `TICK_STREAM_MAX_DELAY_MS`

**Type removed:**
- `TickStreamStatus`

**Functions removed:**
- `buildTickStreamUrl()`
- `getReconnectDelay()`

**State removed:**
- `tickStreamStatus: TickStreamStatus`
- `liveTick: UpstoxLtpTick | null`
- `restPrice: number | null`

**Refs removed:**
- `tickSocketRef: useRef<WebSocket | null>`

**Effects removed:**
- REST LTP fetch effect (fires once on instrument open, calls `upstoxAPI.getLtpQuote`)
- Tick stream WS effect (full connect/reconnect loop, ~70 lines)

**`useWatchlist()` call changed:**
Before: standalone instantiation (created a 2nd `useWatchlist` instance)
After: same call, but now returns from singleton context — no duplicate state

**Added:**
```typescript
const { subscribeInstrument, unsubscribeInstrument, isMarketFeedConnected } = useWatchlist()
const priceSnapshot = useLtp(instrument.instrument_key)
const displayPrice  = priceSnapshot?.ltp ?? null

useEffect(() => {
  subscribeInstrument(instrument.instrument_key)
  return () => unsubscribeInstrument(instrument.instrument_key)
}, [instrument.instrument_key, subscribeInstrument, unsubscribeInstrument])

const liveTick: UpstoxLtpTick | null = priceSnapshot?.ltp != null
  ? { instrument_key: instrument.instrument_key, last_price: priceSnapshot.ltp, server_timestamp: String(priceSnapshot.ts) }
  : null

const isLive       = isMarketFeedConnected && getISTMarketSession() === "open"
const isConnecting = !isMarketFeedConnected && getISTMarketSession() === "open"
```

---

### MODIFIED: `frontend/src/app/hawk-eye-radar/components/WatchlistCard.tsx`

**Imports added:**
- `useLtp` from `@/hooks/useLtp`

**Props removed from `WatchlistCardProps`:**
- `ltp: number | null`
- `prevClose: number | null`

**Props removed from destructuring:**
- `ltp`
- `prevClose`

**Added inside component:**
```typescript
const snapshot  = useLtp(item.instrument_key)
const ltp       = snapshot?.ltp ?? null
const prevClose = snapshot?.cp ?? null
```

All downstream logic using `ltp` and `prevClose` unchanged. Component now re-renders only when its specific instrument's price changes.

---

### MODIFIED: `frontend/src/app/hawk-eye-radar/page.tsx`

**Changed `WatchlistCard` usage:**
```tsx
// Before:
<WatchlistCard
  item={item}
  ltp={item.ltp}
  prevClose={item.prevClose}
  ...
/>

// After:
<WatchlistCard
  item={item}
  ...
/>
```

`item.ltp` and `item.prevClose` no longer exist on `WatchlistItem` (those were properties of the old `WatchlistItemWithPrice` type from the polling-era hook).

---

### MODIFIED: `frontend/src/app/providers.tsx`

**Added import:**
```typescript
import { WatchlistProvider } from '@/contexts/WatchlistContext'
```

**Added to provider tree:**
```tsx
<AuthProvider>
  <WatchlistProvider>          // ← added
    <ChartPreferencesProvider>
      ...
    </ChartPreferencesProvider>
  </WatchlistProvider>          // ← added
</AuthProvider>
```

`WatchlistProvider` placed inside `AuthProvider` because it reads `useAuth()` internally.

---

## TypeScript — Zero New Errors

Before this change, there were ~25 pre-existing TypeScript errors in the codebase (unrelated components: `CAIRealtimeDemo`, `EventsPanel`, `CandlestickChart`, `LoadingStateExample`, etc.).

All new code introduced **zero additional TypeScript errors**. Verified via `npx tsc --noEmit`.
