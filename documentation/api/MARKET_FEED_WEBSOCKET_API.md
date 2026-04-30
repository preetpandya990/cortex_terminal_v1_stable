# Market Feed WebSocket API Reference

**Endpoint:** `GET /api/v1/upstox/market-feed/ws`  
**Protocol:** WebSocket (ws:// development · wss:// production)  
**Date:** 2026-04-28  

---

## Connection

```
ws://localhost:8000/api/v1/upstox/market-feed/ws?token=<jwt>
```

The JWT access token is required as a query parameter. Connections without a valid token are rejected immediately with close code 4001.

**Example (frontend):**
```typescript
const url = `${WS_BASE_URL}/upstox/market-feed/ws?token=${encodeURIComponent(accessToken)}`
const ws = new WebSocket(url)
```

---

## Authentication

| Method | Value |
|---|---|
| Transport | Query parameter: `?token=<jwt>` |
| Token type | Short-lived JWT access token (same as REST API bearer token) |
| On missing token | `{type:"error", code:"AUTH_REQUIRED"}` then close(4001) |
| On invalid/expired token | `{type:"error", code:"AUTH_REQUIRED"}` then close(4001) |

---

## Client → Server Messages

All messages must be valid JSON text frames.

### Subscribe

Start receiving `ltpc` ticks for one or more instruments.

```json
{
  "type": "sub",
  "instrument_keys": ["NSE_EQ|INE002A01018", "NSE_EQ|INE669E01016"]
}
```

| Field | Type | Constraints |
|---|---|---|
| `type` | string | Must be `"sub"` |
| `instrument_keys` | string[] | 1–100 items; each must match `^[A-Z_]+\|[A-Z0-9_|]+$` |

**Response:** `{type:"subscribed", instrument_keys:[...], ts:<ms>}`

### Unsubscribe

Stop receiving ticks for one or more instruments.

```json
{
  "type": "unsub",
  "instrument_keys": ["NSE_EQ|INE002A01018"]
}
```

**Response:** `{type:"unsubscribed", instrument_keys:[...], ts:<ms>}`

### Pong (optional)

Heartbeat reply. The server sends `ping` every 30s; clients may reply with `pong` but it is not required.

```json
{ "type": "pong" }
```

---

## Server → Client Messages

### `ltpc` — Live Price Update

Sent for each subscribed instrument whenever a new trade occurs (after server-side 250ms throttle).

```json
{
  "type": "ltpc",
  "instrument_key": "NSE_EQ|INE002A01018",
  "ltp": 2851.50,
  "cp": 2840.00,
  "ts": 1704067200500
}
```

| Field | Type | Description |
|---|---|---|
| `instrument_key` | string | Upstox instrument key |
| `ltp` | number | Last traded price |
| `cp` | number | Previous session close price |
| `ts` | integer | Server timestamp — milliseconds since epoch |

**Derived change %:** `((ltp - cp) / cp) * 100`

### `subscribed` — Subscription Acknowledgement

```json
{
  "type": "subscribed",
  "instrument_keys": ["NSE_EQ|INE002A01018"],
  "ts": 1704067200500
}
```

### `unsubscribed` — Unsubscription Acknowledgement

```json
{
  "type": "unsubscribed",
  "instrument_keys": ["NSE_EQ|INE002A01018"],
  "ts": 1704067200500
}
```

### `ping` — Server Heartbeat

Sent every 30 seconds. Clients should treat absence of ping for >60s as a stale connection.

```json
{ "type": "ping", "ts": 1704067200500 }
```

### `error` — Validation / Auth Error

```json
{
  "type": "error",
  "code": "INVALID_KEY",
  "message": "Invalid instrument key format: ['NSE_EQ|invalid key']"
}
```

| Code | Meaning | Connection |
|---|---|---|
| `AUTH_REQUIRED` | Missing or invalid JWT | Closed (4001) |
| `INVALID_KEY` | Instrument key fails format validation | Stays open |
| `INVALID_MESSAGE` | Message is not valid JSON or unknown type | Stays open |
| `INTERNAL` | Unexpected server error | Stays open |

---

## Instrument Key Format

Upstox instrument keys follow the pattern: `EXCHANGE_TYPE|SYMBOL_CODE`

**Examples:**
```
NSE_EQ|INE002A01018    Reliance Industries (equity)
NSE_EQ|INE009A01021    Infosys (equity)
NSE_INDEX|Nifty 50     NIFTY50 index
NSE_FO|45450           Futures/Options (F&O)
BSE_EQ|500325          BSE equity
```

**Regex:** `^[A-Z_]+\|[A-Z0-9_|]+$`

---

## Rate Limits

No per-connection rate limiting on the WebSocket endpoint itself. The underlying Upstox Market Data Feed V3 limits are:

| Mode | Individual limit | Combined limit |
|---|---|---|
| `ltpc` | 5,000 instruments | 2,000 instruments |

The server enforces this via `MarketFeedService` subscription management. The per-message limit of 100 `instrument_keys` is a server-side validation constraint.

---

## Lifecycle Example

```
Client                          Server
  |                               |
  |--- WS connect ?token=... ---->|
  |                               |  ← JWT validated
  |<-- {type:"subscribed"...} ----|  (auto-subscribe currently subscribed keys on reconnect)
  |                               |
  |--- {type:"sub", keys:[...]} ->|
  |<-- {type:"subscribed",...} ---|
  |                               |
  |<-- {type:"ltpc",...} ---------|  (price updates at ≤4/sec per instrument)
  |<-- {type:"ltpc",...} ---------|
  |<-- {type:"ltpc",...} ---------|
  |                               |
  |<-- {type:"ping", ts:...} -----|  (every 30s)
  |                               |
  |--- {type:"unsub", keys:[.]} ->|
  |<-- {type:"unsubscribed",...} -|
  |                               |
  |--- WS close(1000) ----------->|
  |                               |  ← server calls unsubscribe_many() cleanup
```

---

## Connection Limits

Upstox Market Data Feed V3: **2 WebSocket connections per user** (standard plan).

The backend `MarketFeedService` maintains exactly **1 connection** to Upstox shared across all API clients. Frontend connects to the backend endpoint — not to Upstox directly.

---

## Comparing with the Old Tick Stream Endpoint

| | Old `/upstox/ticks/ws` | New `/upstox/market-feed/ws` |
|---|---|---|
| Per-instrument | Yes (one WS per open DetailPane) | No (one WS per user session) |
| Auth | None (unauthenticated) | Required JWT query param |
| Multiple instruments | No | Yes (up to 100 per sub message) |
| Upstox connections | 1 per open DetailPane | 1 shared (backend singleton) |
| REST LTP polling | Separate polling in useWatchlist | Eliminated |
| Mock mode | GBM synthetic ticks | GBM via same Redis channel |
| Throttle | `interval_ms` param (100–5000ms) | Fixed 250ms server-side (configurable) |
