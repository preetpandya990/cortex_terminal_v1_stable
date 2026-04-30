# Market Feed Architecture

**Feature:** Unified Real-Time Market Feed  
**Date:** 2026-04-28  

---

## System Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  UPSTOX MARKET DATA FEED V3                                                 │
│  wss://api.upstox.com/...  (ltpc mode, protobuf binary)                     │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ binary protobuf frames
                                 │ per-trade updates
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  MarketFeedService  (backend/app/services/market_feed.py)                   │
│                                                                             │
│  ┌─────────────────────────┐   ┌──────────────────────────────────────┐    │
│  │  BaseWebSocketService   │   │  Subscription Registry               │    │
│  │  - connect/reconnect    │   │  _ref_counts: dict[str, int]         │    │
│  │  - exponential backoff  │   │  subscribe_many / unsubscribe_many   │    │
│  │  - SSL cert verify ON   │   └──────────────────────────────────────┘    │
│  └─────────────────────────┘                                               │
│                                                                             │
│  on_message(protobuf)                                                       │
│    → FeedResponse.ParseFromString()                                         │
│    → _extract_ltpc(feed) → (ltp, cp)                                        │
│    → throttle: 250ms per instrument         ← MARKET_FEED_THROTTLE_MS       │
│    → asyncio.create_task(_publish(...))     ← fire-and-forget               │
│                                                                             │
│  Prometheus: received / emitted / discarded / redis_errors / reconnects     │
└────────────────────────────────┬────────────────────────────────────────────┘
                                 │ redis.publish("cai:market-feed:ltpc", json)
                                 │ { type, instrument_key, ltp, cp, ts }
                                 ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Redis Pub/Sub  — channel: cai:market-feed:ltpc                             │
│  At-most-once delivery · fire-and-forget · horizontally scalable            │
└──────────────┬──────────────────────────────────────────────────────────────┘
               │
   ┌───────────┼──────────── (future: N API pods) ────────────────┐
   │           │                                                   │
   ▼           ▼                                                   ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  /api/v1/upstox/market-feed/ws  (backend/app/api/v1/upstox.py)          │
│                                                                          │
│  Per-connection:                                                          │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │  asyncio.Queue(maxsize=256)  — outbound message buffer          │    │
│  │  _redis_listener  — subscribe Redis, filter by subscribed_keys  │    │
│  │  _sender          — drain queue, send JSON to WebSocket client  │    │
│  │  _heartbeat       — enqueue ping every 30s                      │    │
│  └─────────────────────────────────────────────────────────────────┘    │
│                                                                          │
│  Auth: JWT via ?token=<jwt> query param → reject(4001) if invalid       │
│  Cleanup: unsubscribe_many(all_keys) on every disconnect                 │
└───────────────────────────┬──────────────────────────────────────────────┘
                            │ JSON ltpc ticks over WebSocket
                            │ { type:"ltpc", instrument_key, ltp, cp, ts }
                            ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  WatchlistProvider  (frontend/src/contexts/WatchlistContext.tsx)             │
│                                                                             │
│  Single WebSocket connection (auth-gated)                                   │
│  Subscription ref-counts: instrument_key → count                            │
│                                                                             │
│  subscribeInstrument(key)   count 0→1: send {type:"sub",...}               │
│  unsubscribeInstrument(key) count 1→0: send {type:"unsub",...}             │
│                                        evict from PriceFeed                 │
│                                                                             │
│  onmessage: priceFeed.update(key, ltp, cp, ts)                             │
│                                                                             │
│  Also manages: watchlist CRUD (React Query mutations)                       │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ priceFeed.update(key, ltp, cp, ts)
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  PriceFeed  (frontend/src/lib/price-feed.ts)                                │
│                                                                             │
│  prices:    Map<instrumentKey, PriceSnapshot>                               │
│  listeners: Map<instrumentKey, Set<() => void>>                             │
│                                                                             │
│  update()  → stores snapshot → scheduleFlush()                              │
│  scheduleFlush()  → requestAnimationFrame → notifyAll()  ← BATCH            │
│  subscribe(key, cb) / unsubscribe / getSnapshot / getServerSnapshot         │
└──────────────────────────────┬──────────────────────────────────────────────┘
                               │ useSyncExternalStore
                               ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  useLtp(instrumentKey)  (frontend/src/hooks/useLtp.ts)                      │
│  Returns PriceSnapshot | null                                               │
│  Concurrent Mode safe · Strict Mode safe · SSR safe                        │
└─────────────┬───────────────────────────────────────────────────────────────┘
              │
   ┌──────────┼────────────────┬──────────────────────────────┐
   ▼          ▼                ▼                              ▼
