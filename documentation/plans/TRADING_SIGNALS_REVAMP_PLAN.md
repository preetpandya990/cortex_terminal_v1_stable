# Trading Signals Revamp — Implementation Plan

**Feature:** Cortex AI → Trading Signals tab  
**Status:** Ready for implementation  
**Standard:** Production-grade, world-class, industry best practices  
**Last Updated:** 2026-04-28

---

## 1. Executive Summary

The Trading Signals section has 11 identified defects ranging from a broken WebSocket bridge (the real-time stream endpoint simply does not exist) to silently ignored API filters, a miscalibrated fusion engine, and a frontend wired to polling instead of live data. This plan addresses every defect in a correct, clean, permanent way — no patches, no hacks, no workarounds.

The implementation is structured into 6 sequential phases. Each phase is independently shippable and leaves the system in a stable, tested state. No phase leaves a half-working feature.

---

## 2. Defect Registry

| ID | Location | Severity | Description |
|----|----------|----------|-------------|
| D-01 | `backend/app/api/v1/cai.py` | **Critical** | `/api/v1/cai/stream` WebSocket endpoint does not exist. Redis pub/sub channels publish but no subscriber bridges to frontend. Real-time signals are completely non-functional. |
| D-02 | `backend/app/api/v1/fusion.py:81` | **Critical** | `PubSubClient()` instantiated without required `redis: Redis` arg → `TypeError` at runtime. Signal generation via POST has never worked end-to-end. |
| D-03 | `backend/app/ai/fusion/signal_assembler.py:265` | **Critical** | `SignalAssembler()` constructed with no `ensemble_predictor` or `feature_loader`, so ML weight (40%) always contributes score=0. Signals are purely event-driven or entirely zero. |
| D-04 | `backend/app/ai/fusion/signal_assembler.py:182` | **High** | `gather_technical_signals` is a permanent stub always returning `score=0, confidence=0`. The 20% technical weight consistently drags BUY/SELL fused scores below their ±50 thresholds, causing legitimate signals to collapse to HOLD. |
| D-05 | `backend/app/api/v1/fusion.py:99–153` | **High** | `GET /fusion/signals` accepts `signal_type`, `time_horizon`, `min_confidence` query params but builds no WHERE clauses for them. All three frontend filters are silently non-functional. |
| D-06 | `backend/app/ai/fusion/models.py` | **High** | `AITradingSignal` has no `time_horizon`, `expires_at`, `target_price`, or `stop_loss` columns. The API hardcodes `time_horizon="intraday"` and `expires_at=generated_at` for every row, making them meaningless. |
| D-07 | `backend/app/ai/fusion/signal_assembler.py:282–288` | **Medium** | Redis publish payload is a slim 5-field object `{signal_id, symbol, action, confidence, timestamp}`. `useSignalsRealtime` tries to inject this directly as a full `TradingSignal` — 9 required fields are missing, causing broken rows in the UI cache. |
| D-08 | `backend/app/ai/fusion/signal_pipeline.py:193` | **Medium** | `process_event_end_to_end` hardcodes `symbols = ["RELIANCE", "TCS"]` instead of reading `affected_symbols` from the classified event. |
| D-09 | `backend/app/ai/fusion/signal_assembler.py:275` | **Low** | `reasoning` is hardcoded as `"Fused signal: {fused_score:.2f}"` — a debug string exposed as a user-facing field rendered in the signals table and detail modal. |
| D-10 | `frontend/src/components/ai/SignalsPanel.tsx:50` | **Medium** | `SignalsPanel` uses `useSignals` (30s polling) instead of `useSignalsRealtime` (polling + WebSocket cache injection). Even after D-01 is fixed, the panel won't receive live updates. |
| D-11 | `frontend/src/hooks/useSignalsRealtime.ts:32` | **Low** | Cache injection checks `signal.confidence` (raw ML score from slim Redis payload) against `filters.min_confidence`, but the UI displays and filters on `calibrated_confidence`. Wrong field reference. |
| D-12 | `frontend/src/app/cortex-ai/page.tsx:38` | **Low** | `ConnectionStatusIndicator` hardcoded `status="connected"` regardless of actual WebSocket state. Always misleads the user. |
| D-13 | — | **High** | No automated signal generation. Signals only exist when `POST /fusion/signals/generate/{symbol}` is called manually. In a trading platform, stale signals are worse than no signals. |

