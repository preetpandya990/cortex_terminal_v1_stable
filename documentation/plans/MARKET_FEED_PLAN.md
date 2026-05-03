# Unified Real-Time Market Feed — Implementation Plan

## Problem Statement

The hawk-eye-radar page generates excessive Upstox API REST calls due to three compounding issues:

1. `useWatchlist` is instantiated independently in both `page.tsx` and `DetailPane.tsx` — two separate polling intervals with no shared cache
2. `getLtpQuote` is called once per watchlist item per poll (no batching)
3. Poll interval (3s) with cache TTL (5s) yields an effective fetch every 6s per item per instance

**Result:** With 4 watchlist items and DetailPane open, the 2,000 req/30min Upstox rate limit is already breached. At 10 items: 6,000 req/30min (3× over limit).

## Solution

Replace all REST LTP polling with a single persistent WebSocket connection to Upstox Market Data Feed V3 (`ltpc` mode), distributed via Redis pub/sub, consumed by a singleton React context. The per-instrument tick stream in `DetailPane` is consolidated into the same feed.

**Result:** LTP polling goes from N × 2 REST calls/poll → 0. One WS connection handles all instruments for all components.

---

## Architecture

```
Upstox Market Data Feed V3 WS (ltpc mode)
        ↓ protobuf binary, per-trade
MarketFeedService  ── throttle 250ms/instrument ──→  Redis "cai:market-feed:ltpc"
        (singleton, API process)                              ↓
                                               All API pods subscribe (future-proof)
                                                             ↓
                                        /api/v1/upstox/market-feed/ws  (new endpoint)
                                                             ↓ JSON ltpc ticks
                                              WatchlistProvider (singleton React context)
                                                             ↓
                                                  PriceFeed external store
                                                             ↓ useSyncExternalStore
                                        WatchlistCard · DetailPane · LivePriceBadge
```

### What Gets Eliminated

| Before | After |
|---|---|
| N REST `getLtpQuote` calls per poll (watchlist) | **0** |
| Duplicate `useWatchlist` instance in `DetailPane` | **Removed** |
| Per-instrument tick stream WS in `DetailPane` | **Removed** |
| `TickStreamService` + `/upstox/ticks/ws` endpoint | **Deleted** |

### Throttling Strategy — Option C (Defense in Depth)

- **Backend:** `MarketFeedService` throttles per-instrument at 250ms before publishing to Redis
- **Frontend:** `PriceFeed` store batches listener notifications via `requestAnimationFrame` (~60fps max flush rate)
- Net effect: at most 4 ticks/sec reach Redis per instrument; React renders at most at display refresh rate

### Horizontal Scaling Design

Currently single `uvicorn --workers 1`. Redis pub/sub is used regardless so the design is correct when Kubernetes/ECS horizontal scaling is introduced — only one pod holds the Upstox WS connection, all pods receive ticks via Redis. Consistent with the existing trade suggestions pattern.

---

## Message Contracts

### Redis channel payload (`cai:market-feed:ltpc`)
```json
{ "type": "ltpc", "instrument_key": "NSE_EQ|INE002A01018", "ltp": 2851.50, "cp": 2840.00, "ts": 1704067200500 }
```

### Frontend → Backend WS (client commands)
```json
{ "type": "sub",   "instrument_keys": ["NSE_EQ|INE002A01018", "NSE_EQ|INE669E01016"] }
{ "type": "unsub", "instrument_keys": ["NSE_EQ|INE002A01018"] }
```

### Backend → Frontend WS (server events)
```json
{ "type": "ltpc",         "instrument_key": "...", "ltp": 2851.50, "cp": 2840.00, "ts": 1704067200500 }
{ "type": "subscribed",   "instrument_keys": ["..."], "ts": 1704067200500 }
{ "type": "unsubscribed", "instrument_keys": ["..."], "ts": 1704067200500 }
{ "type": "ping",         "ts": 1704067200500 }
{ "type": "error",        "code": "INVALID_KEY" | "AUTH_REQUIRED", "message": "..." }
```

---

## Phase 0 — Contracts & Schemas (Foundation)

