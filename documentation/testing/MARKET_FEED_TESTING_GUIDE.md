# Market Feed Testing Guide

**Feature:** Unified Real-Time Market Feed  
**Date:** 2026-04-28  

---

## 1. Prerequisites

### Without Upstox Token (Mock Mode — Recommended for Local Dev)

No special setup needed. `MarketFeedService` detects the absence of `UPSTOX_ACCESS_TOKEN` and spawns GBM synthetic tick generators automatically. The entire WS path is exercised.

Verify mock mode is active in server logs:
```
INFO  MarketFeedService ready (throttle=250ms)
```
No Upstox WS connection log should appear.

### With Real Upstox Token

Set in `.env`:
```bash
UPSTOX_ACCESS_TOKEN=<your_token>
```

Verify live mode in server logs:
```
INFO  MarketFeedService connected to WebSocket
INFO  Subscribed N instruments on Upstox market feed
```

---

## 2. Backend Verification

### 2.1 Schema Validation

```bash
cd backend
.venv/bin/python -c "
from app.schemas.market_feed import SubCommand, UnsubCommand, LtpcMessage, ErrorEvent
from app.core.redis import RedisChannels
from app.core.config import get_settings

s = get_settings()
print('Throttle setting:', s.MARKET_FEED_THROTTLE_MS)
print('Redis channel:', RedisChannels.MARKET_FEED_LTPC)
print('All schemas OK')
"
```

Expected output:
```
Throttle setting: 250
Redis channel: cai:market-feed:ltpc
All schemas OK
```

### 2.2 App Import Integrity

```bash
cd backend
.venv/bin/python -c "
from app.services.market_feed import MarketFeedService
from app.api.v1.upstox import router, ws_router
from app.main import create_app
print('WS routes:', [r.path for r in ws_router.routes])
"
```

Expected: `WS routes: ['/market-feed/ws']`

### 2.3 Verify Old Tick Stream Is Gone

```bash
grep -r "tick_stream\|ticks/ws\|TickStreamService" backend/app/ 2>/dev/null | grep -v ".pyc"
```

Expected: No output (only docstring/comment references in `base_websocket.py` are acceptable).

### 2.4 WebSocket Connection Test (wscat)

Install wscat if needed: `npm install -g wscat`

**Without token (should reject):**
```bash
wscat -c "ws://localhost:8000/api/v1/upstox/market-feed/ws"
```
Expected: Connection closed with code 4001 after `{"type":"error","code":"AUTH_REQUIRED",...}`

**With valid token:**
```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"...","password":"..."}' | jq -r '.access_token')

wscat -c "ws://localhost:8000/api/v1/upstox/market-feed/ws?token=$TOKEN"
```

Then type:
```json
{"type":"sub","instrument_keys":["NSE_EQ|INE002A01018"]}
```

Expected responses:
```json
{"type":"subscribed","instrument_keys":["NSE_EQ|INE002A01018"],"ts":...}
{"type":"ltpc","instrument_key":"NSE_EQ|INE002A01018","ltp":1000.xx,"cp":1000.0,"ts":...}
{"type":"ltpc",...}
...
```

Heartbeat (after 30s):
```json
{"type":"ping","ts":...}
```

### 2.5 Subscription Validation Test

```bash
# Should reject — invalid key format
wscat -c "ws://localhost:8000/api/v1/upstox/market-feed/ws?token=$TOKEN"
> {"type":"sub","instrument_keys":["invalid key"]}
```

Expected: `{"type":"error","code":"INVALID_KEY",...}` — connection stays open.

### 2.6 Prometheus Metrics

After at least one subscription:
```bash
curl -s http://localhost:8000/metrics | grep market_feed
```

Expected output (values will vary):
```
market_feed_subscriptions_active 1.0
market_feed_ticks_received_total 42.0
market_feed_ticks_emitted_total 8.0
market_feed_ticks_discarded_total 34.0
market_feed_redis_publish_errors_total 0.0
market_feed_upstox_reconnects_total 0.0
```

**Note:** In mock mode, `ticks_received` stays 0 (no protobuf frames); `ticks_emitted` increments as GBM tasks publish directly to Redis.

---

## 3. Frontend Verification

### 3.1 PriceFeed Store Unit Behaviour

Open browser console on any page with the provider active:

```javascript
// Import the singleton
import('/price-feed').then(m => {
  const pf = m.priceFeed
  
  // Simulate an update
  pf.update("NSE_EQ|TEST", 2851.50, 2840.00, Date.now())
  
  // Check snapshot
  console.log(pf.getSnapshot("NSE_EQ|TEST"))
  // Expected: { ltp: 2851.50, cp: 2840.00, ts: <timestamp> }
})
```

