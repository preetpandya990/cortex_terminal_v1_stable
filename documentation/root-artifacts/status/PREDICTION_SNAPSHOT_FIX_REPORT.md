# Prediction Snapshot Coalescing & Watchlist Scheduler Fix — Full Report

Date: 2026-07-10
Scope: uncommitted working-tree changes on top of commit `dce286f` (the "Live ML Explanation Fix")

---

## 1. Background — how this investigation started

Two documents already existed in the project root describing prior work:

- **`LIVE_ML_EXPLANATION_FIX_REPORT.md`** — describes a real bug where AI explanations were generated without a live ML prediction ("No live model read available"), caused by four independent producers (REST `/predict`, SSE prediction refresher, explanation Stage 3, watchlist scheduler) having no single source of truth for the prediction snapshot. The fix introduced a new shared service, `backend/app/services/prediction_snapshot.py`, that all four producers now call.
- **`UNCOMMITTED_CHANGES_REVIEW.md`** — an independent code review of that fix's uncommitted diff, which found two **critical** bugs introduced as side effects of the fix, plus several medium/low findings around dependency pins and repo hygiene.

This session cross-referenced both documents, confirmed every finding against the actual code, planned a production-grade fix, implemented it, and verified it against the real environment.

---

## 2. What we found

### 2.1 Critical — cancellation cascade in prediction-snapshot coalescing
`get_prediction_snapshot()` coalesced concurrent callers for the same `(instrument_key, timeframe)` onto one shared `asyncio.Task`. Every caller wrapped its own await in a per-caller timeout (`asyncio.wait_for(..., timeout=25)`). When one caller's timeout fired, asyncio's cancellation propagated into the **shared task itself**, killing the in-flight prediction for every other concurrent caller waiting on the same instrument — even ones with time left on their own budget.

**Deeper issue found during planning (not in the original review):** the shared task's DB session was *borrowed* from whichever caller happened to become the "owner" (first to create the task). If that owner's own request scope exited (e.g. its `async with AsyncSessionLocal() as db:` block closed on timeout), the DB session was closed **out from under the still-running shared task and every other awaiter**, corrupting concurrent reads.

### 2.2 Critical — watchlist scheduler now runs blocking sequential inference
The scheduler had moved from a fire-and-forget Kafka publish loop to calling `get_prediction_snapshot()` (full feature load + GPU inference) **sequentially, one instrument at a time**, for up to 200 instruments per run, inside a single held-open DB session. This:
- Contradicted the codebase's own documented guidance (`EnsemblePredictor.predict_batch()`'s docstring: sequential single-item GPU calls risk VRAM fragmentation on the 4GB card).
- Had no pause/shutdown checkpoint inside the batch loop — a 200-instrument run couldn't respond to a control-plane pause/shutdown until it fully drained.
- Silently dropped the context-job publish entirely if a single instrument's inference failed (previously the job was always published, degrading gracefully).

### 2.3 Medium — `pandas` rolled back without a validated reason
`requirements.in`/`requirements.txt` reverted `pandas==3.0.2` → `2.3.3`, leaving the old "smoke-verified on 3.0.2" comment orphaned above the new pin. The review flagged this as looking like an unvalidated environment snapshot rather than a deliberate decision.