Define all Pydantic schemas and TypeScript types before writing any logic. All phases code against these contracts.

**Backend:** `backend/app/schemas/market_feed.py` *(new)*
- `LtpcMessage` — instrument_key, ltp, cp, ts
- `SubCommand`, `UnsubCommand` — instrument_keys list with validation
- `SubscribedEvent`, `UnsubscribedEvent`, `ErrorEvent` — server → client shapes
- Validators: instrument_key format `^[A-Z_]+\|[A-Z0-9_|]+$`, max 100 keys per message

**Frontend:** types added to `frontend/src/types/market-feed.ts` *(new)*
- `LtpcMessage`, `SubCommand`, `UnsubCommand`, `MarketFeedServerEvent`
- `PriceSnapshot = { ltp: number | null; cp: number | null; ts: number }`

---

## Phase 1 — Backend: `MarketFeedService`

**File:** `backend/app/services/market_feed.py` *(new — replaces `tick_stream.py`)*

Extends `BaseWebSocketService`. Single class, single responsibility: own the Upstox WS connection and publish decoded ticks to Redis.

### 1.1 — Subscription Registry (ref counting)

```python
_subscriptions: dict[str, int]  # instrument_key → active subscriber count
_ref_lock: asyncio.Lock          # guards all subscription mutations

async def subscribe(self, key: str) -> None:
    # increment ref; if 0→1, send binary sub frame to Upstox WS

async def unsubscribe(self, key: str) -> None:
    # decrement ref; if 1→0, send binary unsub frame to Upstox WS

async def subscribe_many(self, keys: list[str]) -> None:
    # batch — sends single sub frame for all newly activated keys

async def unsubscribe_many(self, keys: list[str]) -> None:
    # batch — sends single unsub frame for all fully deactivated keys
```

Upstox WS subscription frame (sent as binary):
```json
{ "guid": "<uuid4>", "method": "sub", "data": { "mode": "ltpc", "instrumentKeys": ["..."] } }
```

Unsubscribe frame:
```json
{ "guid": "<uuid4>", "method": "unsub", "data": { "instrumentKeys": ["..."] } }
```

### 1.2 — Backend Throttling (250ms per instrument)

```python
_last_emitted: dict[str, float]   # instrument_key → last emit epoch_ms
THROTTLE_MS: ClassVar[int] = 250  # configurable via settings.MARKET_FEED_THROTTLE_MS

def _should_emit(self, key: str, now_ms: float) -> bool:
    return now_ms - self._last_emitted.get(key, 0) >= self.THROTTLE_MS
```

On every decoded protobuf tick: check `_should_emit`. If false, discard (increment discard metric). If true, update `_last_emitted[key]` and proceed to publish.

### 1.3 — Redis Publish

```python
async def _publish_tick(self, key: str, ltp: float, cp: float, ts: int) -> None:
    payload = json.dumps({"type": "ltpc", "instrument_key": key, "ltp": ltp, "cp": cp, "ts": ts})
    asyncio.create_task(self._redis.publish(RedisChannels.MARKET_FEED_LTPC, payload))
    # fire-and-forget — never blocks tick processing loop
```

Redis publish errors are caught inside the task, logged, and counted in `market_feed_redis_publish_errors_total`.

### 1.4 — Protobuf Decoding

Reuses `_feed_to_dict` and `_extract_ltp` logic from `tick_stream.py` verbatim. Extracts from `ltpc` union field: `ltp`, `cp` (previous close). Timestamp sourced from `FeedResponse.currentTs`.

### 1.5 — Mock Mode (development)

If `settings.UPSTOX_ACCESS_TOKEN` is absent:
- Skip Upstox WS connection entirely
- On `subscribe_many(keys)`: spawn a GBM asyncio task per new key
- GBM task publishes synthetic `LtpcMessage` to the same Redis channel at 250ms intervals
- On `unsubscribe_many(keys)`: cancel GBM tasks for deactivated keys
- Frontend and WS endpoint are completely unaware — same code path

GBM parameters: drift=0, volatility=0.0002 per interval (matches existing `tick_stream.py` mock).

### 1.6 — Prometheus Metrics

