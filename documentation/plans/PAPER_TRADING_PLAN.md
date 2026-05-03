# Paper Trading & Audit — Implementation Plan

**Status:** Design Complete / Ready to Implement  
**Migration:** `0014_paper_trading.py`  
**Date:** 2026-05-01

---

## 1. Overview

Translate `TradeSuggestion` records into simulated per-user paper trades with live P&L via the existing Upstox WebSocket tick feed. Outcomes are stored in an append-only audit table that feeds back into ML/AI fine-tuning. Admin/Dev users see aggregated outcome stats on the main dashboard.

**Core flow:**
```
TradeSuggestion → "Enter Trade" click → qty suggestion (WAC + risk %)
  → user adjusts → paper_order created → fill against Upstox tick
  → paper_position opened (live P&L via Redis) → user closes / SL/TP hit
  → paper_trade_outcome written → ML feedback pipeline
```

---

## 2. Database Schema (migration `0014`)

### 2.1 `portfolios`
One active paper portfolio per user.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `user_id` | INT FK → `users.id` ON DELETE CASCADE | UNIQUE (one portfolio per user for now) |
| `portfolio_type` | VARCHAR(10) DEFAULT `'PAPER'` | CHECK IN (`PAPER`, `LIVE`) |
| `name` | VARCHAR(100) | User-defined label |
| `initial_capital` | NUMERIC(18,2) | Immutable after creation |
| `current_cash` | NUMERIC(18,2) | Updated on every fill |
| `currency` | VARCHAR(3) DEFAULT `'INR'` | |
| `risk_per_trade_pct` | NUMERIC(4,2) DEFAULT `2.00` | % capital risked per trade (0.5–10) |
| `max_open_positions` | INT DEFAULT `10` | Hard cap on concurrent positions |
| `is_active` | BOOL DEFAULT `true` | Soft-delete |
| `created_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `updated_at` | TIMESTAMPTZ DEFAULT NOW() | |

**Indexes:** `(user_id)` UNIQUE WHERE `is_active = true`

---

### 2.2 `paper_orders`
Append-only intent records. One row per user order action.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `portfolio_id` | UUID FK → `portfolios.id` | |
| `suggestion_id` | UUID FK → `trade_suggestions.suggestion_id` ON DELETE SET NULL | Source suggestion |
| `symbol` | VARCHAR(50) NOT NULL | |
| `instrument_key` | VARCHAR(100) NOT NULL | |
| `transaction_type` | VARCHAR(4) NOT NULL | CHECK IN (`BUY`, `SELL`) |
| `product_type` | VARCHAR(10) NOT NULL | CHECK IN (`CNC`, `MIS`, `NRML`) |
| `order_type` | VARCHAR(10) NOT NULL | CHECK IN (`MARKET`, `LIMIT`, `SL`, `SL-M`) |
| `quantity` | INT NOT NULL | User-confirmed qty |
| `price` | NUMERIC(12,4) | Limit price (NULL for MARKET) |
| `trigger_price` | NUMERIC(12,4) | SL trigger (NULL if not SL order) |
| `status` | VARCHAR(12) DEFAULT `'PENDING'` | CHECK IN (`PENDING`, `OPEN`, `COMPLETE`, `REJECTED`, `CANCELLED`) |
| `validity` | VARCHAR(6) DEFAULT `'DAY'` | CHECK IN (`DAY`, `IOC`) |
| `rejection_reason` | VARCHAR(200) | Populated on REJECTED status |
| `placed_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `updated_at` | TIMESTAMPTZ DEFAULT NOW() | |

**Indexes:** `(portfolio_id, status)`, `(suggestion_id)`, `(symbol, placed_at DESC)`

---

