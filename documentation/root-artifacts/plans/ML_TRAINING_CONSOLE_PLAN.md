# ML Training Operator Console — Plan

**Date:** 2026-05-27
**Status:** Approved, awaiting implementation
**Predecessors:** `ML_AUDIT_REPORT.md` → `ML_REMEDIATION_PLAN.md` → `ML_IMPLEMENTATION_PLAN.md` → `ML_IMPLEMENTATION_TASKS.md`

---

## Context

The current ML pipeline is CLI-only: `production_training_orchestrator.py` runs blind for hours, six `error_state_*.json` files have piled up in `backend/models/production/` over the last two days from failed pre-flight conditions (env drift, GPU pressure, data gaps), and the realized-outcome signal from paper trading is consumed only as drift advisory by D1 — it never re-enters training.

This plan introduces an operator-only web console that closes those three gaps without replacing the existing infrastructure: it pre-flights before launch, streams live progress with cancel support, and (Phase 2) builds a feedback-weights artifact the orchestrator's next run consumes.

The console will be used heavily during the current low-accuracy phase and naturally decay to rare ad-hoc use once the systemd weekly retrain is producing stable models.

## Decisions locked

- **Operator-only.** No RBAC; admin-gated route in the existing frontend.
- **Host = existing `frontend/` Next.js app**, new route `/admin/training/`. Reuses the existing auth, WS in-band reauth protocol, `MLModelsPanel` patterns, and types.
- **Outcome maturity gate = `closed_at + 1 day`.** TP1/TP2/TP3/SL flags are settled by then.
- **Gating against systemd**: hard lock-sharing via `.scheduled_retrain.lock` (`fcntl.flock`) + soft 4-hour warning before the next `OnCalendar` fire (operator can override with a reason string that is audited).
- **Phase 2 = B1 reweighting + B2 active learning merged into a single weight formula** (no two-pipeline split).
- **No bypass of A6 quality gates from the UI.** Promotions still go through `ModelPromoter` + `promote_model.py`; break-glass still requires the `--reason` + `CONFIRM` flow.

## Scope: two-phase delivery

- **Phase 1 — Dispatch console.** Ships first. Solves the error_state problem and gives the UI shell. Adds zero ML logic; wraps existing scripts.
- **Phase 2 — Feedback formatter.** Builds on Phase 1's shell. Needs ~7+ days of matured paper-trade outcomes before it has meaningful data to weight on, so the staging is natural — Phase 1 starts producing trades while Phase 2 is being built.

---

## Phase 1 — Dispatch console design

### Frontend (`frontend/src/app/admin/training/`)