```python
market_feed_subscriptions_active     # Gauge   — current subscribed instrument count
market_feed_ticks_received_total     # Counter — all ticks from Upstox WS
market_feed_ticks_emitted_total      # Counter — ticks that passed throttle
market_feed_ticks_discarded_total    # Counter — ticks dropped by throttle
market_feed_redis_publish_errors_total  # Counter
market_feed_upstox_reconnects_total  # Counter
```

### 1.7 — Graceful Shutdown

```python
async def stop(self) -> None:
    # 1. Cancel all GBM mock tasks
    # 2. Send unsub for all active subscriptions
    # 3. Close Upstox WS with code 1000
    # 4. Clear _subscriptions, _last_emitted
```

---

## Phase 2 — Backend: Redis Channel Registration

**File:** `backend/app/core/redis.py` *(modify)*

Add one constant to `RedisChannels`:
```python
MARKET_FEED_LTPC = "cai:market-feed:ltpc"
```

No other changes. All existing channels and pub/sub logic untouched.

---

## Phase 3 — Backend: WebSocket Endpoint

**File:** `backend/app/api/v1/upstox.py` *(modify)*

Add new endpoint. Remove old tick stream endpoint and all supporting functions.

### 3.1 — Endpoint: `GET /upstox/market-feed/ws`

```python
@ws_router.websocket("/market-feed/ws")
async def websocket_market_feed(
    websocket: WebSocket,
    market_feed: MarketFeedService = Depends(get_market_feed_service),
    redis: Redis = Depends(get_redis),
) -> None:
```

**Connection lifecycle:**

```
1. websocket.accept()
2. Await first message — must be {type: "auth", token: "..."}
   → validate JWT (same logic as trade_suggestions.py)
   → on failure: send {type: "error", code: "AUTH_REQUIRED"}, close 4001
3. Create conn_id = uuid4()
4. Create asyncio.Queue(maxsize=256) for this connection
5. Track subscribed_keys: set[str] = set()
6. Launch tasks:
   a. _redis_listener_task(conn_id, queue, subscribed_keys_ref, redis)
   b. _sender_task(conn_id, queue, websocket)
   c. _heartbeat_task(conn_id, queue)  — enqueues ping every 30s
7. Main loop: read client frames
   → {type: "sub",   instrument_keys: [...]} → validate → market_feed.subscribe_many() → subscribed_keys.update()
   → {type: "unsub", instrument_keys: [...]} → validate → market_feed.unsubscribe_many() → subscribed_keys.difference_update()
   → {type: "ping"}  → enqueue pong
   → unknown type    → send error, continue (do not close)
8. On disconnect (WebSocketDisconnect or any exception):
   → market_feed.unsubscribe_many(list(subscribed_keys))
   → cancel tasks a, b, c
   → drain queue
```

**`_redis_listener_task`:**
```python
async def _redis_listener_task(queue, subscribed_keys_ref, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.MARKET_FEED_LTPC)
    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        data = json.loads(message["data"])
        if data["instrument_key"] in subscribed_keys_ref:
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                queue.get_nowait()   # drop oldest
                queue.put_nowait(data)
```

**`_sender_task`:**
```python
async def _sender_task(queue, websocket):
    while True:
        message = await queue.get()
        await websocket.send_json(message)
```

**Backpressure:** Queue maxsize=256. On full: drop oldest message (same pattern as `WebSocketManager`). Log warning with `conn_id` and `instrument_key`.

### 3.2 — Input Validation

Per the schemas defined in Phase 0:
- `instrument_keys` must be non-empty, max 100 per message
- Each key must match `^[A-Z_]+\|[A-Z0-9_|]+$`
- Validation failure: send `{type: "error", code: "INVALID_KEY", message: "..."}` and **continue** (do not close connection)

### 3.3 — Remove Old Tick Stream

Delete from `backend/app/api/v1/upstox.py`:
- `async def websocket_ticks(...)` — the entire function
- `_mock_tick_loop(...)` helper
- All `TICK_STREAM_*` constants
- `TickStreamService` import and `get_tick_service` dependency usage

---