---

## 3. Architecture Overview (Post-Revamp)

```
                          ┌──────────────────────────┐
   NSE Market Hours       │   Signal Scheduler        │
   Nifty50+Next50    ──► │   (asyncio periodic task) │
   High-Impact Events ──► │   15-min cadence          │
   On-demand (watchlist)  └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │   SignalAssembler          │
                          │                            │
                          │  gather_event_signals()    │ ←── ai_event_classifications
                          │    weight: 0.35            │     (48h lookback, decay)
                          │                            │
                          │  gather_ml_signals()       │ ←── EnsemblePredictor (app.state)
                          │    weight: 0.40            │     XGBoost + GRU
                          │                            │
                          │  gather_technical_signals()│ ←── upstox_ohlcv (RSI-14, EMA)
                          │    weight: 0.25            │     candle_tf matches signal_tf
                          │                            │     [Phase 5]
                          │                            │
                          │  fuse_signals()            │
                          │  derive_time_horizon()     │
                          │  compute_expires_at()      │
                          │  build_reasoning()         │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  ai_trading_signals (PG)   │
                          │  + time_horizon            │
                          │  + expires_at              │
                          │  + target_price            │
                          │  + stop_loss               │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  Redis Pub/Sub             │
                          │  cai:signals:all           │ ←── Full signal payload
                          │  cai:signals:{symbol}      │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  /api/v1/cai/stream (WS)  │
                          │  ConnectionManager         │
                          │  In-band JWT auth          │
                          │  JWT exp re-check/30s hb   │
                          │  Redis subscriber task     │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  useCAIWebSocket (hook)    │
                          │  → onSignal callback       │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  useSignalsRealtime        │
                          │  → filter by calibrated_  │
                          │    confidence              │
                          │  → inject into RQ cache    │
                          └──────────┬───────────────┘
                                     │
                          ┌──────────▼───────────────┐
                          │  SignalsPanel              │
                          │  + ConnectionStatusInd.   │
                          │    (live WS status)        │
                          └──────────────────────────┘
```

---

## 4. Implementation Phases

---

### Phase 1 — Database Migration (Foundation)
**Files:** `backend/alembic/versions/0013_trading_signals_revamp.py`, `backend/app/ai/fusion/models.py`

**Goal:** Add the four missing columns to `ai_trading_signals` that the rest of the system depends on. This must be done first as every subsequent phase reads or writes these columns.

#### 1.1 New Alembic Migration: `0013_trading_signals_revamp.py`

Add to `ai_trading_signals`:

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `time_horizon` | `VARCHAR(20)` | Yes | `NULL` | `intraday` / `swing` / `positional` |
| `expires_at` | `TIMESTAMPTZ` | Yes | `NULL` | Computed at assembly time |
| `target_price` | `NUMERIC(12, 2)` | Yes | `NULL` | TP1 from ML prediction |
| `stop_loss` | `NUMERIC(12, 2)` | Yes | `NULL` | Stop loss from ML prediction |

Create two new indexes:
- `idx_ai_signals_time_horizon` on `(time_horizon)` — for time horizon filter
- `idx_ai_signals_expires_at` on `(expires_at)` — for expiry filter in list queries

Both columns are nullable so the migration is backward-compatible with existing rows (they will have NULL, treated as "unknown" by the API).

#### 1.2 Update ORM Model: `AITradingSignal`

Add the four mapped columns to the SQLAlchemy model to match the migration. Types: `String(20)` for `time_horizon`, `DateTime(timezone=True)` for `expires_at`, `Numeric(12, 2)` for both price fields.

**Rollback:** The migration `downgrade()` drops the four columns and their indexes. Fully reversible.

---

### Phase 2 — Backend Core Logic Fixes
**Files:** `backend/app/ai/fusion/signal_assembler.py`, `backend/app/api/v1/fusion.py`, `backend/app/ai/fusion/signal_pipeline.py`

**Goal:** Fix all server-side logic defects: DI wiring, fusion engine correctness, signal assembly completeness, API filters, and Redis payload shape.

#### 2.1 Fix Dependency Injection in the Generate Endpoint (D-02, D-03)

In `fusion.py`, replace the manual `PubSubClient()` and `SignalAssembler()` instantiations with proper FastAPI dependency injection:

