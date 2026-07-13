# Deployment Findings — 2026-07-10

## 1. Live-ML-inference-in-prompt fix: partially deployed

**Code status:** Fix is implemented (uncommitted, on top of commit `dce286f`):
- `backend/app/services/prediction_snapshot.py` (new) — coalesced/batched live prediction snapshot service.
- `backend/app/api/v1/ai_stream.py` — on-demand SSE explanation path now fetches a live snapshot via `get_prediction_snapshot()` and attaches it as `prediction_data` on the Kafka `CONTEXT_JOBS` message.
- `backend/app/workers/watchlist_context_scheduler.py` — pre-warming scheduler updated to batch-fetch live snapshots via `get_prediction_snapshots_batch()` and attach `prediction_data` per instrument.
- `backend/app/ai/intelligence/explanation_worker.py` — already renders `ml_snapshot` into the outgoing Gemini prompt via `_build_context_prompt()` (`## Current ML Ensemble Snapshot` section: direction, confidence, conviction, threshold, probabilities, volatility, per-model breakdown).
- New tests: `test_ai_stream_prediction_context.py`, `test_prediction_snapshot.py`, `test_watchlist_context_scheduler.py` (could not execute locally — broken venv: pydantic/pydantic-core version mismatch + missing `torch`, unrelated to this fix).

**Container status (checked live via `docker exec`):**
- `cortex-api`: has the *new* code — `ai_stream.py` and `prediction_snapshot.py` match the working tree exactly. On-demand path is live.
- `cortex-worker`: has an **older** `watchlist_context_scheduler.py` (hardcodes `"prediction_data": ""`) and is **missing** `prediction_snapshot.py` entirely. The pre-warming/scheduler path is NOT fixed in the running deployment.
- `explanation_worker.py` (prompt-building consumer) is identical and up to date in the worker container either way, so once `prediction_data` is populated, prompts do get the live ML snapshot.
- Both images report the same build timestamp (`2026-07-08T17:46:34Z`) but different image IDs/content for `worker` vs `api` — almost certainly a Docker layer-caching mismatch during build (same Dockerfile/context, divergent COPY layer for `worker`).

**Net effect:** On-demand explanations (user opens a watchlist card) already get live ML inference in the prompt. Scheduler-driven pre-warmed contexts still do not.

**Action needed:** Rebuild the `worker` image (with `--no-cache` or after busting the stale layer) and restart the `worker` container, then commit/deploy the pending changes.

---

## 2. No ML model loaded in either running container

**Symptom:** All predictions return `"available": false, "unavailable_reason": "no_model"`.

**Root cause:** The `ml_models` named Docker volume (`/app/models` in both containers) is completely empty. Startup logs in both `cortex-api` and `cortex-worker` show, on every restart (including 2026-07-08 and 2026-07-10):

```
Bootstrap: inference artifact not found for xgboost at /app/models/production/treelite/xgboost_model.so
Bootstrap: inference artifact not found for gru at /app/models/production/onnx/gru_optimized.onnx
Found: xgboost v1.1.1_xgboost (deployed 2026-06-01 06:19:32.238460+00:00)
Failed to load production ML models: XGBoost Treelite library not found: models/production/models/1.1.1_xgboost_inference.so
```

The model registry DB record points to `1.1.1_xgboost_inference.so`, but the artifact isn't in the volume, so `app.main`'s `lifespan` ensemble load fails and `app.state.ml_predictor` stays `None` for the life of the container.

**The artifacts do exist on the host**, just never copied into the volume:
- `backend/models/production/models/1.1.1_xgboost_inference.so`
- `backend/models/production/models/1.1.1_gru_inference.onnx`
- `backend/models/production/onnx/gru_optimized.onnx`

**Why:** `docker-compose.yml` mounts `ml_models:/app/models` as a fresh named volume rather than bind-mounting or `COPY`-ing `./backend/models` into the image/volume — so it started empty and nothing has populated it since the volume's creation (2026-07-08T14:49:08Z).

**Action needed:** Copy the model artifacts from `backend/models/production/` into the `ml_models` volume (e.g. `docker cp` into a running container, or add a bind mount / COPY step to the compose/Dockerfile) and restart both `api` and `worker` containers.
