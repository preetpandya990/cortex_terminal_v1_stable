# Paper Trading & Audit Feature

**Date Implemented:** 2026-05-02  
**Status:** Complete (backend + frontend)  
**Author:** Het Trivedi  

---

## Overview

A full paper (simulated) trading system layered on top of the Cortex AI signal pipeline. Users can create a virtual portfolio, execute orders derived from ML trade suggestions, and track live P&L via WebSocket — all without touching real capital. Every trade outcome is stored immutably for ML feedback and audit.

---

## Architecture

```
TradeSuggestion (ML signal)
       │
       ▼
PlaceOrderRequest ──► order_service ──► PaperFill
       │                                    │
       │                             position_service (WAC)
       │                                    │
       │                             PaperPosition
       │                                    │
       ▼                                    ▼
pnl_worker (500 ms)          outcome_service (on close)
       │                                    │
  Redis pub/sub                     PaperTradeOutcome
  cai:paper:pnl:{id}                (ML feedback fields)
       │
       ▼
 Frontend WS
 LivePnLUpdate frame
```

---

## Backend

### Database Migration
**File:** `backend/alembic/versions/0014_paper_trading.py`

- 6 tables, 28 indexes, 2 TimescaleDB hypertables
- Tables: `portfolios`, `paper_orders`, `paper_fills`, `paper_positions`, `paper_trade_outcomes`, `paper_pnl_snapshots`
- Partial unique index `uq_portfolios_user_active` — enforces one active portfolio per user

### Models
**File:** `backend/app/models/paper_trading.py`

| Model | Key Fields |
|-------|-----------|
| `Portfolio` | `initial_capital`, `current_cash`, `risk_per_trade_pct`, `max_open_positions` |
| `PaperOrder` | `suggestion_id`, `transaction_type`, `product_type`, `order_type`, `status` |
| `PaperFill` | `fill_price`, `slippage_bps`, full NSE charge breakdown, `settlement_date` |
| `PaperPosition` | `avg_cost_price` (WAC), `unrealized_pnl`, `stop_loss`, `target_price_1/2/3` |
| `PaperTradeOutcome` | Immutable audit record with ML feedback fields |
| `PaperPnlSnapshot` | Daily EOD snapshot (TimescaleDB hypertable) |

### Schemas
**File:** `backend/app/schemas/paper_trading.py`

- 9 enums: `PortfolioType`, `TransactionType`, `ProductType`, `OrderType`, `OrderValidity`, `OrderStatus`, `PositionSide`, `PositionStatus`, `ExitReason`
- 4 request schemas: `CreatePortfolioRequest`, `UpdatePortfolioSettingsRequest`, `PlaceOrderRequest`, `ClosePositionRequest`
- 8 response schemas, 4 list schemas, 2 utility schemas, 2 real-time WS schemas
- All `Decimal` DB columns serialised as `float` via `field_validator(mode="before")`

### Services

#### `charge_calculator.py`
NSE statutory charge engine (post-Oct 2024 rates):
- STT: 0.1% on buy (CNC equity)
- Exchange: 0.00297% both sides
- SEBI: ₹10/crore
- GST: 18% on (exchange + SEBI)
- Stamp duty: 0.015% on buy (CNC), 0.003% (MIS)
- Pure `Decimal` arithmetic throughout — no float

#### `qty_suggester.py`
Kelly-style risk-based position sizing:
```
raw_qty = (current_cash × risk_pct / 100) / (entry − stop_loss)
suggested_qty = max(1, min(floor(raw_qty), max_affordable_qty))
```
Returns all intermediate values so the UI can display the rationale.

#### `portfolio_service.py`
- Portfolio CRUD with `DuplicatePortfolioError` on second active portfolio (caught from `IntegrityError` on `uq_portfolios_user_active`)
- `get_portfolio_summary()` — three SQL aggregate queries, zero ORM row loading (O(1) memory)
- `deduct_cash()` / `credit_cash()` helpers used by order_service

#### `order_service.py`
- `place_order()` → `_place_buy_order()` or `_place_sell_order()`
- `_execute_fill()` — reads LTP from `cai:ltp:{instrument_key}`, applies 3 bps slippage, calls `calculate_charges()`, creates `PaperFill`
- `_compute_settlement_date()` — CNC BUY → next NSE trading day via `nse_calendar`; skips weekends and holidays
- `_assert_t1_settlement()` — blocks CNC SELL if shares unsettled (T+1 enforcement)
- `execute_pending_order_fill()` — called by pnl_worker when LIMIT/SL/SL-M price is breached

#### `position_service.py`
- WAC recalculation on every add: `new_avg = (old_qty × old_avg + fill_qty × fill_price) / new_qty`
- `apply_sell_fill_to_position()` — partial or full close; writes `PaperTradeOutcome` on full close
- `close_position()` — REST close: reads LTP, applies slippage, creates synthetic SELL order+fill