- `pubsub: PubSubClient = Depends(get_pubsub_client)` — uses the existing singleton Redis pool
- Pass `app.state.ml_predictor` and a new `FeatureLoader` instance (constructed with the shared `CacheService` and a new `AsyncSession`) into `SignalAssembler` at the call site

The generate endpoint receives the `Request` object (already present via `@limiter.limit`), reads `request.app.state.ml_predictor`, and constructs the assembler inline before calling `assemble_signal`. This is the correct FastAPI pattern for app-state access without polluting the DI tree with non-singleton objects.

#### 2.2 Revamp `SignalAssembler.assemble_signal` (D-04, D-06, D-07, D-09)

Add four new private methods and wire them into `assemble_signal`:

**`_derive_time_horizon(timeframe: str) -> str`**

Maps the prediction timeframe to a time horizon using standard industry rules:

```
1minute / 5minute / 15minute / 30minute  →  intraday
1hour / 4hour / 1D (daily)               →  swing
1week / 1month                           →  positional
```

The current system runs `timeframe="1d"` → all ML signals will be `swing`. Stored in DB.

**`_compute_expires_at(time_horizon: str, generated_at: datetime) -> datetime`**

Computes signal expiry based on time horizon, anchored to NSE market hours:

```
intraday   →  end of current NSE session (15:30 IST same day),
               or if generated after 15:30, end of next session
swing      →  generated_at + 5 trading days (using nse_calendar)
positional →  generated_at + 21 trading days (using nse_calendar)
```

Uses the existing `nse_calendar` from `app.services.market_calendar` — already used by the scanner. Trading day arithmetic skips weekends and NSE holidays correctly.

**`_extract_price_levels(ml_signals: dict) -> tuple[float | None, float | None]`**

Reads `ml_signals["prediction"]["entry_price"]` (= current price at prediction time, which is TP1 equivalent) and `ml_signals["prediction"]["stop_loss"]` from the ML result. These are already computed by `EnsemblePredictor._assemble_prediction` using ATR/volatility-based sizing. Returns `(target_price, stop_loss)` as floats or None if ML was unavailable.

**`_build_reasoning(action, confidence, time_horizon, event_signals, ml_signals, regime) -> str`**

Constructs a human-readable reasoning string from assembled data, no LLM required:

```
"Swing BUY (high confidence · 78.3%): ML ensemble projects bullish momentum 
with 78% confidence. 2 recent events reinforce the setup. Market regime: 
bull_trending. Stop loss at ₹2,680 / Target ₹2,920."
```

Confidence bands: ≥ 0.80 = "high", ≥ 0.60 = "medium", < 0.60 = "low".

**Full Redis publish payload (D-07)**

Replace the slim 5-field Redis publish with the full signal shape — identical to what `GET /fusion/signals` serializes per row. This is the contract `useSignalsRealtime` expects when injecting into the React Query cache.

#### 2.3 Fix Fusion Weights for the Stub Phase (D-04)

Until Phase 5 (technical indicators) is implemented, set weights to `event=0.50, ml=0.50, technical=0.00`. The `__init__` weight-sum validation already enforces they total 1.0. Remove the technical layer call entirely from `assemble_signal` during this phase — don't waste the DB roundtrip for a guaranteed zero.

Document clearly with a `# TODO(phase-5): restore technical weight to 0.25` comment and update `event/ml` to 0.35/0.40 when Phase 5 ships.

#### 2.4 Fix `GET /fusion/signals` Server-Side Filters (D-05)

Apply WHERE clauses for all three filter params:

- `signal_type` → `WHERE action = :signal_type` (uppercase match, since DB stores `BUY`/`SELL`/`HOLD`)
- `time_horizon` → `WHERE time_horizon = :time_horizon`
- `min_confidence` → `WHERE confidence_score >= :min_confidence`
- **Expiry window (decided):** By default return signals where `expires_at IS NULL OR expires_at > NOW() - INTERVAL '24 hours'`. This means active signals AND signals that expired within the last 24 hours are always returned — matching professional terminal behavior (traders need same-day context). Signals older than 24 hours are excluded. No `include_expired` toggle on the frontend. Expired-but-recent rows are visually differentiated in the UI (see Phase 6.4).

The count query for pagination must apply the **same WHERE clauses** — otherwise page count is wrong.

#### 2.5 Fix Hardcoded Symbols in Signal Pipeline (D-08)