## Phase 4 — Backend: Lifespan Wiring & Cleanup

**File:** `backend/app/main.py` *(modify)*

In `lifespan()` startup block, replace tick service with market feed service:

```python
# Remove:
tick_service = TickStreamService(...)
app.state.tick_service = tick_service

# Add:
market_feed_service = MarketFeedService(
    redis=redis_client,
    throttle_ms=settings.MARKET_FEED_THROTTLE_MS,  # default 250
)
await market_feed_service.start()
app.state.market_feed_service = market_feed_service
```

In `lifespan()` shutdown block:
```python
await market_feed_service.stop()
```

Add to `backend/app/core/config.py`:
```python
MARKET_FEED_THROTTLE_MS: int = 250
```

Add dependency getter:
```python
def get_market_feed_service(request: Request) -> MarketFeedService:
    return request.app.state.market_feed_service
```

**Delete entirely:** `backend/app/services/tick_stream.py`

`backend/app/services/base_websocket.py` — **keep unchanged** (still used by `MarketFeedService`).

---

## Phase 5 — Frontend: `PriceFeed` External Store

**File:** `frontend/src/lib/price-feed.ts` *(new)*

Framework-agnostic class. Compatible with React `useSyncExternalStore`. No React imports.

```typescript
export type PriceSnapshot = {
  ltp: number | null
  cp:  number | null
  ts:  number
}

class PriceFeed {
  private prices    = new Map<string, PriceSnapshot>()
  private listeners = new Map<string, Set<() => void>>()
  private flushScheduled = false

  /** Called by WatchlistProvider on every WS tick. */
  update(instrumentKey: string, ltp: number, cp: number, ts: number): void {
    this.prices.set(instrumentKey, { ltp, cp, ts })
    this.scheduleFlush(instrumentKey)
  }

  /**
   * useSyncExternalStore subscribe — scoped to one instrument key.
   * Returns unsubscribe function.
   */
  subscribe(instrumentKey: string, callback: () => void): () => void {
    if (!this.listeners.has(instrumentKey)) this.listeners.set(instrumentKey, new Set())
    this.listeners.get(instrumentKey)!.add(callback)
    return () => this.listeners.get(instrumentKey)?.delete(callback)
  }

  /** useSyncExternalStore getSnapshot — returns null if no data yet. */
  getSnapshot(instrumentKey: string): PriceSnapshot | null {
    return this.prices.get(instrumentKey) ?? null
  }

  /** SSR-safe server snapshot — always null (prices are live/client-only). */
  getServerSnapshot(_instrumentKey: string): null {
    return null
  }

  private scheduleFlush(instrumentKey: string): void {
    // Coalesce multiple update() calls within one animation frame into a
    // single notify pass. Falls back to setTimeout(0) in non-browser envs.
    if (typeof requestAnimationFrame !== "undefined") {
      if (!this.flushScheduled) {
        this.flushScheduled = true
        requestAnimationFrame(() => {
          this.flushScheduled = false
          this.notifyAll()
        })
      }
    } else {
      // SSR / test environment — notify synchronously
      this.listeners.get(instrumentKey)?.forEach(cb => cb())
    }
  }

  private notifyAll(): void {
    this.listeners.forEach(callbacks => callbacks.forEach(cb => cb()))
  }
}

// Module-level singleton — one instance for the entire app lifetime.
export const priceFeed = new PriceFeed()
```

**Note:** `flushScheduled` batches ALL instrument updates within one frame into a single notify pass. Multiple instruments updating in the same tick batch cause exactly one React render cycle — not one per instrument.

---

## Phase 6 — Frontend: `WatchlistContext`

**File:** `frontend/src/contexts/WatchlistContext.tsx` *(new)*

### 6.1 — Context Interface