- New page mirroring `frontend/src/app/admin/governance/page.tsx` structure (admin guard, same layout shell).
- Tabs: **Preflight** · **Launch** · **Live Run** · **History**.
- Preflight tab is a green/red status board (GPU memory free MB, env package drift count, data coverage at threshold, lock state, time-to-next-systemd-fire). Red on any gate → Launch tab disabled.
- Live Run tab subscribes to a WS that tails `run_log.ndjson` and emits structured step events (step number 1–10, current loss/AUC-PR/etc., ETA). Cancel button sends a graceful SIGTERM that lets the orchestrator write its current checkpoint before exiting.
- History tab queries MLflow (already at `backend/mlruns/`) for past runs; links into the existing MLflow UI for the deep dive (don't reimplement).

### Backend (`backend/app/api/v1/admin_training.py` — new)

Routes:
- `GET /api/v1/admin/training/preflight` → runs all probes, returns `PreflightReport` (per-gate pass/warn/fail + remediation hint).
- `POST /api/v1/admin/training/launch` → accepts `TrainingConfig` overrides + `reason` string; acquires the shared lock; shells `scheduled_retrain.py --once` (reuse this entry point — it already handles lock + subprocess + log tee) or directly the orchestrator with the override; returns `run_id`.
- `GET /api/v1/admin/training/runs` → MLflow + checkpoint-dir union; paginated.
- `WS /api/v1/admin/training/runs/{run_id}/stream` → tails `run_log.ndjson`, emits parsed events. Uses the in-band WS auth pattern.
- `POST /api/v1/admin/training/runs/{run_id}/cancel` → SIGTERM the subprocess; checkpoint preserved.

Preflight probes (single-purpose helpers):
- `_probe_gpu()` → `nvidia-smi --query-gpu=memory.free,memory.total --format=csv,noheader,nounits`. Fail if `free < 1500 MiB`; warn if `< 2500 MiB`.
- `_probe_env()` → `pip freeze` diff against the locked `requirements-ml-training.txt`. Fail on any deviation in the **critical set** (numpy, scikit-learn, tensorflow, torch, onnx, xgboost) since those break GPU per the ENV CONSTRAINT.
- `_probe_data()` → row counts from `upstox_ohlcv` for 1D + symbol coverage at `min_symbol_coverage` threshold; fundamentals freshness from the fundamentals tables.
- `_probe_lock()` → non-blocking `fcntl.flock` attempt on `.scheduled_retrain.lock`; fail if held.
- `_probe_schedule()` → distance to next `SCHEDULED_RETRAIN.cron_*`; warn if within 4 h.
- `_probe_disk()` → free bytes in `backend/models/production/` checkpoint root.

### Reuse, don't reinvent

- `scheduled_retrain.py::run_one_challenger()` already handles lock-acquire + subprocess + log tee — the launch endpoint just calls it with `dry_run=False`.
- `run_log.ndjson` is already written by `_write_run_log_entry` (E3) — the WS tails this file.
- MLflow's local file store at `backend/mlruns/` already has all run metadata — query via `mlflow-skinny`'s `MlflowClient`, don't shadow it.
- `ConfigOverride` schema mirrors `TrainingConfig` fields one-to-one — generate the TS type from the dataclass to avoid drift.

---

## Phase 2 — Feedback formatter design

### The combined B1+B2 weighting formula

```
sample_weight(signal) = outcome_factor × confidence_interaction_factor
sample_weight ∈ clipped to [0.1, 5.0]
```

**outcome_factor (B1 — reinforce what paid):**
- `hit_tp3` → 3.0
- `hit_tp2` → 2.0
- `hit_tp1` → 1.5
- `direction_correct` only (no TP hit, no SL hit) → 1.0
- `hit_sl` → 0.5 (down-weight, not zero — we still want the model to see the failure)

**confidence_interaction_factor (B2 — mine hard negatives):**
- `confidence ≥ 0.70` AND `NOT direction_correct` → ×2.0 (confident-and-wrong = highest priority signal)
- `confidence < 0.50` AND any outcome → ×1.0 (model already uncertain; no boost)
- everything else → ×1.0

Final weight clipped to `[0.1, 5.0]` so a few outliers don't dominate the gradient. Constants are starting points — Phase 2 includes a "preview weight distribution" view in the UI so operator can sanity-check the histogram before committing to a run.

### Outcome-maturity filter

```sql
WHERE pto.closed_at < NOW() - INTERVAL '1 day'
```

Applied at the join step. Subject to two read-only verifications before formalizing (see "Open verifications" below).

### Files to create

- `backend/scripts/build_feedback_weights.py` — joins `paper_trade_outcomes` ↔ `ai_trading_signals` ↔ stored feature snapshot at signal time; applies the weight formula; writes parquet `(signal_timestamp, instrument_key, sample_weight)` to `backend/feedback_bundles/feedback_weights_{utc_iso}.parquet`. Idempotent + dry-run mode.
- `backend/app/ml/training/feedback_loader.py` — thin loader that orchestrator step-2/3 calls when `--feedback-weights <path>` is passed; merges weights onto the training DataFrame by `(signal_timestamp, instrument_key)`; falls back to `1.0` for unmatched rows (cold rows from before paper trading existed).

### Orchestrator wiring

- `production_training_orchestrator.py` accepts new optional `--feedback-weights <path>` CLI flag.
- Step-2 (`_compute_features`): no change to feature computation; just records the path.
- Step-3 (`_generate_targets_and_weights`): if path provided, merges the weights file; threads result as `sample_weight=` into XGBoost `DMatrix` and Keras `model.fit(sample_weight=...)`.
- **Lineage tag** (critical for honesty): the feedback bundle SHA-256 + row count + window (min/max `closed_at`) goes into `MLModelMetadata.lineage` JSONB as a new `feedback_bundle` block. C2 promotion report surfaces it so reviewers see "trained with N feedback rows from window X-Y" — keeps the bundle integrity story intact.

### UI tab in Phase 2

New **Feedback** tab in `/admin/training/`:
- "Build bundle" button → calls a new `POST /api/v1/admin/training/feedback/build` that runs `build_feedback_weights.py`.
- Preview: total row count, window (min/max `closed_at`), weight histogram (matplotlib → PNG or recharts), top-10 highest-weighted signals by symbol.
- "Use in next launch" toggle on the Launch tab references the most recent bundle path.

---

## Open verifications (do these before Phase 2 implementation, all read-only)

- **Outcome write timing.** Confirm `hit_tp1/2/3` and `hit_sl` are written atomically with the position close in `paper_trading/outcome_service.py`, not lazily by a separate worker. If lazy → the maturity gate must be `MAX(closed_at, outcome_written_at) + 1 day` instead.
- **Partial-fill attribution.** Confirm whether each partial close produces its own `PaperTradeOutcome` row or whether outcomes aggregate to one row per full close. Affects how weight is attributed back to the originating signal.

---

## Files to create / modify

**Backend (new):**
- `backend/app/api/v1/admin_training.py` — REST + WS routes
- `backend/scripts/build_feedback_weights.py` — Phase 2 weight builder
- `backend/app/ml/training/feedback_loader.py` — Phase 2 weight loader for orchestrator
- `backend/feedback_bundles/` — output dir (gitignored)

**Backend (modify):**
- `backend/scripts/production_training_orchestrator.py` — `--feedback-weights` flag + step-2/3 wiring + lineage tag
- `backend/scripts/scheduled_retrain.py` — expose `run_one_challenger` as importable + accept the feedback-weights path
- `backend/app/main.py` (or wherever routes register) — wire the new router

**Frontend (new):**
- `frontend/src/app/admin/training/page.tsx`
- `frontend/src/app/admin/training/components/PreflightBoard.tsx`
- `frontend/src/app/admin/training/components/LaunchForm.tsx`
- `frontend/src/app/admin/training/components/LiveRunStream.tsx`
- `frontend/src/app/admin/training/components/RunHistory.tsx`
- `frontend/src/app/admin/training/components/FeedbackPreview.tsx` (Phase 2)
- `frontend/src/hooks/useTrainingRunStream.ts` — WS hook mirroring `usePnLWebSocket.ts`

**Frontend (modify):**
- `frontend/src/lib/api.ts` — add `adminTrainingAPI`
- `frontend/src/types/` — add `TrainingConfig`, `PreflightReport`, `RunEvent`, `FeedbackBundle` types

---

## Verification (per phase)

**Phase 1:**
- Launch a real training run from the UI, watch it stream all 10 steps, cancel mid-step-5, confirm checkpoint preserved (orchestrator resumes on next launch).
- Simulate each preflight failure: hold the lock manually, deliberately downgrade numpy in a venv, set `min_symbol_coverage` higher than current coverage. Each must show red and block launch.
- Confirm UI launch and systemd timer can't fire simultaneously: hold the lock from the UI for 5 minutes during a Saturday window; timer must skip the slot.

**Phase 2:**
- Run `build_feedback_weights.py --dry-run` — inspect weight histogram, confirm no NaN, confirm row count matches `SELECT COUNT(*) FROM paper_trade_outcomes WHERE closed_at < NOW() - INTERVAL '1 day'`.
- Train end-to-end with `--feedback-weights <path>`; confirm `MLModelMetadata.lineage.feedback_bundle.sha256` matches the file's SHA-256.
- C2 promotion report for that run includes the feedback bundle block — operator-visible.