In `signal_pipeline.process_event_end_to_end`, replace `symbols = ["RELIANCE", "TCS"]` with a DB lookup of `AIEventClassification.affected_symbols` for the given `raw_event_id`, filtered through the event classification chain. Fall back to an empty list (skip signal generation) if no symbols are found.

---

### Phase 3 — WebSocket Stream Endpoint (D-01)
**Files:** `backend/app/api/v1/cai.py`, `backend/app/main.py`

**Goal:** Implement the `/api/v1/cai/stream` WebSocket endpoint that bridges all 4 Redis pub/sub channels to connected browser clients. This is the most architecturally significant piece.

#### 3.1 `ConnectionManager`

A module-level `ConnectionManager` class (within `cai.py`) maintains a `set[WebSocket]` of active connections. Thread-safe via asyncio (single-threaded event loop). Exposes:

- `connect(ws)` — add to set
- `disconnect(ws)` — remove from set
- `broadcast(message: str)` — iterate set, send to each; on `WebSocketDisconnect` or `RuntimeError`, silently remove the dead connection

No rooms/channels needed — all CAI clients receive all 4 channel types and filter client-side. Keeps the server simple.

#### 3.2 WebSocket Endpoint

```
@cai_ws_router.websocket("/stream")
async def cai_stream(websocket: WebSocket)
```

Registered as a separate `ws_router` on `cai.py`, included in `main.py` at prefix `/api/v1/cai` (same as current HTTP router). This is the established FastAPI pattern for mixing HTTP and WebSocket on the same prefix.

**Connection lifecycle:**

1. `await websocket.accept()`
2. Wait for first message (with 10s timeout) — must be `{"type": "auth", "token": "<jwt>"}`. Call `decode_token(token)` from `app.core.security`. On failure, send `{"type": "error", "code": "AUTH_FAILED"}` and `await websocket.close(code=4001)`.
3. On success, call `manager.connect(websocket)`. Send `{"type": "connected", "channels": ["cai:signals:all", "cai:regime:all", "cai:events:high_impact", "cai:models:drift_alerts"]}` — matches what `useCAIWebSocket.handleMessage` already parses.
4. Launch two concurrent asyncio tasks:
   - **`_redis_subscriber_task`**: Creates a dedicated `PubSub` object from the singleton Redis client. Subscribes to all 4 channels. On each message, calls `manager.broadcast(json.dumps({"channel": msg["channel"], "data": msg["data"]}))`. Runs until cancelled.
   - **`_client_reader_task`**: Reads inbound frames from the client. Handles `"ping"` frames with a `"pong"` response. No other client-to-server messages are expected. Runs until `WebSocketDisconnect`.
5. `asyncio.wait` on both tasks with `FIRST_COMPLETED`. When one exits (client disconnects or Redis error), cancel the other.
6. `finally`: `manager.disconnect(websocket)`, cancel remaining tasks, close pubsub.

**Heartbeat and token re-validation (decided):** The `_redis_subscriber_task` fires every 30 seconds using `asyncio.wait_for` with a 30s timeout on `pubsub.get_message`. On each heartbeat tick:
1. Send `{"type": "heartbeat"}` to `manager.broadcast` — clients filter this out already.
2. Check the `exp` claim of the `TokenPayload` captured at connect time. This is a nanosecond datetime comparison — no DB query, no crypto. If `exp < now`, send `{"type": "error", "code": "TOKEN_EXPIRED"}` and close with code 4001. The frontend's existing reconnect logic (`scheduleReconnect`) picks up the 4001, the auth context refreshes the access token, and `useCAIWebSocket` reconnects with a fresh token transparently.

This means a user who logs out in another tab — invalidating their token — will lose the WebSocket connection within at most 30 seconds. This is financial-grade security and matches the FAPI (Financial-grade API) security profile requirement for long-lived connections.

**Security:** JWT is validated server-side before the connection is admitted and re-validated on every heartbeat. Token is never logged. In production, add an IP-based rate limit on the WebSocket upgrade (1 connection per authenticated user per 5 seconds using the existing `limiter` infrastructure).

**Scale-out note:** Because `ConnectionManager` is in-process, a second backend instance wouldn't share it. The Redis pub/sub subscription is per-instance though, so each instance broadcasts to its own clients correctly. For multi-instance scale-out, promote `ConnectionManager` to a Redis-backed fanout (encode/broadcaster library). Noted as a future concern, not needed now.

