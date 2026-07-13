# Review: Uncommitted Changes Since `dce286f`

Scope: all working-tree changes as of 2026-07-10 (business-logic/prediction-snapshot integration + Docker/dependency updates). Reviewed against production-grade standards (correctness, performance, reliability, security). No fixes applied — findings only.

---

## 🔴 Critical

### 1. Cancellation cascade in prediction-snapshot request coalescing
**File:** `backend/app/services/prediction_snapshot.py` (`get_prediction_snapshot`, lines ~132–171)
**Also touches:** `backend/app/api/v1/ai_stream.py:1155-1166` (`get_explanation`, `asyncio.wait_for(..., timeout=_OPERATION_TIMEOUT_SECS)` at line 144 = 25s)

`get_prediction_snapshot()` coalesces concurrent callers for the same `(instrument_key, timeframe)` onto one shared `asyncio.Task` via `await task`. Every call site wraps its own call in a per-caller timeout (e.g. `get_explanation`'s `asyncio.wait_for(_fetch_explanation_for_instrument(...), timeout=25s)`). When `await task` is cancelled by *one* caller's timeout, asyncio propagates that cancellation into the shared `task` itself, raising `CancelledError` to **every other concurrent caller** awaiting the same instrument — even callers with time left on their own budget.

**Impact:** for a popular symbol viewed by multiple simultaneous SSE/REST clients, one slow/timed-out request silently kills the prediction fetch for everyone else sharing that instrument's in-flight computation.

**Fix direction:** wrap the shared task in `asyncio.shield()` (or otherwise decouple its lifecycle from any individual awaiter's cancellation).

**Test gap:** `backend/tests/unit/test_prediction_snapshot.py::test_get_prediction_snapshot_coalesces_inflight_calls` only exercises the happy path via `asyncio.gather` — no cancellation/timeout scenario is covered, so this bug has no regression test.

**Secondary waste, same function:** non-owner callers each open their own `AsyncSessionLocal()` (in `ai_stream.py`) before finding out they're not the task owner — that DB connection sits open, unused, for the full coalesced wait. Wasteful under load.

---

### 2. Scheduler now runs sequential blocking inference for up to 200 instruments per run
**File:** `backend/app/workers/watchlist_context_scheduler.py`, enqueue loop (~lines 339–375)
**Config:** `WATCHLIST_SCHEDULER_BATCH_CAP` default `200` (`backend/app/core/config.py`)

Previously this loop only published lightweight Kafka messages per stale instrument. It now calls `get_prediction_snapshot()` — full feature load + XGBoost/GRU ensemble inference — **sequentially, one instrument at a time**, inside a single held-open DB session (`async with self._session_factory() as db:` wraps the entire loop), for up to 200 instruments per run.

**Problem A — contradicts the codebase's own documented guidance.** `EnsemblePredictor.predict_batch()` (`backend/app/ml/inference/ensemble_predictor.py:643`) docstring: *"Running N sequential inferences on a 4 GB VRAM card risks VRAM fragmentation; a single batched call avoids that entirely."* This change calls single-instrument `predict()` in a loop instead of using the batched path that exists specifically to avoid this.

**Problem B — no pause/shutdown checkpoint inside the batch.** `self._pause.checkpoint()` and `self._shutdown.is_set()` are only checked at the top of the outer `while` loop (lines 219-220, 236), not between instruments in the enqueue loop. A 200-instrument batch running real inference will not respond to a control-plane pause or shutdown signal until it fully drains — previously this loop completed near-instantly.

**Problem C — silent behavior change on failure.** If snapshot computation raises for an instrument, the `except Exception` at the bottom of the loop logs a warning and **the context job is not published at all** for that instrument. Previously the job was always published (with or without a prediction). Instruments now silently drop out of the watchlist context pipeline on any transient inference error, with no dedicated metric to detect it (only a generic warning log, indistinguishable from a publish failure).

**Fix direction:** batch via `predict_batch()`, add a pause/shutdown checkpoint per instrument (or per sub-batch), and either publish the job anyway on snapshot failure (degrading gracefully, as before) or add a distinct metric/counter for "snapshot failed vs. publish failed" so this is observable.

---

## 🟠 Medium

### 3. `pandas` rolled back from an already-live, previously-validated pin
**File:** `backend/requirements.in`, `backend/requirements.txt`
**Change:** `pandas==3.0.2` → `pandas==2.3.3`

Verified pandas 3.0.0 is a real, official release (2026-01-21), not a phantom/aspirational version — this reverts off a pin the prior comment explicitly called "the working app runs on it... functionally smoke-verified," to an older line. The new comment ("matches the tested Python 3.11.15 runtime currently serving this repo") describes what was observed in some environment, not why the decision is correct — it reads like an environment snapshot capturing whatever happened to be installed rather than a deliberate engineering call. Needs a real justification (what broke on 3.0.2, what was validated on 2.3.3) or it shouldn't ship silently bundled with unrelated infra changes.

### 4. New dependencies added without concrete justification
**File:** `backend/requirements.in`, `backend/requirements.txt`
**Added:** `bcrypt`, `python-jose`, `email-validator`, `matplotlib`, `mlflow`, `tl2cgen`

Comments follow the pattern "Installed in the tested runtime" rather than "used by `<module>` for `<purpose>`." This phrasing across six new deps suggests they were captured from a working `pip freeze` rather than deliberately curated. Specific concern: `python-jose` is added alongside the already-present `PyJWT` — two JWT libraries in one service should be justified or consolidated, not silently coexisting. Recommend confirming each new package is actually imported somewhere before merging.

---

## 🟡 Low

### 5. Stray crash-dump artifact in working tree
**File:** `backend/models/production/error_state_20260708_190118.json` (untracked)

A crash dump from `production_training_orchestrator.py` — `KeyError: 'loss'` in `_train_gru_with_optimization` (line 2236, accessing `history.history['loss']`), i.e. Keras training didn't return the expected metric key. Two separate concerns:
- (a) this is debug/crash debris that shouldn't be committed — should be `.gitignore`d or deleted, not left staged for accidental commit;
- (b) it indicates GRU training in the production orchestrator is currently broken/unresolved — out of scope for this diff, but sitting adjacent to it and worth tracking separately.

---

## ✅ No issues found (verified against current best practice)

- `prediction_snapshot.py`'s dedup of `serialize_prediction_card` across `ai_stream.py`, `ml_predictions.py` (`/predict`, `/prediction-card`) — correct consolidation of previously triplicated logic.
- `aiobreaker` pin `>=1.3.0` → `==1.2.0`: verified 1.3.0 never existed on PyPI (project last released 1.2.0 in 2021) — this is a correctness fix, not a downgrade.
- `TA-Lib` → `0.6.8`: verified TA-Lib ships prebuilt wheels bundling the native C library since 0.6.5 — dropping `libta-lib-dev`/`libta-lib0` apt dependencies in `backend/Dockerfile` is a legitimate simplification.
- `backend/Dockerfile`, `frontend/Dockerfile`: pinned base image tag, non-root user with explicit `chown` for writable runtime paths, multi-stage Next.js standalone build, added healthchecks — aligned with current container best practice.

---

## Open question
Fix priority/order for items #1 and #2 not yet decided — in particular whether the scheduler should move to `predict_batch()` (true batched GPU inference) or stay per-instrument with a checkpoint + concurrency cap added.