### 3.2 Hawk-Eye Radar Page Checks

1. **Load the page** — navigate to `/hawk-eye-radar`

2. **Check WS connection established:**
   - Open browser DevTools → Network → WS filter
   - Should see one connection to `/api/v1/upstox/market-feed/ws`
   - Should NOT see any connection to `/api/v1/upstox/ticks/ws` (old endpoint is gone)
   - Should NOT see repeated REST calls to `/upstox/ltp` every few seconds

3. **Verify watchlist prices update in real-time:**
   - Add 1–3 instruments to watchlist
   - Prices should update automatically without page refresh
   - In mock mode: prices will drift over time (GBM random walk)

4. **Open DetailPane:**
   - Click any suggestion or watchlist item
   - Verify: no new `/ticks/ws` WS connection opened
   - Verify: the chart's live candle updates (price badge shows live tick marker)
   - Verify: only ONE WS connection in Network tab (not two)

5. **Close DetailPane:**
   - Verify: WS connection stays open (watchlist still subscribed)
   - Verify: no `unsub` frame sent for instruments still in watchlist (ref count stays > 0)

6. **Remove all watchlist items:**
   - Verify: `unsub` frames sent for each removed instrument
   - Verify: WS connection stays open (waiting for future subscriptions)

### 3.3 Connection Status Indicator

On the hawk-eye-radar page:
- **Green dot** — feed connected, market session open
- **Amber dot** — feed reconnecting (test by restarting backend)
- **Grey dot** — market closed or user not authenticated

### 3.4 Network Tab — What Should NOT Appear

| Request | Before | After |
|---|---|---|
| `GET /upstox/ltp?instrument_key=...` (repeated polling) | Appeared every 3–6s per instrument | **Must not appear** (except one-shot candle page loads) |
| `GET /upstox/ticks/ws` WS connection | One per DetailPane open | **Must not appear** |
| Multiple WS connections to `/market-feed/ws` | N/A | **Must be exactly one** |

---

## 4. Regression Checks

### 4.1 Candle Chart Still Works

- Open DetailPane on any instrument
- Switch timeframes — chart should reload candles
- Scroll left — load-more history should trigger
- Verify live candle merges when `ltp` tick arrives

### 4.2 Watchlist CRUD

- Add instrument → appears in watchlist with live price
- Remove instrument → disappears from watchlist
- Drag reorder → order persists after page refresh

### 4.3 Trade Suggestions WebSocket (Separate, Unaffected)

- Connect to `/api/v1/trade-suggestions/ws` (used by `useWebSocket` in `page.tsx`)
- This is an entirely separate WS endpoint — should still function normally
- New suggestions should appear without page refresh

### 4.4 Mock Mode End-to-End

When running without Upstox token:
1. Backend starts without error
2. Navigate to hawk-eye-radar
3. Add instruments to watchlist
4. Prices show and drift over time (GBM)
5. DetailPane opens and shows moving price badge
6. Chart candle live merge works

---

## 5. Performance Validation

### Before vs After (DevTools → Network → XHR)

Idle on hawk-eye-radar page, 10 watchlist items:

| Metric | Before | After (target) |
|---|---|---|
| REST calls/min | ~200 | **0** |
| WS connections | 0–N (per instrument) | **1** |
| REST calls visible in 30min | ~6,000 | **0** |

### Memory

Check for leaks after extended use:
- Chrome DevTools → Memory → Heap snapshot
- `PriceFeed` listeners map should not grow unboundedly
- Each instrument should have at most a few listeners (one per `useLtp` call site that's mounted)

---

## 6. Rollback Procedure

If issues are found post-deployment:

1. **Backend rollback:** The old `tick_stream.py` and old `upstox.py` are in git history. The key commit is the one adding `market_feed.py` and modifying `upstox.py`.

2. **Frontend rollback:** The old `useWatchlist.ts` (with polling) is in git history. Revert `WatchlistContext.tsx`, `useWatchlist.ts`, `DetailPane.tsx`, `WatchlistCard.tsx`, `providers.tsx`.

3. **No database migrations involved** — this is a pure application-layer change.

---

## 7. Known Limitations

- **After-hours prices:** `ltpc` mode only sends ticks on actual trades. After market close (15:30 IST), no new ticks arrive. The last price before close remains displayed — this is correct behaviour (not a bug).

- **First load price:** On page load, `useLtp` returns `null` until the first `ltpc` tick arrives for that instrument. Components should handle `null` gracefully (show `—` placeholder) — this is already implemented in `WatchlistCard` and `DetailPane`.

- **Mock mode cp field:** In mock mode, `cp` is set to the GBM starting price (₹1,000) — not a real previous close. Change % in mock mode will always start near 0% and drift.