### 2.3 `paper_fills`
Immutable execution records. TimescaleDB hypertable on `executed_at`.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `order_id` | UUID FK → `paper_orders.id` | |
| `portfolio_id` | UUID FK → `portfolios.id` | Denormalized for query speed |
| `symbol` | VARCHAR(50) NOT NULL | |
| `fill_quantity` | INT NOT NULL | |
| `fill_price` | NUMERIC(12,4) NOT NULL | Actual simulated fill price |
| `slippage_bps` | NUMERIC(6,2) DEFAULT `0` | Applied slippage (2–5 bps for liquid stocks) |
| `brokerage` | NUMERIC(10,4) DEFAULT `0` | Flat ₹20 or 0.03% |
| `stt` | NUMERIC(10,4) DEFAULT `0` | 0.1% CNC / 0.025% MIS sell-side |
| `exchange_charges` | NUMERIC(10,4) DEFAULT `0` | 0.00335% |
| `sebi_charges` | NUMERIC(10,4) DEFAULT `0` | ₹10/crore |
| `gst` | NUMERIC(10,4) DEFAULT `0` | 18% on brokerage + exchange charges |
| `stamp_duty` | NUMERIC(10,4) DEFAULT `0` | 0.015% CNC buy / 0.003% MIS buy |
| `total_charges` | NUMERIC(10,4) NOT NULL | Sum of all charges |
| `net_amount` | NUMERIC(14,4) NOT NULL | `fill_qty * fill_price + total_charges` |
| `settlement_date` | DATE NOT NULL | T+1 for CNC delivery |
| `executed_at` | TIMESTAMPTZ NOT NULL | Hypertable partition key |

**Hypertable:** `create_hypertable('paper_fills', 'executed_at', chunk_time_interval => INTERVAL '1 month')`  
**Indexes:** `(portfolio_id, executed_at DESC)`, `(order_id)`, `(symbol, executed_at DESC)`

---

### 2.4 `paper_positions`
Mutable live state. One row per open position per portfolio.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `portfolio_id` | UUID FK → `portfolios.id` | |
| `suggestion_id` | UUID FK → `trade_suggestions.suggestion_id` ON DELETE SET NULL | |
| `symbol` | VARCHAR(50) NOT NULL | |
| `instrument_key` | VARCHAR(100) NOT NULL | |
| `quantity` | INT NOT NULL | Current open qty |
| `avg_cost_price` | NUMERIC(12,4) NOT NULL | Weighted average cost (WAC) |
| `last_price` | NUMERIC(12,4) | Updated from tick feed (not truth) |
| `unrealized_pnl` | NUMERIC(14,4) | `(last_price - avg_cost) * qty` — cached |
| `realized_pnl` | NUMERIC(14,4) DEFAULT `0` | Accumulated from partial closes |
| `total_charges` | NUMERIC(10,4) DEFAULT `0` | Cumulative charges on this position |
| `side` | VARCHAR(5) NOT NULL | CHECK IN (`LONG`, `SHORT`) |
| `target_price_1` | NUMERIC(12,4) | From source suggestion |
| `target_price_2` | NUMERIC(12,4) | |
| `target_price_3` | NUMERIC(12,4) | |
| `stop_loss` | NUMERIC(12,4) | From source suggestion |
| `status` | VARCHAR(8) DEFAULT `'OPEN'` | CHECK IN (`OPEN`, `CLOSED`) |
| `opened_at` | TIMESTAMPTZ DEFAULT NOW() | |
| `closed_at` | TIMESTAMPTZ | Populated on close |
| `updated_at` | TIMESTAMPTZ DEFAULT NOW() | |

**Constraints:** UNIQUE `(portfolio_id, symbol)` WHERE `status = 'OPEN'`  
**Indexes:** `(portfolio_id, status)`, `(symbol)`, `(suggestion_id)`

---