#### 3.3 Register in `main.py`

Add `app.include_router(cai.ws_router, prefix="/api/v1/cai", tags=["Cortex AI WebSocket"])` alongside the existing HTTP router.

---

### Phase 4 — Signal Generation Scheduler (D-13)
**Files:** `backend/app/main.py`, new `backend/app/services/signal_scheduler.py`

**Goal:** Replace the manual-only signal generation with a production-grade automated scheduler using a hybrid symbol universe: a curated scheduled universe for predictable cycle times, and on-demand generation for watchlist symbols outside that universe.

#### 4.1 Symbol Universe (decided: Hybrid)

**Scheduled universe:** Nifty 50 + Nifty Next 50 = 100 symbols. Defined as a constant list in `settings` (not hardcoded in the service), so it can be updated without a deploy. These 100 symbols run on every 15-minute cycle during market hours. Cycle time is fully deterministic — 100 symbols processed via batched ML inference (see §4.4) completes in seconds, well within the 15-minute window.

**On-demand universe:** Any `trading_symbol` from `watchlist_items` that is not in the scheduled universe. When a user views a symbol not in the Nifty universe, signal generation is triggered once per session for that symbol and the result is cached in Redis for 15 minutes (`cai:signals:on-demand:{symbol}`). This covers mid-cap and small-cap watchlist stocks without making the scheduler non-deterministic.

This is the architecture used by institutional platforms (QuantConnect scheduled universe + dynamic universe selection). It gives predictable cycle times and full personal coverage.

#### 4.2 `SignalScheduler` Service

A new `backend/app/services/signal_scheduler.py` module with a `SignalScheduler` class. Follows the same lifecycle pattern as `MarketFeedService` (start/stop methods, asyncio background task).

**`start()`**: Launches an asyncio task that runs `_scheduler_loop`.

**`_scheduler_loop()`**:

```
while running:
    await _sleep_until_next_run()     # sleep to next 15-min boundary IST
    if not nse_calendar.get_session().is_open_now:
        continue                      # market closed, skip
    await _run_generation_cycle()
```

**`_sleep_until_next_run()`**: Computes the next 15-minute boundary in IST (09:15, 09:30, 09:45, …, 15:15) and sleeps until then. Uses `zoneinfo.ZoneInfo("Asia/Kolkata")` — already used by the scanner. Never sleeps more than 15 minutes, never drifts.

**`_run_generation_cycle()`**: Runs batched signal generation for all 100 scheduled symbols (see §4.4 for the batched inference architecture). Logs summary: total symbols, signals generated, errors, cycle duration. On any per-symbol error, logs and continues — never breaks the cycle.

**`generate_on_demand(symbol: str)`**: Public method called when a user views a watchlist symbol not in the scheduled universe. Checks Redis cache first (`cai:signals:on-demand:{symbol}` with 15-minute TTL). On cache miss, triggers a single-symbol signal generation using the same batched pipeline (batch size = 1). Returns immediately with a fire-and-forget task so the API endpoint is non-blocking.

**`stop()`**: Cancels the background task. Awaits it with a 5s timeout.

#### 4.3 Event-Driven Trigger

In the intelligence processing pipeline (`signal_pipeline.process_event_end_to_end`), after a high-impact event classification is committed (fix D-08 first), immediately call `scheduler.generate_on_demand(symbol)` for each symbol in `affected_symbols` if the event impact score is ≥ 0.7. This is a fire-and-forget `asyncio.create_task` call — it doesn't block the intelligence pipeline.

#### 4.4 Batched ML Inference Architecture (decided: Option C)

Running 100 sequential per-symbol ML inference calls on a 4GB GPU risks VRAM fragmentation and makes cycles take minutes. The correct architecture decouples feature loading (I/O bound) from inference (GPU bound):

**Phase A — Parallel feature loading:**
Launch all 100 symbol feature loads as concurrent asyncio tasks with a DB semaphore of 20 (I/O bound, not GPU bound — higher concurrency is safe). Collect all `(symbol, tabular_features, sequence_features, current_price, volatility)` tuples. Symbols that fail feature loading are logged and skipped.