#### `outcome_service.py`
- `compute_ml_feedback()` — populates `ml_direction_correct`, `hit_tp1/2/3`, `hit_sl`
- `_get_regime_at_entry()` — queries `ai_regime_detections` table, graceful `None` on error
- `get_outcome_stats()` — aggregate stats with segment breakdowns by confidence, regime, direction

#### `pnl_worker.py`
Entry point: `run_pnl_worker(redis)` — started as `asyncio.create_task` in app lifespan.

- Subscribes to `cai:market-feed:ltpc` pub/sub channel
- Writes `cai:ltp:{instrument_key}` (TTL 300 s) on every tick
- SADDs portfolio IDs to `cai:paper:dirty_portfolios` set
- Every 500 ms: drains dirty set, recomputes P&L, publishes `LivePnLUpdate` to `cai:paper:pnl:{portfolio_id}`
- Auto-closes positions at SL/TP breach (SL checked first, then TP3→TP2→TP1)
- ML feedback scheduled as `asyncio.create_task` after auto-close (non-blocking)
- Uses `WorkerSessionLocal` (separate DB pool) to avoid starving the API connection pool
- Instrument→portfolio cache in Redis HSET with 60 s TTL

### API Router
**File:** `backend/app/api/v1/paper_trading.py`  
**Prefix:** `/api/v1/paper-trading`

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/portfolios` | Create portfolio |
| `GET` | `/portfolios/me` | Active portfolio + live stats |
| `PATCH` | `/portfolios/me` | Update risk settings |
| `POST` | `/orders` | Place order from suggestion |
| `GET` | `/orders` | List orders (cursor-paginated) |
| `DELETE` | `/orders/{order_id}` | Cancel PENDING/OPEN order |
| `GET` | `/qty-suggestion` | System-suggested quantity |
| `GET` | `/positions` | List positions |
| `GET` | `/positions/{position_id}` | Position detail (fills + outcome) |
| `POST` | `/positions/{position_id}/close` | Close position |
| `GET` | `/outcomes` | Audit log (admin/dev role) |
| `GET` | `/outcomes/stats` | ML feedback stats (admin/dev role) |
| `GET` | `/pnl/snapshots` | Historical daily P&L |
| `WS` | `/ws/pnl` | Real-time P&L stream |

**Notes:**
- `from __future__ import annotations` must NOT be used in the router file — FastAPI resolves route param types at decoration time, not lazily
- WS endpoint authenticates via `?token=` query param (not in-band message)
- Cursor pagination uses base64-encoded `{timestamp}:{uuid}` for orders and outcomes

### Redis Keys

| Key | Type | TTL | Purpose |
|-----|------|-----|---------|
| `cai:ltp:{instrument_key}` | String | 300 s | Current LTP written by pnl_worker |
| `cai:paper:dirty_portfolios` | Set | — | Portfolio IDs pending P&L recompute |
| `cai:paper:symbol_portfolios` | Hash | 60 s | instrument_key → [portfolio_ids] cache |
| `cai:paper:pnl:{portfolio_id}` | Pub/Sub | — | LivePnLUpdate frames |

### Registration in `main.py`
```python
app.include_router(paper_trading.router, prefix="/api/v1/paper-trading")
app.include_router(paper_trading.ws_router, prefix="/api/v1")