### 2.5 `paper_trade_outcomes`
Append-only audit + ML feedback. Written on position close.

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `portfolio_id` | UUID FK → `portfolios.id` | |
| `position_id` | UUID FK → `paper_positions.id` | |
| `suggestion_id` | UUID FK → `trade_suggestions.suggestion_id` ON DELETE SET NULL | |
| `user_id` | INT FK → `users.id` | Denormalized for fast admin queries |
| `symbol` | VARCHAR(50) NOT NULL | |
| `signal_direction` | VARCHAR(4) NOT NULL | BUY / SELL |
| `entry_price` | NUMERIC(12,4) NOT NULL | Actual fill price |
| `exit_price` | NUMERIC(12,4) NOT NULL | Actual exit fill price |
| `quantity` | INT NOT NULL | |
| `gross_pnl` | NUMERIC(14,4) NOT NULL | Before charges |
| `total_charges` | NUMERIC(10,4) NOT NULL | All NSE charges |
| `net_pnl` | NUMERIC(14,4) NOT NULL | After charges |
| `pnl_pct` | NUMERIC(8,4) NOT NULL | `net_pnl / (entry_price * qty) * 100` |
| `hold_duration_seconds` | INT NOT NULL | Position lifetime |
| `exit_reason` | VARCHAR(10) NOT NULL | CHECK IN (`TP1`, `TP2`, `TP3`, `SL`, `MANUAL`, `EXPIRED`) |
| `suggested_entry_price` | NUMERIC(12,4) | From TradeSuggestion |
| `suggested_stop_loss` | NUMERIC(12,4) | |
| `suggested_tp1` | NUMERIC(12,4) | |
| `suggested_tp2` | NUMERIC(12,4) | |
| `suggested_tp3` | NUMERIC(12,4) | |
| `suggestion_consensus_score` | NUMERIC(5,2) | |
| `suggestion_confidence_level` | VARCHAR(10) | HIGH / MEDIUM / LOW |
| `entry_slippage_pct` | NUMERIC(6,4) | `(actual_entry - suggested_entry) / suggested_entry * 100` |
| `ml_direction_correct` | BOOL | Did price move in predicted direction? |
| `hit_tp1` | BOOL DEFAULT FALSE | |
| `hit_tp2` | BOOL DEFAULT FALSE | |
| `hit_tp3` | BOOL DEFAULT FALSE | |
| `hit_sl` | BOOL DEFAULT FALSE | |
| `market_regime_at_entry` | VARCHAR(50) | From `ai_regime_detections` at entry time |
| `created_at` | TIMESTAMPTZ DEFAULT NOW() | Immutable — no `updated_at` |

**Indexes:** `(portfolio_id, created_at DESC)`, `(user_id, created_at DESC)`, `(suggestion_id)`, `(symbol, created_at DESC)`, `(ml_direction_correct)`, `(exit_reason)`

---

### 2.6 `paper_pnl_snapshots`
Daily EOD snapshots. TimescaleDB hypertable on `captured_at`.

| Column | Type | Notes |
|---|---|---|
| `id` | BIGSERIAL PK | |
| `portfolio_id` | UUID FK → `portfolios.id` | |
| `snapshot_date` | DATE NOT NULL | |
| `total_realized_pnl` | NUMERIC(14,4) | |
| `total_unrealized_pnl` | NUMERIC(14,4) | |
| `portfolio_value` | NUMERIC(14,4) | `current_cash + sum(position_values)` |
| `cash_balance` | NUMERIC(14,4) | |
| `num_open_positions` | INT | |
| `num_trades_today` | INT | |
| `win_rate` | NUMERIC(5,2) | Wins / total closed trades today |
| `nifty_close` | NUMERIC(12,4) | For benchmark comparison |
| `captured_at` | TIMESTAMPTZ NOT NULL | Hypertable partition key |

**Hypertable:** `create_hypertable('paper_pnl_snapshots', 'captured_at', chunk_time_interval => INTERVAL '1 year')`

---

## 3. API Endpoints (`/api/v1/paper-trading`)

All endpoints require authentication. `GET /outcomes` and `GET /outcomes/stats` additionally require `role IN ('admin', 'dev')`.