**Phase B — Single batched inference:**
Stack all tabular feature vectors into one `(N, 47)` ndarray → one XGBoost batch call via `EnsemblePredictor.predict_batch`. Stack all sequence features into `(N, 60, 47)` → one ONNX GRU batch call. A new `predict_batch` method is added to `EnsemblePredictor` alongside the existing per-symbol `predict` (which remains for on-demand use). Batch results are mapped back to symbols by index.

**Phase C — Parallel signal assembly and persistence:**
Map batch inference outputs to per-symbol signal dicts. Run event signal gathering, fusion, persistence, and Redis publish concurrently per symbol with a write semaphore of 10.

For 100 symbols on an RTX 3050 (4GB VRAM, FP16): feature loading is parallel DB I/O (~1–2s), one XGBoost batch inference on `(100, 47)` is sub-millisecond via Treelite FIL, one GRU batch inference on `(100, 60, 47)` at FP16 fits well within 4GB VRAM. Total cycle time: seconds vs. minutes for sequential.

#### 4.5 Register in `lifespan`

In `main.py`:
```python
signal_scheduler = SignalScheduler(
    db_factory=AsyncSessionLocal,
    ml_predictor=app.state.ml_predictor,
    cache_service=get_cache_service(),
    pubsub=PubSubClient(get_redis()),
    scheduled_universe=settings.SIGNAL_SCHEDULED_UNIVERSE,  # Nifty 50 + Next 50
)
await signal_scheduler.start()
app.state.signal_scheduler = signal_scheduler
```

Stopped in the shutdown block: `await signal_scheduler.stop()`.

---

### Phase 5 — Technical Indicators Layer (D-04)
**Files:** `backend/app/ai/fusion/signal_assembler.py`

**Goal:** Implement `gather_technical_signals` using existing OHLCV data in `upstox_ohlcv`. Restore full 3-way fusion weights.

#### 5.1 `gather_technical_signals` Implementation

**Timeframe matching (decided: Option B+C):** The method signature is `gather_technical_signals(symbol, candle_timeframe)` — explicitly parameterized. The caller (`assemble_signal`) derives `candle_timeframe` from `time_horizon`: `swing` → `1D`, `intraday` → `1hour`, `positional` → `1D`. Today all signals are `swing` so `1D` candles are used. When intraday signals are added in the future, the caller changes, not this method. This is technically correct from day one — using daily candles to inform an intraday signal would be meaningless.

Query the last 60 candles for the symbol at `candle_timeframe` from `upstox_ohlcv` (ordered descending, limit 60). Compute two indicators entirely in-process using numpy — no additional library dependency:

**RSI-14:**
```
price_changes = np.diff(closes)
gains = np.where(price_changes > 0, price_changes, 0)
losses = np.where(price_changes < 0, -price_changes, 0)
avg_gain = np.mean(gains[-14:])
avg_loss = np.mean(losses[-14:])
rsi = 100 - (100 / (1 + avg_gain / avg_loss)) if avg_loss > 0 else 100
```

**EMA-20 / EMA-50 Crossover:**
Compute both EMAs using the standard recursive formula. Signal: EMA20 > EMA50 = bullish, EMA20 < EMA50 = bearish.

**Score Derivation:**

| Condition | Score |
|-----------|-------|
| RSI > 60 AND EMA20 > EMA50 | +100 (strong bullish) |
| RSI > 55 OR EMA20 > EMA50 | +50 (mild bullish) |
| RSI < 40 AND EMA20 < EMA50 | -100 (strong bearish) |
| RSI < 45 OR EMA20 < EMA50 | -50 (mild bearish) |
| Otherwise | 0 (neutral) |

Confidence: `abs(rsi - 50) / 50` — higher when RSI is more extreme.

**Fallback:** If fewer than 60 candles exist for a symbol, return `score=0, confidence=0` (same as current stub). Never crash, always degrade gracefully.

#### 5.2 Restore Full Weights

After Phase 5 ships, update `SignalAssembler.__init__` defaults to:
```
event_weight=0.35
ml_weight=0.40
technical_weight=0.25
```

Remove the Phase 2 `# TODO(phase-5)` comment.

---

### Phase 6 — Frontend Revamp (D-10, D-11, D-12)
**Files:** `frontend/src/components/ai/SignalsPanel.tsx`, `frontend/src/hooks/useSignalsRealtime.ts`, `frontend/src/app/cortex-ai/page.tsx`

**Goal:** Wire the frontend to the now-working WebSocket stream, fix the confidence filter bug, display the new fields (expires_at, target_price, stop_loss), and show real connection status.