### 2.4 Medium — six "new" dependencies added with weak justification
`bcrypt`, `python-jose`, `email-validator`, `matplotlib`, `mlflow`, `tl2cgen` were added with "installed in tested runtime"-style comments. Cross-referencing against actual code usage found five were genuinely used; `mlflow` turned out to be a **re-pin/relocate of an existing dependency**, not new. `python-jose` duplicates the already-present `PyJWT` (used only in `upstox.py`'s WebSocket auth JWT decode).

### 2.5 Low — stray crash-dump artifact
`backend/models/production/error_state_20260708_190118.json` (untracked) — a crash dump from `production_training_orchestrator.py` (`KeyError: 'loss'` in GRU training), left sitting in the working tree.

---

## 3. What we decided to do about it

We planned the fix in Claude's Plan Mode, researched current (2026) asyncio best practice for cancellation-safe request coalescing, examined an existing reusable pattern in `signal_scheduler.py` for concurrency-limited batched ML inference, and got explicit direction from you on three judgment calls before writing any code:

| Decision point | Choice made |
|---|---|
| `python-jose` / `PyJWT` duplication | **Deferred** to a separate follow-up — WS auth is sensitive, hardened code; not touched in this fix. |
| `pandas` pin | **Verify empirically** rather than guess — install both versions, run the real test suite, re-pin with an honest reason. |
| Scheduler fix depth | **Real batched GPU inference** (`predict_batch()`), not just a concurrency cap on the existing sequential single-item calls — matches the codebase's own documented VRAM guidance. |

The resulting plan (five workstreams) was written to a plan file, approved, and then executed.

---

## 4. How we did it

### 4.1 `backend/app/services/prediction_snapshot.py` — cancellation isolation + independent sessions
- `get_prediction_snapshot()` now takes a `session_factory` instead of a borrowed `db` session. The shared computation opens and closes its **own** session (`async with session_factory() as db:`), fully decoupled from any caller's request lifecycle.
- Each caller now awaits `asyncio.wait_for(asyncio.shield(task), timeout=...)` — the current, standard asyncio idiom for isolating a shared task from any one awaiter's cancellation. Confirmed via current best-practice sources.
- Added an internal `_TASK_COMPUTE_TIMEOUT_SECS = 30` ceiling (above the largest known caller budget of 25s) so a hung computation can't leak a DB connection forever; on internal timeout it returns a new explicit `unavailable_prediction_snapshot("timeout")` state.
- Cleanup of the in-flight task registry is now tied to the task's **own completion** (via `add_done_callback`), not to whichever caller's wait happened to finish first — otherwise a shielded caller timing out early would prematurely evict the still-running task from the registry, causing duplicate concurrent work for late arrivals.
- Updated all four call sites (`ai_stream.py` ×2, `ml_predictions.py` ×2) to pass `session_factory=AsyncSessionLocal` instead of a request-scoped `db`; removed now-unnecessary `Depends(get_db)` dependencies where `db` had no other use in those endpoints.

### 4.2 Watchlist scheduler — real batched inference
- New `get_prediction_snapshots_batch()` in `prediction_snapshot.py`, modeled directly on the existing pattern in `signal_scheduler.py`: concurrent, semaphore-gated feature loading (one short-lived DB session per instrument, never one session held across a whole batch) followed by **exactly one** `predictor.predict_batch()` GPU call per chunk. Returns a snapshot for every requested instrument, including unavailable ones — nothing is silently dropped.
- `watchlist_context_scheduler.py`'s `_run_batch()` rewritten to process `to_enqueue` in chunks of `WATCHLIST_SCHEDULER_CHUNK_SIZE` (new setting, default 20, modeled on `SIGNAL_SCHEDULER_FEATURE_CONCURRENCY`'s precedent). Pause/shutdown are now checked **between every chunk**, not just at the outer scheduling loop, so a large run stays responsive to the control plane. A context job is always published per instrument, even when its snapshot is degraded — matching the pre-regression "always publish, degrade gracefully" behavior.
- Two new Prometheus metrics added (`watchlist_scheduler_snapshot_unavailable_total` by reason, `watchlist_scheduler_publish_failed_total`) so Grafana can distinguish "inference degraded but published" from "publish itself failed" — the review's explicit ask.

### 4.3 `pandas` pin — empirical verification, with a real discovery
Attempted to install `pandas==3.0.2` in the environment to test it: pip immediately reported `mlflow 3.13.0 requires pandas<3, but you have pandas 3.0.2`. Further investigation (downloading and inspecting wheel METADATA directly) confirmed **mlflow's entire 3.x line — verified from 3.0.0 through the current latest, 3.14.0 — declares `pandas<3`**. This is a hard, current, unresolved upstream constraint, not an environment snapshot mistake. `pandas==2.3.3` was therefore the *correct* pin all along; the original rollback just had an inaccurate justification comment.
- Rewrote the comments in `requirements.in` and `requirements.txt` to state the real, verified reason.
- Updated the repo's own committed contract test (`tests/unit/test_dependency_management_r7.py`, which treats `pandas` as a "critical pin that must never silently drift") to assert `2.3.3` instead of `3.0.2`, with the same justification documented inline.
- **Left `requirements.lock` untouched** — it's a pip-compile-generated artifact (the same test file enforces this), and regenerating it correctly requires running `make lock-prod` with full network access, which wasn't available in this session. Hand-editing a generated lockfile would have been exactly the kind of shortcut explicitly ruled out for this fix. **This is a required follow-up action for you.**