| Method | Route | Description |
|---|---|---|
| POST | `/portfolios` | Create paper portfolio (one per user) |
| GET | `/portfolios/me` | Get current user's portfolio + summary |
| PATCH | `/portfolios/me` | Update risk settings (`risk_per_trade_pct`, `max_open_positions`) |
| POST | `/orders` | Place order from a suggestion |
| GET | `/orders` | List orders with cursor pagination |
| DELETE | `/orders/{order_id}` | Cancel PENDING/OPEN order |
| GET | `/positions` | List open positions |
| GET | `/positions/{position_id}` | Single position detail |
| POST | `/positions/{position_id}/close` | Close (full or partial) |
| GET | `/outcomes` | Audit log — all trade outcomes (admin/dev only) |
| GET | `/outcomes/stats` | Aggregated ML feedback stats (admin/dev only) |
| GET | `/pnl/snapshots` | Historical daily P&L snapshots |
| GET | `/qty-suggestion` | Get system-suggested qty for a suggestion |
| WS | `/ws/pnl` | Real-time portfolio P&L stream |

---

## 4. Qty Suggestion Logic

```python
suggested_qty = floor(
    (portfolio.current_cash * portfolio.risk_per_trade_pct / 100)
    / (suggestion.entry_price - suggestion.stop_loss)
)
# Clamped: minimum 1, maximum floor(current_cash / entry_price)
```

Returns `{ suggested_qty, max_affordable_qty, capital_at_risk, risk_pct }` so the user sees exactly what they're risking.

---

## 5. NSE Charge Simulation

Applied on every fill. Constants (Zerodha model):

```python
BROKERAGE_FLAT = 20.0          # ₹20 flat per order
BROKERAGE_PCT = 0.0003         # 0.03% (whichever is lower)
STT_CNC_BOTH = 0.001           # 0.1% on buy + sell
STT_MIS_SELL = 0.00025         # 0.025% on sell side only
EXCHANGE_CHARGE = 0.0000335    # 0.00335%
SEBI_CHARGES = 10 / 1e7        # ₹10 per crore = 0.000001
GST_RATE = 0.18                # 18% on brokerage + exchange charges
STAMP_DUTY_CNC_BUY = 0.00015  # 0.015%
STAMP_DUTY_MIS_BUY = 0.00003  # 0.003%
SLIPPAGE_BPS_DEFAULT = 3       # 3 bps for liquid large-caps
```

---

## 6. Real-Time P&L Architecture

```
Upstox WebSocket (existing BaseWebSocketService)
  └─ on_tick(symbol, last_price)
       └─ HSET ticks:{symbol} last_price {value}
       └─ SADD dirty_portfolios {portfolio_ids with open position in symbol}

P&L Recompute Worker (500ms interval)
  └─ for each portfolio_id in dirty_portfolios:
       └─ fetch open positions from Redis / DB
       └─ recompute unrealized_pnl per position
       └─ check SL/TP breach → trigger auto-close if breached
       └─ PUBLISH cai:paper:pnl:{portfolio_id} {payload}
       └─ SREM dirty_portfolios {portfolio_id}

Frontend WebSocket /ws/pnl
  └─ subscribes to cai:paper:pnl:{portfolio_id}
  └─ receives: { portfolio_id, positions: [...], total_unrealized, total_realized,
                  portfolio_value, cash_balance, ts }
```

New Redis channels to add to `RedisChannels`:
- `PAPER_PNL_PORTFOLIO = "cai:paper:pnl:{portfolio_id}"`
- `PAPER_ORDER_STATUS = "cai:paper:order:{order_id}"`

---

## 7. File Structure