```typescript
interface WatchlistContextValue {
  // Items (prices sourced from PriceFeed, not REST)
  items:     WatchlistItem[]
  isLoading: boolean
  isError:   boolean
  error:     unknown

  // Mutations — identical public API to current useWatchlist
  addToWatchlist:    (item: WatchlistItemCreate) => Promise<void>
  removeFromWatchlist: (itemId: number) => Promise<void>
  reorderWatchlist:  (args: { itemId: number; newPosition: number }) => Promise<void>
  checkInWatchlist:  (instrumentKey: string) => Promise<{ in_watchlist: boolean; item_id: number | null }>

  // Mutation states
  isAdding:    boolean
  isRemoving:  boolean
  isReordering: boolean

  // Market feed subscription management (consumed by DetailPane)
  subscribeInstrument:   (instrumentKey: string) => void
  unsubscribeInstrument: (instrumentKey: string) => void

  // Feed health
  isMarketFeedConnected: boolean
}
```

### 6.2 — Subscription Ref Counting

```typescript
const subscriptionCounts = useRef(new Map<string, number>())

function subscribeInstrument(key: string): void {
  const count = (subscriptionCounts.current.get(key) ?? 0) + 1
  subscriptionCounts.current.set(key, count)
  if (count === 1 && wsConnected) {
    wsSend({ type: "sub", instrument_keys: [key] })
  }
}

function unsubscribeInstrument(key: string): void {
  const count = Math.max(0, (subscriptionCounts.current.get(key) ?? 1) - 1)
  subscriptionCounts.current.set(key, count)
  if (count === 0) {
    subscriptionCounts.current.delete(key)
    if (wsConnected) wsSend({ type: "unsub", instrument_keys: [key] })
  }
}
```

### 6.3 — WebSocket Connection

Uses the existing `useWebSocket` hook. Token passed in-band (consistent with endpoint auth).

```typescript
const { isConnected: isMarketFeedConnected, send: wsSend } = useWebSocket({
  url: `${WS_BASE_URL}/upstox/market-feed/ws`,
  token: accessToken ?? undefined,
  enabled: isAuthenticated && isAuthReady,
  onMessage: (data) => {
    if (data.type === "ltpc") {
      priceFeed.update(data.instrument_key, data.ltp, data.cp, data.ts)
    }
  },
  onConnect: () => {
    // Re-subscribe all instruments with ref count > 0 after reconnect
    const activeKeys = [...subscriptionCounts.current.keys()]
    if (activeKeys.length > 0) wsSend({ type: "sub", instrument_keys: activeKeys })
  },
  reconnect: true,
  reconnectAttempts: 10,
})
```

### 6.4 — Watchlist Fetch & Auto-Subscribe

```typescript
const { data: watchlistItems = [], isLoading, isError, error } = useQuery({
  queryKey: ["watchlist"],
  queryFn: watchlistAPI.getWatchlist,
  enabled: isAuthenticated && isAuthReady,
  staleTime: 30_000,
  refetchOnWindowFocus: true,
})

// Subscribe all watchlist items when data loads / changes
useEffect(() => {
  watchlistItems.forEach(item => subscribeInstrument(item.instrument_key))
  return () => {
    watchlistItems.forEach(item => unsubscribeInstrument(item.instrument_key))
  }
}, [watchlistItems])
```

Mutations (`addToWatchlist`, `removeFromWatchlist`, `reorderWatchlist`) unchanged from current implementation. On successful add: also call `subscribeInstrument`. On successful remove: also call `unsubscribeInstrument`.

### 6.5 — `WatchlistProvider` Component

Standard context provider. Guards WS connection behind `isAuthenticated && isAuthReady` — no connection opened during boot window or logged-out state.

---

## Phase 7 — Frontend: `useWatchlist` Hook Rewrite

**File:** `frontend/src/hooks/useWatchlist.ts` *(rewrite)*

```typescript
import { useContext } from "react"
import { WatchlistContext } from "@/contexts/WatchlistContext"

export function useWatchlist() {
  const ctx = useContext(WatchlistContext)
  if (!ctx) throw new Error("[useWatchlist] Must be used within WatchlistProvider")
  return ctx
}
```

**Deleted from this file:**
- `LTP_POLL_INTERVAL_MS`, `LTP_CACHE_TTL_MS`
- `LTPCache` interface
- `WatchlistItemWithPrice` (merged into `WatchlistItem` via PriceFeed)
- `ltpCacheRef`, `isMountedRef`
- `fetchLTP`, `fetchAllLTPs`
- All `setInterval` / polling logic
- All React Query state for items (moved to context)