### 4.4 Housekeeping
- Confirmed `mlflow` was a re-pin/relocate, not a new dependency — verified `mlflow==3.13.0`'s `MlflowClient` API (the only API surface used, in `admin_training.py`) is unchanged and compatible.
- Deleted the stray crash-dump file and added a `.gitignore` rule (`backend/models/production/error_state_*.json`) so future ones don't land in the working tree.
- `python-jose`/`PyJWT` consolidation intentionally left out of scope, per your decision.

### 4.5 Testing
- Extended `tests/unit/test_prediction_snapshot.py` with new regression tests: cancellation isolation between two concurrent callers with different timeouts, proof the shared task never touches a caller-supplied session, and internal-timeout cleanup.
- Extended `tests/unit/test_watchlist_context_scheduler.py` with new tests: correct chunking (45 instruments / chunk size 20 → 3 batch calls, not 45), mid-batch shutdown responsiveness, always-publish-on-degraded-snapshot, and publish-failure isolation between instruments in the same chunk.
- Local `pip install` couldn't reasonably pull the full torch/tensorflow ML stack needed to import these modules (this was the same blocker noted in the original fix report). Instead, ran the real test suite **inside your running `cortex-api` dev container**, which already has the full ML dependency stack installed — the same approach the original fix report couldn't complete.

---

## 5. Outcome

- **14/14 new and previously-blocked tests pass** (`test_prediction_snapshot.py`, `test_watchlist_context_scheduler.py`, `test_ai_stream_prediction_context.py`) — this resolves the original fix report's "pytest execution blocked" gap.
- **Full `tests/unit` + `tests/api` suite**: 870 passed. The 47 failures / 33 errors present are all **pre-existing and unrelated** to this fix (audit-logger API drift, missing deploy artifacts like systemd unit files, a stale/removed ensemble-API test file, real-DB-state issues) — none touch any file changed in this session.
- All touched files pass `python -m py_compile`.
- The code graph (`graphify-out/`) was updated to reflect all changes.
- The running dev container was used only to borrow its ML dependencies for test verification and was **not restarted**; deploying these fixes remains your deliberate action, consistent with how you've handled every other fix in this project.

### Files changed this session
**New:**
- `backend/app/services/prediction_snapshot.py` extended with `get_prediction_snapshots_batch()` and the session/cancellation fixes (file already existed from the prior fix; this session modified it further)

**Modified:**
- `backend/app/services/prediction_snapshot.py`
- `backend/app/api/v1/ai_stream.py`
- `backend/app/api/v1/ml_predictions.py`
- `backend/app/workers/watchlist_context_scheduler.py`
- `backend/app/core/config.py` (new `WATCHLIST_SCHEDULER_CHUNK_SIZE` setting)
- `backend/app/core/metrics.py` (two new Prometheus counters)
- `backend/requirements.in`, `backend/requirements.txt` (honest pandas/mlflow comments)
- `backend/tests/unit/test_prediction_snapshot.py`
- `backend/tests/unit/test_watchlist_context_scheduler.py`
- `backend/tests/unit/test_dependency_management_r7.py` (pandas critical-pin contract updated to 2.3.3)
- `.gitignore` (crash-dump artifacts)

**Deleted:**
- `backend/models/production/error_state_20260708_190118.json`

### Still open — requires your action
1. **`requirements.lock` regeneration** — still pins `pandas==3.0.2`; run `make lock-prod` (needs network access) to bring it in line with `requirements.in`'s `2.3.3`.
2. **Deploy** — all fixes are code-complete and tested but, per your usual workflow, uncommitted and undeployed. Commit and deploy when ready.
3. **`python-jose`/`PyJWT` consolidation** — explicitly deferred; worth its own scoped task with WS-auth regression coverage.
4. **GRU training `KeyError: 'loss'` bug** — the root cause behind the crash-dump file that was deleted; out of scope here but still unresolved in `production_training_orchestrator.py`.
5. **`test_auth_coverage.py` failure** — pre-existing, found incidentally while running the full suite: `GET /api/v1/ai/explanation` and `POST /api/v1/ai/explanation/{suggestion_id}/request` lack a FastAPI auth dependency (auth appears to be handled in-band inside the function body, similar to the SSE endpoint's documented pattern, but isn't registered in the test's allowlist). Not touched in this session — flagging for your awareness.