```
backend/
  app/
    models/
      paper_trading.py          # Portfolio, PaperOrder, PaperFill, PaperPosition,
                                #   PaperTradeOutcome, PaperPnlSnapshot ORM models
    schemas/
      paper_trading.py          # All Pydantic v2 request/response schemas
    services/
      paper_trading/
        __init__.py
        portfolio_service.py    # Portfolio CRUD + cash management
        order_service.py        # Order placement, cancellation, fill simulation
        position_service.py     # Position open/close, WAC calc, charge calc
        pnl_worker.py           # 500ms recompute worker, SL/TP auto-close
        outcome_service.py      # Outcome record writing, ML feedback fields
        qty_suggester.py        # Risk-based qty suggestion
        charge_calculator.py    # NSE charge constants + calculation
    api/
      v1/
        paper_trading.py        # All REST + WebSocket routes
  alembic/
    versions/
      0014_paper_trading.py     # All 6 tables + hypertables + indexes

frontend/
  src/
    app/
      paper-trading/
        page.tsx                # Main paper trading page
    components/
      paper-trading/
        PortfolioSummaryCard.tsx
        OpenPositionsTable.tsx  # Replaces OpenPositionsPlaceholder.tsx
        OrderEntryModal.tsx     # "Enter Trade" modal with qty suggestion
        TradeHistoryTable.tsx
        PnLChart.tsx            # Portfolio value over time
        AuditOutcomesPanel.tsx  # Admin/Dev only
    hooks/
      usePaperTradingPnL.ts     # WebSocket hook for live P&L
      usePaperPortfolio.ts
    types/
      paper-trading.ts          # TypeScript interfaces
```

---

## 8. Audit → ML Feedback Pipeline

After every `paper_trade_outcome` INSERT, a background task:
1. Computes `ml_direction_correct` by checking if price moved in `signal_direction` within `hold_duration`
2. Checks `hit_tp1/2/3` and `hit_sl` from OHLCV data post-exit
3. Writes to `paper_trade_outcomes` (all within the same transaction as position close)

`GET /outcomes/stats` (Admin/Dev) returns:
```json
{
  "total_trades": 142,
  "win_rate": 0.63,
  "ml_direction_accuracy": 0.71,
  "avg_pnl_pct": 0.84,
  "by_confidence_level": { "HIGH": {...}, "MEDIUM": {...}, "LOW": {...} },
  "by_exit_reason": { "TP1": 45, "TP2": 18, "SL": 32, "MANUAL": 47 },
  "by_regime": { "TRENDING_UP": {...}, "RANGING": {...} },
  "avg_hold_duration_hours": 6.2
}
```

This directly identifies which ML confidence levels, market regimes, and signal pathways produce profitable outcomes — actionable fine-tuning signals.

---

## 9. Implementation Order

1. `0014_paper_trading.py` — migration (all tables, hypertables, indexes)
2. `app/models/paper_trading.py` — ORM models
3. `app/schemas/paper_trading.py` — Pydantic v2 schemas
4. `app/services/paper_trading/charge_calculator.py` — NSE charges
5. `app/services/paper_trading/qty_suggester.py` — risk-based qty
6. `app/services/paper_trading/portfolio_service.py` — portfolio CRUD
7. `app/services/paper_trading/order_service.py` — order + fill simulation
8. `app/services/paper_trading/position_service.py` — WAC, open/close
9. `app/services/paper_trading/outcome_service.py` — audit + ML fields
10. `app/services/paper_trading/pnl_worker.py` — Redis recompute worker
11. `app/api/v1/paper_trading.py` — REST + WebSocket routes
12. Register router in `main.py`
13. Frontend — types, hooks, components, page
14. Wire `OpenPositionsPlaceholder.tsx` → real `OpenPositionsTable.tsx`

---

## 10. Key Constraints & Rules

- `paper_fills` and `paper_trade_outcomes` are **append-only** — no UPDATE or DELETE ever
- `paper_positions` UNIQUE `(portfolio_id, symbol)` WHERE `status = 'OPEN'` — prevents duplicate open positions in the same symbol
- WAC recalculated atomically in a DB transaction on every fill
- T+1 settlement enforced: `settlement_date = fill_date + 1 trading day` (uses existing `NSETradingCalendar`)
- Circuit breaker: reject fill if price is outside NSE 20% band from previous close
- SL/TP auto-close triggers in `pnl_worker.py`, not on the REST layer
- All monetary amounts stored as `NUMERIC` — never `FLOAT`
- `paper_trade_outcomes.created_at` only — no `updated_at` (signals immutability)