Public API is preserved exactly — call sites in `page.tsx` and `WatchlistCard` require zero changes.

---

## Phase 8 — Frontend: `DetailPane` Migration

**File:** `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx` *(modify)*

### 8.1 — Remove

| What | Location |
|---|---|
| Standalone `useWatchlist()` call | line 102 |
| `restPrice` state + REST LTP `useEffect` | lines 131, 271–286 |
| Tick stream `useEffect` block | lines 479–552 |
| `tickSocketRef` ref | line 138 |
| `TickStreamStatus` type | line 61 |
| `buildTickStreamUrl` function | lines 65–73 |
| `getReconnectDelay` function | lines 75–78 |
| `TICK_STREAM_*` constants | lines 41–44 |
| `tickStreamStatus` state | line 128 |
| `liveTick` state (replaced by `useLtp`) | line 127 |
| `isLive`, `isConnecting` derivations (old) | lines 576–581 |
| `WS_BASE_URL` import (if only used by tick stream) | line 6 |

### 8.2 — Add

```typescript
import { useLtp } from "@/hooks/useLtp"

// Inside component:
const { subscribeInstrument, unsubscribeInstrument, isMarketFeedConnected } = useWatchlist()
// useWatchlist() now reads from context — no duplicate hook instance

const priceSnapshot = useLtp(instrument.instrument_key)
const displayPrice = priceSnapshot?.ltp ?? null

// Subscribe this instrument to market feed while DetailPane is mounted
useEffect(() => {
  subscribeInstrument(instrument.instrument_key)
  return () => unsubscribeInstrument(instrument.instrument_key)
}, [instrument.instrument_key])

// liveTick for chart candle merge — adapted from PriceFeed snapshot
const liveTick = priceSnapshot?.ltp != null
  ? { last_price: priceSnapshot.ltp, timestamp: priceSnapshot.ts }
  : null

// isLive / isConnecting derived from shared feed status
const isLive = isMarketFeedConnected && getISTMarketSession() === "open"
const isConnecting = !isMarketFeedConnected && getISTMarketSession() === "open"
```

**Watchlist state** (`inWatchlist`, `watchlistItemId`, `isAdding`, `isRemoving`, `handleToggleWatchlist`) — all still sourced from `useWatchlist()`, which now returns context values. No logic changes needed.

---

## Phase 9 — Frontend: Provider Tree

**File:** `frontend/src/app/providers.tsx` *(modify)*

```tsx
import { WatchlistProvider } from "@/contexts/WatchlistContext"

// Add WatchlistProvider immediately inside AuthProvider:
<AuthProvider>
  <WatchlistProvider>
    <ChartPreferencesProvider>
      {/* ... rest of provider tree unchanged ... */}
    </ChartPreferencesProvider>
  </WatchlistProvider>
</AuthProvider>
```

`WatchlistProvider` reads `useAuth()` internally — must be inside `AuthProvider`. No other provider ordering changes.

---

## Phase 10 — Frontend: `useLtp` Hook

**File:** `frontend/src/hooks/useLtp.ts` *(new)*

```typescript
import { useSyncExternalStore } from "react"
import { priceFeed, type PriceSnapshot } from "@/lib/price-feed"

export function useLtp(instrumentKey: string): PriceSnapshot | null {
  return useSyncExternalStore(
    (callback) => priceFeed.subscribe(instrumentKey, callback),
    ()         => priceFeed.getSnapshot(instrumentKey),
    ()         => priceFeed.getServerSnapshot(instrumentKey),
  )
}
```

`useSyncExternalStore` is the canonical React 18 primitive for external stores (used internally by Redux, Zustand, Jotai). It is Concurrent Mode safe, StrictMode safe, and SSR safe via the server snapshot argument.

Usage: any component that needs a live price imports `useLtp` directly — no prop drilling, no polling, no REST calls.

---

## Phase 11 — Cleanup & Observability

### Dead Code Removal