WatchlistCard  DetailPane  LivePriceBadge  (any future consumer)
```

---

## Component Ownership

| Component | Owns | Consumes |
|---|---|---|
| `MarketFeedService` | Upstox WS connection, throttle, Redis publish | `BaseWebSocketService`, `UpstoxClient`, `Redis` |
| `/market-feed/ws` endpoint | Per-connection queues and tasks | `MarketFeedService`, `Redis pubsub` |
| `WatchlistProvider` | WS connection to backend, subscription ref counts | `useAuth`, `watchlistAPI`, `priceFeed` |
| `PriceFeed` | Price snapshots, per-instrument listener sets | Nothing (plain class) |
| `useLtp` | Nothing | `priceFeed` (module singleton) |
| `WatchlistCard` | Nothing | `useLtp`, `WatchlistItem` |
| `DetailPane` | Nothing (price/ticks) | `useLtp`, `useWatchlist` (context), `upstoxAPI` (candles only) |

---

## Design Decisions

### Why Redis pub/sub instead of in-process fan-out?

**Current state:** Single `uvicorn --workers 1` process. In-process fan-out would work today.

**Chosen:** Redis pub/sub for two reasons:
1. `MARKET_FEED_PLAN.md` notes a docker-compose comment: *"Production: use Kubernetes or ECS"* — horizontal scaling is planned
2. Only ONE process should hold the Upstox WS connection (2-connection limit per user). With multiple pods, Redis is the correct distribution mechanism.
3. Consistent with the existing trade suggestions pattern (worker.py → Redis → API process)

**Cost:** ~0.5–1ms additional latency per tick for the Redis hop. Acceptable for a 250ms throttle window.

### Why `useSyncExternalStore` instead of `useState`?

`useState` for prices would trigger a React render on every `priceFeed.update()` call. With 20 instruments at 4 ticks/sec each, that's 80 re-renders/sec app-wide — unacceptable.

`useSyncExternalStore`:
- Components subscribe individually (only re-renders when their specific instrument changes)
- React 18 Concurrent Mode safe (no tearing)
- Strict Mode safe (subscribe/unsubscribe called twice in dev — no leaks because unsubscribe returns a cleanup function)

### Why not reuse `useWebSocket` hook in `WatchlistProvider`?

`useWebSocket` is designed as a component-lifecycle hook. `WatchlistProvider` needs:
1. Full control over when to reconnect (auth state changes, token rotation)
2. No `forceUpdate` pattern (providers shouldn't trigger renders on connection state changes inside their internal WS logic)
3. Direct access to send/close without the hook's abstraction layer

A raw `WebSocket` with refs is the correct pattern for a provider that manages its own WS lifecycle outside of React's render cycle.

### Why ref-counting for subscriptions?

Two consumers may need prices for the same instrument simultaneously:
- `WatchlistProvider` (because the item is in the user's watchlist)
- `DetailPane` (because the user opened it from a chart)

Without ref-counting: a second subscribe would send a duplicate Upstox sub frame; an unsubscribe from one consumer would cancel the other's prices.

With ref-counting: the Upstox subscription stays active as long as any consumer needs it.

### Why throttle at 250ms on the backend?

Liquid NSE stocks (RELIANCE, TCS, NIFTY components) can produce 100–500 trades/second during peak market hours. Without throttling:
- Redis would receive 500 messages/second per instrument
- Each API pod's Redis listener would process 500 messages/second
- The frontend's WS handler would receive 500 JSON frames/second

250ms throttle → max 4 messages/second per instrument → manageable at any scale.

---

## Previous Architecture (Removed)

```
WatchlistCard ← item.ltp ← useWatchlist (page.tsx instance)
                              ↑
                         setInterval(3s)
                         fetchAllLTPs()
                           getLtpQuote(key)  × N   ← N REST calls per 6s
                           getLtpQuote(key)  × N   ← (per item, no batching)

DetailPane    ← liveTick ← TickStreamService ← /upstox/ticks/ws  (per-instrument WS)
              ← restPrice ← getLtpQuote()  ← one-shot REST on open
              ← useWatchlist (DetailPane instance) ← setInterval(3s) × N REST calls
```

**Problems:**
- `useWatchlist` ran in both `page.tsx` and `DetailPane` → 2× all LTP requests
- `getLtpQuote` was per-instrument → N REST calls per poll
- `DetailPane` had a separate per-instrument WebSocket → additional connection + LTP duplication
- Polling latency: 3–6s stale price displayed to user

---

## Future Considerations

1. **`full` mode upgrade**: Currently using `ltpc` (LTP + close only). Upgrading to `full` mode would provide bid/ask, volume, OI — enabling richer order book display without additional REST calls.

2. **DetailPane candle merge**: The `liveTick` now comes from the market feed at 250ms. If the chart needs finer granularity (< 250ms), `MARKET_FEED_THROTTLE_MS` can be lowered, or a separate intraday tick stream can be implemented on-demand for open charts.

3. **Multi-user isolation**: Currently all subscribers share one Upstox WS connection regardless of user. In a multi-user environment with different watchlists, the ref-counting correctly aggregates all users' instrument needs into one connection.