#### 6.1 Fix `useSignalsRealtime` Confidence Filter (D-11)

Change `signal.confidence` to `signal.calibrated_confidence` in the cache injection filter. One-line fix, but must be done before wiring the panel.

#### 6.2 Switch `SignalsPanel` to `useSignalsRealtime` (D-10)

Replace `const { data, isLoading, refetch, isRefetching } = useSignals(filters)` with `const { data, isLoading, refetch, isRefetching, wsStatus } = useSignalsRealtime(filters)`. The return shape is identical plus `wsStatus`. Expose `wsStatus` as a prop or context value for the page to consume.

#### 6.3 Wire `ConnectionStatusIndicator` to Live WS State (D-12)

In `cortex-ai/page.tsx`:
1. Instantiate `useCAIWebSocket` at the page level (or consume `wsStatus` from `SignalsPanel` via a lifted callback prop).
2. Pass `status` to `ConnectionStatusIndicator`. The indicator's existing `ConnectionStatus` type already covers `connected`, `connecting`, `disconnected`, `reconnecting` — no changes needed there.

#### 6.4 Add New Fields to UI

**Signals table** (SignalsPanel): Add an `Expires` column after `Generated`. Display using `safeFormatDate(signal.expires_at, "MMM d, HH:mm")`. Three visual states:
- Normal: signal is active (expires in > 2 hours)
- Amber: signal expires within 2 hours — `text-amber-600` with a clock icon
- Red + muted row opacity: signal has already expired but is within the 24h window — renders with a red "EXPIRED" badge on the expiry cell and `opacity-60` on the row

No toggle needed. The 24h window is always on. Traders see same-day context without manual controls.

**Detail modal** (SignalDetailModal): Add `Target Price` and `Stop Loss` to the Signal Overview grid. Display as `₹{value.toFixed(2)}` — already the pattern for optional price fields. Only render the cells if the values are non-null.

---

## 5. Out of Scope (Documented, Not Deferred Indefinitely)

**Confidence Calibration (Platt Scaling / Isotonic Regression)**

Research confirms isotonic regression is superior to Platt scaling for financial ML when the calibration set exceeds 200 samples. This requires a calibration dataset of historical signal outcomes (predicted confidence vs. actual trade outcomes), which does not yet exist in the DB. The `calibrated_confidence` field is correctly named and positioned in the schema — when a calibration dataset is built from production trade outcomes, isotonic regression can be applied as a post-processing step in `EnsemblePredictor._assemble_prediction` before returning confidence. Tracked separately.

**Multi-Instance WebSocket Scale-Out**

The current `ConnectionManager` is in-process. For horizontal scaling (multiple backend instances behind a load balancer), the manager needs to use `encode/broadcaster` backed by Redis so every instance can fan out to all clients regardless of which instance they connected to. Not needed at current scale — addressed when horizontal pod autoscaling is configured.

---

## 6. File Change Summary

| File | Change Type | Phase |
|------|-------------|-------|
| `backend/alembic/versions/0013_trading_signals_revamp.py` | New | 1 |
| `backend/app/ai/fusion/models.py` | Modify | 1 |
| `backend/app/ai/fusion/signal_assembler.py` | Revamp | 2, 5 |
| `backend/app/api/v1/fusion.py` | Modify | 2 |
| `backend/app/ai/fusion/signal_pipeline.py` | Modify | 2 |
| `backend/app/api/v1/cai.py` | Revamp | 3 |
| `backend/app/main.py` | Modify | 3, 4 |
| `backend/app/services/signal_scheduler.py` | New | 4 |
| `backend/app/ml/inference/ensemble_predictor.py` | Modify | 4 |
| `backend/app/core/config.py` | Modify | 4 |
| `frontend/src/hooks/useSignalsRealtime.ts` | Modify | 6 |
| `frontend/src/components/ai/SignalsPanel.tsx` | Modify | 6 |
| `frontend/src/app/cortex-ai/page.tsx` | Modify | 6 |
| `frontend/src/components/ai/SignalDetailModal.tsx` | Modify | 6 |

**Total: 14 files, 2 new files, 1 new Alembic migration**

---

## 7. Dependencies Between Phases