| File | Action |
|---|---|
| `backend/app/services/tick_stream.py` | **Delete** |
| `frontend/src/hooks/useWatchlist.ts` | Replaced (Phase 7) |
| `DetailPane.tsx` tick stream block | Removed (Phase 8) |

### Settings Addition

`backend/app/core/config.py`:
```python
MARKET_FEED_THROTTLE_MS: int = Field(default=250, ge=100, le=5000)
```

### Frontend Connection Status Indicator

Surface `isMarketFeedConnected` in the hawk-eye-radar page header as a small status dot:
- **Green** — connected, market open
- **Amber** — reconnecting
- **Grey** — market closed or not authenticated

Consistent with the existing WebSocket connection indicator used for trade suggestions.

### Monitoring & Alerting

Add to Prometheus alert rules:

```yaml
# Alert: market feed reconnecting frequently
- alert: MarketFeedUpstoxReconnecting
  expr: rate(market_feed_upstox_reconnects_total[5m]) > 0.5
  severity: warning

# Alert: Redis publish failures
- alert: MarketFeedRedisPublishErrors
  expr: rate(market_feed_redis_publish_errors_total[1m]) > 0
  severity: warning

# Alert: no ticks emitted (possible silent failure)
- alert: MarketFeedSilent
  expr: rate(market_feed_ticks_emitted_total[5m]) == 0 and market_feed_subscriptions_active > 0
  severity: critical
```

---

## Dependency & Execution Order

```
Phase 0  ── Contracts & Schemas
    ↓
    ├── Phase 1  MarketFeedService
    │       ↓
    │   Phase 2  Redis channel constant
    │       ↓
    │   Phase 3  WS endpoint  ← depends on Phase 1 + 2
    │       ↓
    │   Phase 4  Lifespan wiring + delete tick_stream.py
    │
    └── Phase 5  PriceFeed store
            ↓
        Phase 10  useLtp hook
            ↓
        Phase 6  WatchlistContext  ← depends on Phase 5 + 10
            ↓
        Phase 7  useWatchlist rewrite  ← depends on Phase 6
            ↓
        Phase 8  DetailPane migration  ← depends on Phase 7 + 10
            ↓
        Phase 9  providers.tsx  ← depends on Phase 6
            ↓
        Phase 11  Cleanup & Observability
```

Phases 1–4 (backend) can be built and tested in complete isolation before touching frontend. Frontend phases (5–10) can be developed and tested against mock mode (no Upstox token required).

---

## Files Changed Summary

| File | Action |
|---|---|
| `backend/app/services/market_feed.py` | **Create** |
| `backend/app/services/tick_stream.py` | **Delete** |
| `backend/app/core/redis.py` | Modify — add `MARKET_FEED_LTPC` channel |
| `backend/app/core/config.py` | Modify — add `MARKET_FEED_THROTTLE_MS` setting |
| `backend/app/api/v1/upstox.py` | Modify — add endpoint, remove tick stream |
| `backend/app/main.py` | Modify — swap service registration |
| `backend/app/schemas/market_feed.py` | **Create** |
| `frontend/src/types/market-feed.ts` | **Create** |
| `frontend/src/lib/price-feed.ts` | **Create** |
| `frontend/src/hooks/useLtp.ts` | **Create** |
| `frontend/src/contexts/WatchlistContext.tsx` | **Create** |
| `frontend/src/hooks/useWatchlist.ts` | Rewrite (thin context consumer) |
| `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx` | Modify — remove 3 systems |
| `frontend/src/app/providers.tsx` | Modify — add WatchlistProvider |

**No changes to:** `CandlestickChart`, `WatchlistCard`, `page.tsx`, `TradeSuggestionCard`,
`useWebSocket`, `AuthContext`, `api.ts`, `base_websocket.py`, or any ML/ingestion backend code.

---

## Rate Limit Impact

| Scenario | Before | After |
|---|---|---|
| 10 watchlist items, page only | 100 req/min | **0** |
| 10 watchlist items + DetailPane open | 200 req/min | **0** |
| 10 items, 30-min window | 6,000 req | **0** |
| Budget remaining (2,000/30min) | ~0 for LTP | **2,000 available** for candles, scan, etc. |
