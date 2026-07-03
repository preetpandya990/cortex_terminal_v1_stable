# Demand-Driven AI Processing — Implementation Report

**Date:** 2026-07-02
**Project:** Cortex Merge AI-ML
**Component:** Tier-2 Gemini Dispatch Control (Sentiment / Forecast / Classification)
**Status:** ✅ COMPLETE — code-complete, tested, migration applied, live-verified end-to-end. **UNCOMMITTED.**

---

## Executive Summary

Three background paths — sentiment batching (`NLPEngine`), event classification (`EventClassifier`), and news-forecast batching (`forecast_batch_worker`) — previously auto-fired Gemini calls continuously and unconditionally. When RSS floods in at market open (~09:30 UTC), all three would fire heavily and exhaust the 100 RPD Gemini quota before the two HIGH-priority, user-facing callers (trade explanations, watchlist/instrument context) got a turn.

This work converts the 3 Tier-2 paths to **demand-driven**: they keep accumulating pending work in queues, but only fire Gemini when an admin explicitly dispatches from the Worker Control Panel, with a scheduled daily safety net (09:00 IST) as a fallback. All 3 new flags default `True` (today's auto-behavior) — the change ships as a pure no-op; flipping any flag to `False` is a deliberate, separate operator action.

Source design doc: `DEMAND_DRIVEN_AI_PROCESSING_IMPLEMENTATION_PLAN.md` (repo root).

---

## What Was Built

### 1. Database Migration
- `alembic/versions/0049_event_classification_unique_nlp_result.py` — adds `UNIQUE (nlp_result_id)` to `ai_event_classifications`, with a defensive dedup step first (safe no-op on clean data).
- Applied to the dev DB: **0 rows deleted** (16,081 existing rows, zero duplicates). Upgrade/downgrade both verified.
- `AIEventClassification.nlp_result_id` updated to `unique=True` in `app/ai/fusion/models.py`.

### 2. Config + Metrics
- New settings in `app/core/config.py`: `SENTIMENT_AUTO_FLUSH`, `FORECAST_AUTO_DISPATCH`, `EVENT_CLASSIFIER_AUTO_DISPATCH` (all default `True`), plus `AI_SAFETY_NET_RUN_TIME_IST` (`"09:00"`) and 3 per-category thresholds.
- New Prometheus metrics in `app/core/metrics.py`: `ai_processing_dispatch_total` (category/trigger_source/outcome), `ai_processing_pending_gauge`, plus safety-net run/duration/last-run metrics.

### 3. Queue + Flush Logic (3 producers)
- **`app/ai/intelligence/nlp_engine.py`** — `_flush_batch()` now returns `calls_made: int`; added `flush_pending_sentiment()` / `pending_sentiment_count()`; auto-trigger gated on `SENTIMENT_AUTO_FLUSH`. Queue is in-process memory — a worker restart before dispatch silently drops pending items (documented, accepted).
- **`app/ai/fusion/forecast_batch_worker.py`** — `_flush_batch()` now returns the outcome dict; added `flush_pending_forecasts()` / `pending_forecast_count()`; stops early and raises on `budget_throttled`/`error` so a quota outage fails loudly instead of silently losing the queue. Redis-backed, durable across restarts.
- **`app/ai/intelligence/event_classifier.py`** — extracted `_gemini_classify()` (shared by inline + deferred paths); added `_persist_classification()` (shared insert/update); new Redis pending queue + `flush_pending_classifications()` / `pending_classification_count()`. Accepted limitation: a later-upgraded classification does not retroactively regenerate `AITradingSignal` rows for that event.

### 4. Safety-Net Scheduler
- **`app/workers/ai_processing_safety_net.py`** (new) — mirrors `WatchlistContextScheduler` exactly (same `__slots__`/constructor, reuses its `_parse_run_times`/`_seconds_until_next_run` helpers). No market-hours guard (quota resets at midnight, independent of NSE hours). Checks each of the 3 pending counts against its threshold and dispatches only categories over threshold — one category's failure never blocks the other two.
- Registered in `app/workers/registry.py` as the 18th worker task (`"ai_processing_safety_net"`). The existing generic `POST /tasks/{name}/trigger` endpoint gives the admin a "force safety net now" action with zero new endpoint code.

### 5. WorkerClient Extension
- **`app/core/worker_client.py`** — added `WorkerDispatchError`, `dispatch_ai_processing(category)` (deliberate exception to the client's fail-open contract — raises instead of returning `None`, since an admin explicitly clicked Dispatch and needs to know if it failed), and `get_ai_processing_status()` (normal fail-open read).

### 6. New Routers
- **`app/api/worker_ai_processing.py`** (worker sidecar, :8001) — `GET /ai-processing/status`, 3 `POST .../dispatch` routes. Mounted in `app/worker_app.py`.
- **`app/api/v1/admin_ai_processing.py`** (main API, :8000) — thin proxy to the sidecar, rate-limited (`10-30/minute`), admin-auth-gated. No `from __future__ import annotations` (breaks `@limiter.limit` + `Annotated` dependency resolution — same gotcha documented in `admin_strategies.py`). Mounted in `app/main.py` at `/api/v1/admin/ai-processing`.

### 7. Frontend
- New `<AIProcessingQueueCard />` added as a sibling section to `<TasksGrid />` on `/admin/workers`, mirroring `TaskCard.tsx`'s patterns exactly (toast integration, `createPortal` tooltip, indeterminate progress bar, emerald/amber/rose/slate color system).
- Files: `frontend/src/types/ai_processing.ts`, `frontend/src/hooks/useAIProcessingQueue.ts`, `frontend/src/components/admin/AIProcessingQueueCard.tsx`; wired into `frontend/src/app/admin/workers/page.tsx`.

---

## Verification

| Check | Result |
|---|---|
| `alembic upgrade head` | Applied cleanly, 0 rows deleted, constraint present. Downgrade tested and confirmed reversible, then re-upgraded to head. |
| Backend test suite (7 new/extended files) | **68/71 passed.** 3 failures are pre-existing bugs (2 stale heuristic-prefilter test assumptions in `test_event_classifier.py`, 1 `aiobreaker` version-mismatch in `test_worker_client.py`) — confirmed via isolated `git stash` comparison against unmodified code, **not** introduced by this work. |
| TypeScript / ESLint (frontend) | Zero new errors in any touched file. One pre-existing lint rule violation (`react-hooks/set-state-in-effect`) reproduced identically in the pattern's source (`TaskCard.tsx`) — not a regression. |
| Live boot (API + worker sidecar, bare-metal uvicorn) | Both boot cleanly. Worker registers **18 tasks** including `ai_processing_safety_net` (schedule logged as `09:00 IST`). |
| Live endpoint checks (`curl`) | `GET /ai-processing/status` (sidecar) → 200 with real pending counts. `GET /api/v1/admin/ai-processing/status` (main API) → 401 without auth (correctly gated). |
| Live browser flow (Playwright + system Chrome, no browser download) | Logged in as admin, navigated to `/admin/workers`, confirmed the new card renders with all 3 categories. Clicked **Dispatch Now** on Sentiment Batching → toast: **"Dispatched — Sentiment Batching: 1 drained, 1 Gemini call(s)"**, pending count 1→0. Backend logs confirm a real `llm.generate_structured` Gemini call fired (1581ms) through the full stack: browser → main API → worker sidecar → `NLPEngine`. Zero console errors beyond an expected pre-login 401. |

---

## Operator Actions / Notes

- **Nothing is committed to git yet.** All 16 files (12 backend, 4 frontend) are uncommitted working-tree changes.
- Both the API (port 8000) and worker sidecar (port 8001) were restarted bare-metal (`.venv/bin/uvicorn`, not docker-compose) during verification and were **left running intentionally** so Gemini call volume can be observed from a clean baseline going forward.
- Flipping any of `SENTIMENT_AUTO_FLUSH` / `FORECAST_AUTO_DISPATCH` / `EVENT_CLASSIFIER_AUTO_DISPATCH` to `False` in any environment, and running migration 0049 in staging/production, remain deliberate operator actions outside this implementation pass.