# In lifespan:
pnl_worker_task = asyncio.create_task(run_pnl_worker(get_redis()), name="paper_trading_pnl_worker")
```

---

## Frontend

### Stack
Next.js 16 · React 19 · TypeScript 5 · TanStack Query v5 · Tailwind CSS v4 · Axios

### TypeScript Types
**File:** `frontend/src/types/paper_trading.ts`

All backend enums as string literal unions, all request/response interfaces, WS payload types, query param interfaces, and UI helper constants (`SIDE_COLORS`, `ORDER_STATUS_COLORS`, `EXIT_REASON_LABELS`).

### API Client Extension
**File:** `frontend/src/lib/api.ts` — `paperTradingAPI`

12 typed methods following the existing `requestData()` pattern:

```typescript
paperTradingAPI.getMyPortfolio()
paperTradingAPI.createPortfolio(payload)
paperTradingAPI.updatePortfolioSettings(payload)
paperTradingAPI.placeOrder(payload)
paperTradingAPI.getOrders(params?)
paperTradingAPI.cancelOrder(orderId)
paperTradingAPI.getPositions(params?)
paperTradingAPI.getPositionDetail(positionId)
paperTradingAPI.closePosition(positionId, payload)
paperTradingAPI.getQtySuggestion(suggestionId)
paperTradingAPI.getOutcomes(params?)
paperTradingAPI.getOutcomeStats()
paperTradingAPI.getPnlSnapshots(params?)
```

### React Query Hooks
**File:** `frontend/src/hooks/usePaperTrading.ts`

Structured query keys under `paperTradingKeys`. 13 hooks:

- `usePortfolioSummary()` — stale 30 s, 404 → no-retry (no portfolio yet)
- `useCreatePortfolio()` — invalidates portfolio
- `useUpdatePortfolioSettings()` — invalidates portfolio
- `usePositions(params?)` — stale 10 s, refetch every 30 s
- `usePositionDetail(positionId)` — stale 10 s
- `useClosePosition()` — invalidates positions, portfolio, orders
- `useOrders(params?)` — stale 15 s
- `usePlaceOrder()` — invalidates orders, positions, portfolio
- `useCancelOrder()` — invalidates orders
- `useQtySuggestion(suggestionId)` — stale 60 s
- `useOutcomes(params?)` — stale 60 s
- `useOutcomeStats()` — stale 120 s
- `usePnlSnapshots(params?)` — stale 300 s

### P&L WebSocket Hook
**File:** `frontend/src/hooks/usePnLWebSocket.ts`

Purpose-built hook for `LivePnLUpdate` frames.

**Key design decisions:**
- Token passed as `?token=` query param in WS URL (matches backend requirement)
- `positionPnLMap` is a `MutableRefObject<Map<string, LivePositionPnL>>` — updated on every 500 ms frame with **no parent re-render**
- `portfolioStats` is React state — triggers exactly one re-render per frame for the summary card
- Per-row components poll the shared ref at 500 ms via `setInterval` — only the changed row re-renders
- Exponential backoff: `min(base × 2^attempt, 30_000 ms)` + ±20% jitter, max 10 attempts

### Components

#### `CreatePortfolioModal.tsx`
- Capital quick-select presets (₹1L, ₹5L, ₹10L, ₹50L)
- Kelly risk % range slider (0.5–5%, step 0.5)
- Max open positions quick-select (5/10/15/20) + custom input
- Live summary line: "₹5,00,000 capital · 2% risk = ₹10,000 risked per trade · up to 10 positions"
- Client-side validation before mutation

#### `ClosePositionModal.tsx`
- Live price badge (from WS tick passed by parent row)
- "All" shortcut button to fill full open quantity
- MARKET / LIMIT order type toggle
- Estimated P&L preview (excludes charges, labelled accordingly)
- Exit reason dropdown (MANUAL / TP1–3 / SL / EXPIRED)

#### `PortfolioSummaryCard.tsx`
- 6-stat grid: Portfolio Value, Cash Available, Unrealized P&L, Realized P&L, Open Positions, Win Rate
- Prefers live WS `portfolioStats`, falls back silently to REST response
- Total return % badge (green/red) with trend icon
- Settings button hook for future settings modal

#### `OpenPositionsTable.tsx`
Full replacement for `OpenPositionsPlaceholder.tsx`.

States handled:
1. **Loading** — spinner
2. **No portfolio** — empty-state CTA → `CreatePortfolioModal`
3. **API error** — error banner
4. **No open positions** — empty-state message
5. **Positions** — live P&L table

Table columns: Symbol, Side, Qty, Avg Cost, Last Price, Unrealized P&L, P&L %, Stop Loss, Target 1, Close button

Feed status badge: Live (green) / Connecting (amber) / Offline (grey)

Aggregate footer: position count + total unrealized P&L.

### Page Wiring
**File:** `frontend/src/app/page.tsx`

```tsx
{isAuthReady && isAuthenticated && <OpenPositionsTable />}
```

`OpenPositionsPlaceholder` import removed and replaced.

---

## Business Rules Implemented

| Rule | Implementation |
|------|---------------|
| One active portfolio per user | Partial unique index + `DuplicatePortfolioError` (409) |
| T+1 settlement (CNC BUY) | `_compute_settlement_date()` + `_assert_t1_settlement()` |
| 3 bps slippage simulation | BUY: `ltp × 1.0003`, SELL: `ltp × 0.9997` |
| NSE statutory charges | `charge_calculator.py` (post-Oct 2024 rates) |
| WAC cost basis (SEBI mandated) | `_update_wac()` in `position_service.py` |
| SL/TP auto-close priority | SL first, then TP3 → TP2 → TP1 |
| ML feedback on every close | `outcome_service.compute_ml_feedback()` async |

---

## Pending

- **Strategy Marketplace** — deferred pending user-provided strategy document
- **`PlaceOrderModal.tsx`** — "Enter Trade" modal from `TradeSuggestionCard` (deferred)
- **Portfolio Settings Modal** — settings button wired but modal not yet built