```
Phase 1 (DB migration)
    └── Phase 2 (backend logic)    ← needs new columns in ORM model
            └── Phase 3 (WS)      ← needs correct Redis publish payload (D-07)
            └── Phase 4 (scheduler)  ← needs working assemble_signal (D-02, D-03)
                    └── Phase 5 (technical)  ← needs scheduler to call it
Phase 3 + Phase 6 must ship together ← frontend WS hook + backend WS endpoint
Phase 2 must ship before Phase 6    ← frontend filters need server-side support
```

Phases 5 and 6 can be developed in parallel after Phase 3 is done.

---

## 8. Architectural Decisions (Closed)

All five open questions have been resolved. Decisions are final and reflected throughout this plan.

---

### D1 — Symbol Universe: Hybrid (Scheduled + On-Demand)

**Decision:** Nifty 50 + Nifty Next 50 (100 symbols) run on a scheduled 15-minute cycle. Any watchlist symbol outside this universe is generated on-demand when first accessed in a session, cached for 15 minutes in Redis.

**Rationale:** Predictable, deterministic cycle times for the high-volume core universe. Full personal coverage for mid/small-cap watchlist stocks without making the scheduler non-deterministic. Mirrors QuantConnect's scheduled + dynamic universe pattern used in institutional platforms.

**Impact:** `SignalScheduler`, `settings.SIGNAL_SCHEDULED_UNIVERSE` config key, on-demand cache key `cai:signals:on-demand:{symbol}`.

---

### D2 — Expired Signal Display: Always Show Last 24 Hours

**Decision:** The API default window is `expires_at > NOW() - INTERVAL '24 hours'`. No frontend toggle. Expired-but-recent rows render with a red "EXPIRED" badge and 60% opacity.

**Rationale:** Professional trading terminals (Trading Technologies) always keep intraday history visible within the same session. A signal that expired at noon is still relevant context at 3 PM. Hiding signals the moment they expire is disorienting and reduces trust in the system.

**Impact:** `GET /fusion/signals` WHERE clause, `SignalsPanel` row styling.

---

### D3 — WebSocket JWT Re-validation: On Every 30s Heartbeat

**Decision:** On each 30-second heartbeat, the server checks the `exp` claim of the `TokenPayload` captured at connect time. If expired, sends `{"type": "error", "code": "TOKEN_EXPIRED"}` and closes with code 4001. Client reconnects with a refreshed token transparently.

**Rationale:** Financial-grade security (FAPI profile) requires that long-lived connections do not outlive token validity. A logout on another tab must terminate the WebSocket within ≤ 30 seconds. The `exp` check is a nanosecond datetime comparison — no performance cost.

**Impact:** `_redis_subscriber_task` heartbeat loop in `cai.py`.

---

### D4 — Technical Indicators Timeframe: Parameterized, Matching Signal Horizon

**Decision:** `gather_technical_signals(symbol, candle_timeframe)` is explicitly parameterized. Caller derives `candle_timeframe` from `time_horizon`: `swing`/`positional` → `1D`, `intraday` → `1hour`. Today all signals are `swing` so `1D` is used. No future refactor needed when intraday signals are added.

**Rationale:** Using daily candles to score an intraday signal is technically incorrect — the RSI and EMA would reflect multi-week trends, not intraday momentum. Multi-timeframe matching is the standard in every professional trading system. Building the correct abstraction now costs nothing.

**Impact:** `gather_technical_signals` signature, `assemble_signal` caller, `1hour` OHLCV data already available in DB.

---

### D5 — ML Inference: Batched (Decouple Feature Loading from Inference)

**Decision:** Feature loading (I/O bound) and ML inference (GPU bound) are decoupled into two phases. Phase A: all 100 symbol feature loads run concurrently (DB semaphore = 20). Phase B: one batched XGBoost call on `(N, 47)` and one batched GRU ONNX call on `(N, 60, 47)`. Phase C: signal assembly and DB writes run concurrently (write semaphore = 10). A `predict_batch` method is added to `EnsemblePredictor`.

**Rationale:** 100 sequential per-symbol GPU inferences on a 4GB VRAM card risks VRAM fragmentation and makes cycles take minutes. Batching XGBoost via Treelite FIL delivers sub-millisecond throughput on `(100, 47)`. GRU batch at FP16 on `(100, 60, 47)` fits well within 4GB. Total cycle time: seconds vs. minutes. This is NVIDIA's recommended production inference pattern for ensemble models.
