# Task List — Trade Suggestions & Strategy Compliance

## P0 — Blocking (Zero Suggestions Generated)

1. ✅ **Initialize `feature_loader` in `worker_lifespan()`**
   Fixed: ensemble metadata stored in `ml_components`; fresh `FeatureLoader` injected
   per-cycle inside session context, cleared after session exits.

---

## P1 — Critical (Pipeline Structural Gaps)

2. ✅ **Fix Redis publishing payload**
   Fixed: `_compute_consensus()` now publishes full JSON suggestion payload so WebSocket
   clients can render immediately without a round-trip REST fetch.

3. ✅ **Move `redis_listener_task` to app startup**
   Fixed: `suggestions_redis_listener()` is a zero-arg persistent coroutine started
   in `main.py` lifespan; removed per-connection spawn from WebSocket endpoint.

4. ✅ **Replace Pathway 2 scanner stub**
   Fixed: `_resolve_scanner_signal_for_symbol()` reads real scanner data from
   `scanner:results:v2:1d` Redis cache, matches by `trading_symbol`; fallback
   with capped confidence on cache miss.

5. ✅ **Wire strategy compliance into trade suggestions**
   Implemented: junction-table approach.
   - Migration 0024: `regime_type`/`time_horizon` added to `trade_suggestions`;
     `user_suggestion_compliance` table created with (suggestion_id, user_id,
     strategy_id, passed, pipeline_result, evaluated_at).
   - `SuggestionComplianceService.evaluate_and_persist()` — single JOIN loads all
     subscribed users + strategies; runs `StrategyFilterPipeline`; bulk-inserts
     compliance rows with ON CONFLICT DO NOTHING.
   - Correlation engine: derives `regime_type` and `time_horizon` at generation
     time; calls compliance service inline after suggestion commit.
   - API `list_suggestions`: hard_gate + subscriptions → subquery filter on
     passed=TRUE; soft_filter → loads compliance annotations for badge display.
   - Frontend: `StrategyBadge` component on suggestion cards (green shield =
     strategy match; amber shield = blocked with gate name tooltip).

---

## P2 — High (Reliability)

6. ✅ **Add `feature_loader` initialization verification at worker startup**
   Fixed: `_REQUIRED_ML_KEYS` check after ML init logs an explicit `WARNING` listing all missing keys; on success logs `ML component verification passed` with key count.

7. ✅ **`trade_suggestions` missing from `api/v1/__init__.py`**
   Fixed: added to imports and `__all__`.

8. ✅ **Verify `affected_symbols` population**
   Fixed: added `normalize_and_validate_symbols()` to `event_classifier.py` — two-strategy matching (exact `trading_symbol` + name CONTAINS) against `instrument_master` restricted to NSE EQ; wired into `classify()` before persisting `AIEventClassification`.

---

## P3 — Medium (Performance / Reliability)

9. ✅ **Add end-to-end latency tracking**
   Fixed: structured pipeline stage logs added to `on_scanner_anomaly()` — `trigger`, `signal_gather` (with scanner_ms/ai_ms/ml_ms), `suggestion_committed` (total_pipeline_ms), `redis_published` (publish_ms); `ws_broadcast` stage with `e2e_ms` from `generated_at` added to WebSocket handler in `trade_suggestions.py`.

10. ✅ **WebSocket reconnection logic verification**
    Fixed: full rewrite of `useWebSocket.ts` — exponential backoff with ±25% jitter; token re-sent in-band on every reconnect; message deduplication sliding window (64 IDs); proactive client `ping` (was incorrectly `pong`); server `ping` → client `pong` response; `reconnect()` public API to recover after max-attempts; `NEXT_PUBLIC_WS_DEBUG` gating for production log suppression.

11. ✅ **Cache ML predictions for 30s**
    Fixed: `gather_ml_signals()` checks Redis at `cortex:ml:signal:{symbol}:{timeframe}` before feature loading + inference; on miss runs full pipeline and writes result with `setex(30)`; `_ml_cache` wired in via new `redis` param on `SignalAssembler.__init__`; `correlation_loop` in `worker.py` passes `redis_client._redis`; cache read/write failures are non-fatal debug-logged.
