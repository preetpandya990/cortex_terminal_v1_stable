# Demand-Driven AI Processing — Implementation Plan (verified against current code)

## Context

The system currently auto-dispatches Gemini calls from 3 background paths — sentiment
batching (`NLPEngine`), event classification (`EventClassifier`), and news forecast
batching (`forecast_batch_worker`) — continuously and automatically. When RSS floods in
at market open (~09:30 UTC) all three fire heavily and exhaust the 100 RPD Gemini quota
before the trading day starts, starving the two HIGH-priority, user-facing callers (trade
explanations, watchlist/instrument context).

This plan converts the 3 Tier-2 paths to demand-driven: they keep accumulating pending
work in queues, but only fire Gemini when an admin explicitly dispatches from the Worker
Control Panel, with a scheduled daily safety net (09:00 IST) as a fallback. All 3 flags
default `True` (today's auto-behavior) — flipping any to `False` is a deliberate, separate
post-deploy action.

I re-verified every file this plan touches against the actual current codebase (not just
the original proposal doc) via 3 parallel Explore passes + direct reads. Key corrections
vs. the original doc, folded into this plan:

- **`forecast_batch_worker.py` already exists** (untracked in git) with `forecast_batch_loop()`
  and `_flush_batch()` fully implemented and registered in `registry.py` as `"forecast_batch"`.
  Only the dispatch/count/gating additions are new.
- **`ai_event_classifications.nlp_result_id` has no unique constraint** — confirmed via
  direct model read (`backend/app/ai/fusion/models.py:93`, plain indexed column). The
  "UPDATE by nlp_result_id" design requires a migration (user confirmed: add a UNIQUE
  constraint with a defensive dedup step first).
- **`event_classifier.py`'s `get_ollama_client()` is a legacy-named factory that actually
  returns the Gemini-backed `CortexIntelligenceClient`** (confirmed via `llm_client.py`
  docstring/imports) — naming debt, not a functional issue. Not renaming it (out of scope,
  avoid unrelated churn) but noting it so it isn't mistaken for a second LLM provider.
- **No rate limiting exists on `admin_worker.py` today**, but the `@limiter.limit(...)`
  convention is established elsewhere (`admin_strategies.py`, `hawk_eye.py`,
  `trade_suggestions.py`) via `from app.core.limiter import limiter`. Critical gotcha
  confirmed from `admin_strategies.py`'s own comment: **do not add
  `from __future__ import annotations`** to the new admin router — it breaks
  `@limiter.limit` because it needs to resolve `Annotated` deps (`AdminUserID`) eagerly.
- **Frontend convention is snake_case throughout** (not snake_case→camelCase as the
  original doc assumed) — `TaskDetail` etc. keep backend field names verbatim. New types
  follow the same convention.

---

## 1. Database migration — `ai_event_classifications.nlp_result_id` uniqueness

New file `backend/alembic/versions/0049_event_classification_unique_nlp_result.py`:
- Defensive dedup first (safe on repeated runs, safe on already-clean data): for any
  `nlp_result_id` with >1 row, delete all but the most recent (`created_at DESC`, tie-break
  `id DESC`) via a `DELETE ... WHERE id NOT IN (SELECT DISTINCT ON (nlp_result_id) id ...)`
  raw SQL step.
- Add `UNIQUE` constraint on `nlp_result_id` (`op.create_unique_constraint`).
- `downgrade()` drops the constraint (dedup is not reversible, documented in the migration
  docstring — acceptable, matches other one-way-dedup migrations in this repo, e.g. 0047).
- Update `AIEventClassification.nlp_result_id` in `backend/app/ai/fusion/models.py:93` to
  `unique=True` to keep the ORM model in sync with the DB constraint.

I will run `alembic upgrade head` locally/dev as part of this work. Staging/prod migration
run is an operator action during rollout (per §8 sequencing).

## 2. Backend — worker sidecar queue + flush logic

**`backend/app/ai/intelligence/nlp_engine.py`**
- Change `_flush_batch()` (line 601) to return `calls_made: int` — `0` if `active` ends up
  empty (nothing was queued), `1` otherwise (exactly one Gemini call is attempted per
  invocation, whether the single-item or multi-item branch runs — confirmed from current
  code, both branches make exactly one `generate_structured`/`_call_batch_llm` call).
- Add `NLPEngine.flush_pending_sentiment() -> dict[str, int]` (classmethod): loops
  `calls_made += await cls._flush_batch()` while `not cls._queue.empty()`, tracks
  `dispatched` as the queue size observed at entry; returns `{"dispatched": N, "calls_made": M}`.
- Add `NLPEngine.pending_sentiment_count() -> int` → `cls._queue.qsize()`.
- Gate the existing triggers: in `_flusher_loop()` (line 570), skip entering the drain loop
  when `not settings.SENTIMENT_AUTO_FLUSH` (still respond to `aclose()`'s own drain, which
  is unconditional and unaffected). In `_analyze_with_audit_meta()` (line 550), only call
  `cls._flush_trigger.set()` on the size threshold when `settings.SENTIMENT_AUTO_FLUSH` is
  `True` — when `False`, callers still enqueue and await their future, but nothing wakes the
  flusher until an explicit dispatch call drains the queue directly (bypassing the trigger).
- Document in a code comment (non-obvious): this queue is in-process `asyncio.Queue` memory
  — a worker restart before dispatch silently drops pending items via `aclose()`'s neutral-
  fallback drain. Also note: with the flag off, `analyze_sentiment()` callers (i.e.
  `event_processing_loop`) block until an explicit dispatch resolves their future — a large
  backlog stalls event-processing throughput, not just sentiment enrichment.

**`backend/app/ai/fusion/forecast_batch_worker.py`**
- Change `_flush_batch()` (line 171) to return the outcome dict it already computes
  internally (`{"outcome": str, "valid_count": int, "total": int}`) instead of `None`, so
  callers can detect `budget_throttled`/`error` and stop draining early.
- Add `flush_pending_forecasts(redis, session_factory) -> dict[str, int]`: loop —
  `LPOP COUNT NEWS_FORECAST_BATCH_SIZE`, group into one `batch`, call `_flush_batch`, tally
  `dispatched += len(batch)` and `calls_made += 1`; **stop looping** (don't keep draining)
  if the returned outcome is `budget_throttled` or `error`, and propagate that as a raised
  `RuntimeError` with a clear message (caught one level up by the sidecar router → 502) —
  this satisfies "must not silently swallow errors." Continue looping while `LLEN > 0`.
- Add `pending_forecast_count(redis) -> int` → `redis.llen(_QUEUE_KEY)`.
- Gate `forecast_batch_loop()` (line 96): when `not settings.FORECAST_AUTO_DISPATCH`, skip
  the `LPOP` call entirely — just poll `LLEN` for the `news_forecast_queue_depth` gauge,
  then sleep (existing accumulation-into-a-lost-local-list risk avoided by never draining).

**`backend/app/ai/intelligence/event_classifier.py`**
- New Redis List key `cortex:event:classifier:pending` (module constant
  `_PENDING_QUEUE_KEY`).
- Extract the existing Gemini-call block (lines 584-647, the "3. Gemini call" section
  inside `_classify_with_ollama`) into `async def _gemini_classify(self, content, entities)
  -> dict[str, Any]` — identical logic, just pulled into its own method so both the inline
  path and the new flush path share it with zero duplicated prompt logic.
- In `_classify_with_ollama`, when `event_type == "general"` (line 549) and
  `not settings.EVENT_CLASSIFIER_AUTO_DISPATCH`: skip calling `_gemini_classify`, instead
  `LPUSH` a JSON payload `{"content_hash": ..., "content": content[:1500], "entities":
  entities, "nlp_result_id": nlp_result_id, "enqueued_at": <ISO8601>}` onto
  `_PENDING_QUEUE_KEY`, and return the heuristic/rule-based result immediately (unchanged
  shape) — `classify()`'s synchronous flow in `event_processor.py` is never blocked.
- Thread `nlp_result_id` down into `_classify_with_ollama(self, content, entities,
  nlp_result_id)` (currently not passed at line 503) — `classify()` already has it in scope
  at the call site (line 363).
- Refactor the tail of `classify()` (lines 420-433: build `AIEventClassification`, `db.add`,
  `commit`, `refresh`) into `async def _persist_classification(self, db, nlp_result_id,
  classification_result, validated_symbols, fast_hl, slow_hl, *, existing:
  AIEventClassification | None = None) -> AIEventClassification`: if `existing` is `None`,
  builds+adds+commits a new row (today's behavior, called from `classify()`); if provided,
  updates its fields in place and commits (called from the flush path). This reuses the
  exact symbol-merge/decay math for both insert and update, and is the concrete mechanism
  behind "UPDATE the existing row."
- Add `async def flush_pending_classifications(self, db_factory, redis) -> dict[str, int]`:
  loop popping one item at a time from `_PENDING_QUEUE_KEY` (`LPOP` without count — bounds
  blast radius per item so a quota failure mid-drain loses at most the one in-flight item,
  not a whole popped batch); for each: re-run the full three-source symbol merge (content-
  level extraction + `_gemini_classify` LLM symbols, mirroring `classify()`'s merge logic),
  call `_gemini_classify(content, entities)`, look up the existing row by `nlp_result_id`
  (`SELECT ... WHERE nlp_result_id = :id`, guaranteed unique post-migration), and call
  `_persist_classification(..., existing=row)`. Also writes the same
  `cortex:event_class:{content_hash}` cache key as the inline path. On
  `GeminiQuotaExhausted`/`GeminiBudgetThrottled`/other exception: stop popping further items
  and raise, same fail-loud contract as forecast. Returns `{"dispatched": N, "calls_made":
  N}` (1:1 — no batching for classification, unlike sentiment/forecast).
- Add `pending_classification_count(redis) -> int` → `redis.llen(_PENDING_QUEUE_KEY)`.
- **Documented, accepted limitation** (code comment): an upgraded classification arriving
  later does not retroactively regenerate `AITradingSignal` rows for that event — the
  synchronous `event_processor.py` flow already ran on the heuristic result.

## 3. Backend — safety-net scheduler

New file `backend/app/workers/ai_processing_safety_net.py`, mirroring
`watchlist_context_scheduler.py` exactly (imports its `_parse_run_times` and
`_seconds_until_next_run` rather than duplicating them):
- `class AIProcessingSafetyNet` — same `__slots__`/constructor shape
  (`session_factory, redis, shutdown, pause, trigger`).
- `run()`: same while-loop/pause-checkpoint/`trigger.wait_or_timeout()` skeleton, but
  **no market-hours guard** — instead, each scheduled or triggered wake calls `_run_batch()`
  which independently checks each of the 3 pending counts (direct in-process calls: `NLPEngine.pending_sentiment_count()`, `pending_forecast_count(redis)`,
  `EventClassifier.pending_classification_count` — no HTTP hop, this runs inside the
  `worker` process) against `AI_SAFETY_NET_SENTIMENT_THRESHOLD` /
  `_EVENTS_THRESHOLD` / `_FORECAST_THRESHOLD`, and dispatches only the categories over
  threshold, independently (one failing category must not block the other two — same
  per-category isolation as the admin "Dispatch All").
- Single run time from `AI_SAFETY_NET_RUN_TIME_IST` (default `"09:00"`) — reuse
  `_parse_run_times([settings.AI_SAFETY_NET_RUN_TIME_IST])`.
- Register in `backend/app/workers/registry.py`: add `"ai_processing_safety_net"` to
  `TASK_NAMES`, instantiate once (same pattern as `watchlist_scheduler_instance`, lines
  155-163), add the `"ai_processing_safety_net": lambda: ai_processing_safety_net_instance.run()`
  entry. The existing generic `POST /tasks/{name}/trigger` endpoint
  (`worker_control.py:222`) automatically gives the admin a "force safety net now" action —
  no new endpoint needed for that.

## 4. Backend — `WorkerClient` extension (fail-closed, by design)

`backend/app/core/worker_client.py` — all existing methods are intentionally fail-open
(§ module docstring). Add, with a note in the docstring explaining this is a deliberate
exception:
- `class WorkerDispatchError(Exception)`.
- `async def dispatch_ai_processing(self, category: str) -> dict[str, Any]`: bypasses the
  shared fail-open `_request()` helper — makes the HTTP call directly through the same
  retry+breaker stack, but on `CircuitBreakerError`/`httpx.HTTPError`/non-2xx, raises
  `WorkerDispatchError(...)` with a clear message instead of returning `None`.
- `async def get_ai_processing_status(self) -> dict[str, Any] | None`: normal fail-open
  read via `self._request("GET", "/ai-processing/status")` — same rationale as
  `get_health`/`get_tasks`.

## 5. Backend — new worker-sidecar router

New file `backend/app/api/worker_ai_processing.py`, mounted in `worker_app.py` (line 192,
right after `app.include_router(control_router)`), same `InternalAuth` pattern imported
from `worker_control.py` (`from app.api.worker_control import InternalAuth`):
- `GET /ai-processing/status` → `{"sentiment": {"pending": N, "auto_flush": bool},
  "forecast": {...}, "classification": {...}}` using the 3 `pending_*_count()` functions +
  the 3 config flags.
- `POST /ai-processing/sentiment/dispatch` → `NLPEngine.flush_pending_sentiment()`.
- `POST /ai-processing/forecast/dispatch` → `flush_pending_forecasts(redis, session_factory)`,
  catching the `RuntimeError` raised on budget/error and returning HTTP 502
  `{"detail": "dispatch_failed", "reason": ...}`.
- `POST /ai-processing/classification/dispatch` → same pattern via
  `EventClassifier(use_llm=True).flush_pending_classifications(...)`.
- All three dispatch endpoints wrap their call in try/except and return 502 on failure —
  never silently swallow, since `WorkerClient.dispatch_ai_processing` depends on this to
  distinguish real failure from "0 pending."

## 6. Backend — main-API admin router

New file `backend/app/api/v1/admin_ai_processing.py`, structured like `admin_worker.py`
(`_client`/`_proxy`/`_WORKER_UNAVAILABLE` pattern reused) **but do NOT add
`from __future__ import annotations`** (breaks `@limiter.limit` + `Annotated` resolution —
confirmed gotcha from `admin_strategies.py`'s own docstring warning):
- Pydantic response models `CategoryStatus`, `AIProcessingStatusResponse`, `DispatchResult`.
- `GET /status` → proxies `WorkerClient.get_ai_processing_status()` (fail-open 503 via
  `_proxy`).
- `POST /sentiment/dispatch`, `/forecast/dispatch`, `/classification/dispatch` → call
  `WorkerClient.dispatch_ai_processing(category)`; catch `WorkerDispatchError` →
  `raise HTTPException(502, detail=str(exc))`.
- Each endpoint decorated `@limiter.limit("10/minute")` (needs `request: Request` as first
  param, per the established convention) and guarded by `AdminUserID`.
- **No `/dispatch-all` backend endpoint** — "Dispatch All" is a frontend-side
  `Promise.allSettled` over the 3 mutations (matches the independent-per-category-failure
  requirement without new backend transaction semantics).

Register in `backend/app/main.py`:
- Add `admin_ai_processing` to the import line (line 22-23).
- `app.include_router(admin_ai_processing.router, prefix=f"{settings.API_V1_PREFIX}/admin/ai-processing", tags=["Admin — AI Processing Queue"])` immediately after line 389.

## 7. Config — `backend/app/core/config.py` (insert after line 454, following the exact
comment-block style of the adjacent `SENTIMENT_BATCH_*` / `NEWS_FORECAST_BATCH_*` settings)

```python
SENTIMENT_AUTO_FLUSH: bool = Field(default=True, description="...")
FORECAST_AUTO_DISPATCH: bool = Field(default=True, description="...")
EVENT_CLASSIFIER_AUTO_DISPATCH: bool = Field(default=True, description="...")

AI_SAFETY_NET_RUN_TIME_IST: str = Field(default="09:00", description="...")
AI_SAFETY_NET_SENTIMENT_THRESHOLD: int = Field(default=100, ge=1, description="...")
AI_SAFETY_NET_EVENTS_THRESHOLD: int = Field(default=50, ge=1, description="...")
AI_SAFETY_NET_FORECAST_THRESHOLD: int = Field(default=20, ge=1, description="...")
```
All 3 `*_AUTO_*` flags default `True` — ship as a pure no-op; flipping to `False` is a
deliberate, separate post-deploy action (§9).

## 8. Metrics — `backend/app/core/metrics.py` (insert after line 474, in a new
`# ── AI Processing Queue Control Metrics ──` section matching the adjacent Forecast Batch
Worker Metrics block style)

```python
ai_processing_dispatch_total = Counter(
    "ai_processing_dispatch_total", "Dispatch calls by category/trigger/outcome",
    ["category", "trigger_source", "outcome"],  # trigger_source: manual|scheduled
)
ai_processing_pending_gauge = Gauge(
    "ai_processing_pending", "Pending queue depth per category", ["category"],
)
```
Emitted from the 3 `flush_pending_*` functions (outcome: `success`/`error`) and from the
safety net (`trigger_source="scheduled"`) vs. the sidecar router (`trigger_source="manual"`).

## 9. Frontend — extend the existing Worker Control Panel

Addition to `frontend/src/app/admin/workers/page.tsx` (verified structure: header →
`<WorkerHealthBanner>` → `<TasksGrid>`, all in a `space-y-6` column) — add
`<AIProcessingQueueCard />` as a new sibling section after `<TasksGrid>`.

- **`frontend/src/types/ai_processing.ts`** (new) — `CategoryStatus { pending: number;
  auto_flush: boolean }`, `AIProcessingStatusResponse { sentiment: CategoryStatus;
  forecast: CategoryStatus; classification: CategoryStatus }`, `DispatchResult {
  dispatched: number; calls_made: number }` — plain snake_case fields, matching the verified
  convention in `worker_control.ts` (no camelCase transformation).
- **`frontend/src/hooks/useWorkerControl.ts`** — export `extractWorkerError` is already
  exported (line 39-44); import and reuse it directly, do not duplicate.
- **`frontend/src/hooks/useAIProcessingQueue.ts`** (new, mirrors `useWorkerControl.ts`):
  `aiProcessingKeys = { all: ['ai_processing'], status: ['ai_processing', 'status'] }`;
  `useAIProcessingStatus()` — `refetchInterval: 15_000`, `placeholderData: keepPreviousData`,
  `GET /admin/ai-processing/status`; `useDispatchCategory()` mutation — `POST
  /admin/ai-processing/{category}/dispatch`, `onSettled` invalidates `aiProcessingKeys.status`
  (matching the existing 4-mutations pattern at lines 84/95/106/117); `useDispatchAll()` —
  `Promise.allSettled` over the 3 dispatch calls, returning a per-category
  `{success: boolean, result?: DispatchResult, error?: string}` map.
- **`frontend/src/components/admin/AIProcessingQueueCard.tsx`** (new, mirrors
  `TaskCard.tsx`): 3 rows (sentiment/forecast/classification) each with pending count +
  Dispatch button + the exact indeterminate progress bar class
  `animate-[progress-slide_1.5s_ease-in-out_infinite]` (keyframe already defined in
  `globals.css:138-141`, reused verbatim, no new CSS needed) while in flight; a top-level
  "Dispatch All" button (disabled while any row in flight); toast integration via
  `useToast()` (same `success(title, desc)` / `error(title, extractWorkerError(err))`
  pattern as `TaskCard.tsx:229/272-290`); same color system (emerald=just succeeded,
  amber=accumulating, rose=error, slate=neutral); tooltip via the existing
  `createPortal`-based pattern (`TaskCard.tsx:121-173`) explaining auto-dispatch is off by
  design and naming the scheduled safety-net time — **and explicitly calling out that the
  sentiment queue is non-durable** (worker restart loses pending items), since that's the
  one category with a real operational gotcha the admin needs to see before relying on it.
- Add `<AIProcessingQueueCard />` to `page.tsx` alongside `<TasksGrid />`.

## 10. Testing

Follow `backend/tests/` layout exactly:
- `backend/tests/ai/intelligence/test_nlp_engine.py` — `flush_pending_sentiment()` drains
  fully with correct `dispatched`/`calls_made`; `SENTIMENT_AUTO_FLUSH=False` prevents the
  flusher from auto-triggering while items still accumulate and are servable via explicit
  dispatch.
- `backend/tests/ai/fusion/test_forecast_batch_worker.py` — `flush_pending_forecasts()`
  against a fake Redis list, including the early-stop-on-budget-throttled path;
  `FORECAST_AUTO_DISPATCH=False` stops the `LPOP` in the loop.
- `backend/tests/ai/intelligence/test_event_classifier.py` (extend) — queue path fires when
  flag off + heuristic="general"; `flush_pending_classifications()` updates the correct row
  by `nlp_result_id` (no duplicate insert — verifiable now that the constraint exists);
  early-stop on quota exhaustion leaves remaining queue items untouched.
- `backend/tests/workers/test_ai_processing_safety_net.py` (new) — per-category threshold
  logic (over/under → dispatched/skipped independently); single-time scheduling via
  `_seconds_until_next_run`.
- `backend/tests/core/test_worker_client.py` (extend) — `dispatch_ai_processing()` raises
  `WorkerDispatchError` (never `None`) on timeout/circuit-open/non-2xx.
- `backend/tests/api/test_admin_ai_processing.py` (new) — dispatch endpoints propagate
  `WorkerDispatchError` as HTTP 502 with a real detail message; `AdminUserID` auth enforced;
  rate limit applied.
- `backend/tests/api/test_worker_ai_processing.py` (new) — `InternalAuth` enforced on all 4
  new worker-sidecar routes.
- `backend/alembic` — verify migration 0049 upgrades and downgrades cleanly against a copy
  of the current dev DB; verify it's a no-op (no rows deleted) on data with no duplicates.

## Verification (end-to-end, after implementation)

1. `alembic upgrade head` locally — confirm 0049 applies cleanly, zero duplicate rows
   deleted (expected, since only one call site ever writes a given `nlp_result_id`).
2. `pytest backend/tests/ai/intelligence/test_nlp_engine.py backend/tests/ai/fusion/test_forecast_batch_worker.py backend/tests/ai/intelligence/test_event_classifier.py backend/tests/workers/test_ai_processing_safety_net.py backend/tests/core/test_worker_client.py backend/tests/api/test_admin_ai_processing.py backend/tests/api/test_worker_ai_processing.py -v`
3. Start the stack (`docker-compose up`), confirm both `api` and `worker` boot cleanly with
   the new config flags (all default `True`) — zero behavior change: `GET
   /api/v1/admin/ai-processing/status` returns `auto_flush: true` for all 3, `pending≈0`.
4. In the browser, open `/admin/workers`, confirm the new `AIProcessingQueueCard` renders,
   polls every 15s, and the Dispatch/Dispatch-All buttons are wired (safe to click even in
   no-op mode — should show `dispatched: 0` since flags are still `True`).
5. Flip `SENTIMENT_AUTO_FLUSH=False` locally, trigger a synthetic event through the
   pipeline, confirm `pending` increments on the card, dispatch manually, confirm
   `calls_made`/`dispatched` in the response and `pending` returns to 0 within one 15s poll.
6. Repeat for forecast (force a cache-miss) and classification (feed an article whose
   heuristic resolves to `"general"`).
7. Trigger `POST /tasks/ai_processing_safety_net/trigger` via the worker control endpoint,
   confirm it independently dispatches only categories over their configured thresholds.

Flipping any `AUTO_*` flag to `False` in staging/production, and running migration 0049
there, remain deliberate operator actions per the original doc's rollout sequencing (§8) —
not part of this implementation pass.
