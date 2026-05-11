# Trading Strategies Feature — Task Tracker

## Phase 1 — Foundation (DB + CRUD + User Profile)

- [x] Migration `0016_trading_strategies.py` — all 6 new tables + continuous aggregate
- [x] SQLAlchemy ORM models — `UserPreferences`, `Strategy`, `UserStrategySubscription`, `StrategyActivation`, `StrategyTrade`, `StrategyBacktestRun`
- [x] Pydantic schemas — `StrategyDefinition`, `ConditionGroup/Leaf`, `StopLossConfig`, `TakeProfitConfig`, `PositionSizingConfig` and all sub-schemas
- [x] Strategy CRUD API — `POST/GET/PUT/DELETE /api/v1/strategies` + duplicate + subscriptions + activations + trades + backtests
- [x] User preferences API — `GET/PUT /api/v1/users/me/preferences`
- [x] User profile API — `GET/PUT /api/v1/users/me/profile` (name, email, password)
- [x] Security fix — `hash_password` / `verify_password` moved to `app.core.security`; `auth.py` imports from there
- [x] Frontend — AppHeader avatar → accessible dropdown (Profile & Settings, My Strategies, Sign Out)
- [x] Frontend — `/settings` page (profile edit + enforcement mode selector with Hard Gate notice)
- [x] Frontend — `/strategies` page skeleton (Global Library tab + My Strategies tab + subscription strip)
- [x] Frontend — TypeScript types (`src/types/strategies.ts`) + API client (`strategiesAPI`, `userAPI` in `lib/api.ts`)

## Phase 2 — Rule Engine

- [x] `MarketContext` resolver — signal fields + Redis LTP cache + staleness enforcement (`app/services/strategy_engine/market_context.py`)
- [x] `StrategyRuleEvaluator` — recursive condition tree (AND / OR / NOT, all operators, short-circuit) (`app/services/strategy_engine/rule_evaluator.py`)
- [x] `StrategyFilterPipeline` — 7-gate pipeline with full `GateResult` audit trail (`app/services/strategy_engine/filter_pipeline.py`)
- [x] `StrategyDispatcher` — per-user fanout + enforcement mode routing (`app/services/strategy_engine/dispatcher.py`)
- [x] Hook `StrategyDispatcher` into `SignalAssembler.assemble_signal()` (fire-and-forget `asyncio.create_task`)
- [x] Migration `0017` — add `strategy_id`, `strategy_activation_id`, `strategy_match_result` to `trade_suggestions`

## Phase 3 — State Machine + Auto-Execution

- [x] `StrategyActivationFSM` — full state transitions (WATCHING → IN_SETUP → IN_TRADE → PARTIAL_EXIT → EXITED) + state_history logging
- [x] Auto-execute integration — `PaperOrderService` call on `IN_TRADE` entry when `auto_execute=true`
- [x] SL / TP position monitoring worker — poll Redis LTP, trigger FSM transitions on hit
- [x] Activation API — `GET /activations`, `GET /activations/{id}`, `POST /activations/{id}/exit`
- [x] Frontend — Strategy builder wizard (6-step: metadata → signal gates → condition tree → risk → sizing → guardrails)
- [x] Frontend — Active activations panel with state badges

## Phase 4 — Performance & Subscriptions

- [x] `strategy_trades` write path — record on every FSM exit with full trade data
- [x] Performance metrics computation — Sharpe, Sortino, Calmar, max drawdown, win rate (numpy-based from trade rows)
- [x] Subscription management API — subscribe, unsubscribe, reorder priorities
- [x] Performance API — `GET /strategies/{id}/performance`, `GET /strategies/{id}/trades`
- [x] Frontend — Strategy detail page (equity curve, metric cards, trade history table, subscribe/unsubscribe)
- [x] Frontend — Subscription priority reorder UI (up/down buttons with atomic save)

## Phase 5 — Backtesting

- [x] `BacktestEngine` — signal_replay mode (historical `ai_trading_signals` + `ml_features`)
- [x] `BacktestEngine` — model_replay mode (re-run `EnsemblePredictor` on historical OHLCV)
- [x] Hybrid seam logic — auto-detect signal history boundary, splice both modes
- [x] Background task runner + DB progress updates (`WorkerSessionLocal`, cancellation-aware)
- [x] Backtest API — fire `run_backtest_task` via `asyncio.create_task` after commit
- [x] Frontend — "New Backtest" modal (symbol, date range, mode selector) embedded in strategy detail page
- [x] Frontend — Backtest results page (`/strategies/[id]/backtests/[runId]`) — status tracker, metric cards, equity curve, per-symbol stats, trade log

## Phase 6 — Admin & Library

- [x] Migration `0018_strategy_audit_logs.py` — append-only `strategy_audit_logs` table with CHECK constraints + indexes
- [x] `StrategyAuditLog` ORM model — tamper-evident FK SET NULL on cascade so audit survives deletions
- [x] Admin schemas — `AdminStrategyItemResponse` (with `creator_username`), `PromoteStrategyRequest`, `StrategyAuditLogResponse`, list wrappers
- [x] Admin service — `admin_list_strategies`, `promote_strategy`, `demote_strategy`, `get_strategy_audit_log` (all with JOIN to users for username)
- [x] Admin API router `admin_strategies.py` — `GET /admin/strategies`, `POST /{id}/promote`, `DELETE /{id}/promote`, `GET /{id}/audit-log` — all behind `AdminUserID` JWT gate
- [x] Registered in `main.py` under `/api/v1/admin/strategies`
- [x] Frontend — `/admin/strategies` governance page — paginated data table, scope/status/search filters, promote/demote reason dialogs (audit-logged), slide-in audit history drawer, stat strip
- [x] Frontend — Admin sidebar nav extended with "Strategies" entry
- [x] Frontend — `adminStrategiesAPI` client in `api.ts` with full TypeScript types
- [x] Frontend — Global library quick-subscribe flow — "Subscribe" button on library cards opens inline priority modal without navigating away, refreshes subscription list on success

---

## Status Key
`[ ]` Not started — `[~]` In progress — `[x]` Done
google-chromegooglr-chrome