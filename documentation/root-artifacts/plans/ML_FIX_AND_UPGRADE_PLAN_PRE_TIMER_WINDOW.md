# ML Fix & Upgrade Plan — Pre-Timer Window

**Created:** 2026-07-16 ~17:15 IST
**Deadline:** Saturday 2026-07-18 20:02 IST — `cortex-retrain.timer` fires and launches the next scheduled training run (~51h window)
**Goal:** Saturday's timer-launched run trains the upgraded challenger (v1.2.0) on fixed code: live fundamental features + feedback weights + crash-proof data fetch. If validation fails, a built-in fallback still guarantees a healthy fresh-data challenger.

---

## Context — verified findings driving this plan (2026-07-16)

1. **All 20 fundamental features are dead weight in every model trained to date.**
   Fundamentals are computed once as of `end_date` and broadcast as a constant per symbol
   (`app/ml/features/feature_pipeline.py`, `compute_features_for_symbol`). Rolling z-score
   normalization (`normalize_features`, method='rolling') maps any constant column to exactly 0
   (std < 1e-8 → std := 1.0, x − mean = 0). Applied identically in training
   (`scripts/production_training_orchestrator.py:1553, 1900, 2005`) and inference
   (`app/ml/inference/feature_loader.py:_prepare_features`). The model has never seen a real
   fundamental value — the "69-feature model" is effectively 49 informative features + 20 zero columns.
   - This **subsumes** the previously-known "fundamentals NaN → zero-fill at single-symbol inference"
     gap: whatever value is imputed normalizes to 0 anyway.
   - **Latent look-ahead hazard:** the constant broadcast stamps *today's* PE/ROE/debt onto rows 10
     years back. Inert today (zeroed), but becomes real CPCV leakage the moment normalization is
     fixed — so point-in-time correctness MUST land together with the normalization fix.
   - Verified enabler: all 4 fundamentals queries are already point-in-time capable
     (`period_date <= as_of_date` — `app/ml/features/fundamental_features.py:166,207,242,262`).

2. **The scheduled run crashes on a poisoned DB session.**
   4 aborted runs on 2026-07-15; first death: `sqlalchemy.exc.PendingRollbackError` — the per-symbol
   `try/except` in `compute_features_batch` swallows a query failure **without `rollback()`**, so every
   subsequent symbol's query re-raises `_revalidate_connection → _invalid_transaction` forever.
   Log: `backend/logs/scheduled_retrain/scheduled_retrain_20260715T153428Z.log`.

3. **The scheduled path never uses feedback weights.**
   `scripts/scheduled_retrain.py` invokes the orchestrator with only `--fresh`. The Phase-2 feedback
   pipeline (`app/ml/training/feedback_loader.py`, `--feedback-weights`) is only reachable via manual
   console dispatch — the weekly challenger has never learned from paper-trade outcomes.

4. **Registry projection drift (serving unaffected, governance blind).**
   `ml_model_metadata`: `1.1.1_xgboost` + `1.1.1_gru` production/active since 2026-06-01.
   `ai_ml_models`: **no live rows** — the 1.1.1 "stamp" promotion (byte-identical artifacts, see
   `backend/models/production/models/1.1.1_metadata.json` note) bypassed the A8 atomic dual-write.

5. **Stale-memory corrections (already fixed, no work needed):**
   - Technical-signal dilution (<52 candles) — fixed via dynamic weight renormalization in `fuse_signals`.
   - Event-score count normalization — fixed via 1/√N dampening + ±100 clip.
   - GRU is NOT weak/shadow — 1.1.1 runs both models live, ~51/49 accretive ensemble, DSR 0.7276.

6. **State as of this plan:** the in-flight 07-15/16 training run was stopped (SIGTERM, clean) and its
   checkpoint state wiped (`backend/models/production/checkpoints/` recreated empty, 4.8 GB freed).
   Serving artifacts + live 1.1.1 models untouched. Lock free. Timer left enabled by design.
   Current source has `gru_trials=15` (old runs used 5) → Saturday's run runs a few hours longer;
   fits the 24h systemd `TimeoutStartSec`.

---

## Workstreams

### WS1 — Survival fix: `PendingRollbackError` (~2–3h, FIRST, non-negotiable)

The run must survive before any upgrade matters.

- [ ] Dig the FIRST error out of `scheduled_retrain_20260715T153428Z.log` (what poisoned the session)
      and fix that trigger.
- [ ] `compute_features_batch` (`app/ml/features/feature_pipeline.py`): `await db.rollback()` in the
      per-symbol except path (or session-per-chunk) so one bad symbol can't cascade to all subsequent ones.
- [ ] Confirm `--fresh` initializes cleanly against the now-empty checkpoints dir.

### WS2 — Bring the 20 fundamental features to life (~1–1.5 days, the core upgrade)

**2a. Point-in-time series (leakage fix — mandatory companion to 2b):**
- [ ] New `compute_fundamental_features_series()` in `fundamental_features.py` — fetch each symbol's
      full quarterly history in ~4 queries (not 40× per-date calls), compute the 20 features at each
      quarter boundary in pandas, `merge_asof` (step-function, ffill) onto the daily feature frame.
- [ ] Wire into `compute_features_for_symbol` / batch path, replacing the single-date broadcast.

**2b. Cross-sectional normalization (the actual "features are zero" fix):**
- [ ] Exclude the 20 fundamental columns from rolling z-score in `normalize_features` callers
      (training orchestrator + feature_loader).
- [ ] Rank-normalize fundamentals per date across the universe → [-1, 1], in the existing batch
      post-pass where median imputation already lives (`compute_features_batch`).
      **Rank over z-score**: PE/EV-EBITDA tails are brutal; rank needs no winsorization.
- [ ] NaN → cross-sectional median before ranking (existing pattern, now on point-in-time values).

**2c. Inference parity — version-gated (do NOT shift the live champion's input distribution):**
- [ ] Daily feature-store refresh persists per-feature cross-sectional quantile grids
      (e.g., 101 quantiles/feature/day; small table or JSON blob).
- [ ] `feature_loader` single-symbol on-demand path: impute from persisted median, interpolate rank
      from the persisted grid.
- [ ] **Gate by the model's `feature_version` from registry metadata:** models with feature_version
      1.0.0 (live 1.1.1) keep the old behavior (fundamentals → 0 — their trained distribution);
      new semantics activate only for feature_version 2.0.0 models after promotion.

**Version bumps:**
- [ ] `feature_version` 1.0.0 → 2.0.0 in registration metadata / manifest.
- [ ] Checkpoint `SCHEMA_VERSION` 4 → 5 (feature semantics are model-affecting).

### WS3 — Feedback weights in the scheduled path (~2–3h, Friday)

- [ ] `scripts/scheduled_retrain.py`: build a fresh feedback bundle (call `feedback_loader` /
      `build_feedback_weights.py` logic) before invoking the orchestrator; pass `--feedback-weights <path>`.
- [ ] Non-fatal: bundle-build failure logs loudly and trains unweighted (never blocks the run).

### WS4 — Hygiene (Saturday, only if time permits; nothing blocks the run)

- [ ] Repair `ai_ml_models` projection: insert/update live rows for 1.1.1 via the A8 projection logic;
      root-cause how the stamp promotion bypassed `ModelPromoter`.
- [ ] Fusion threshold `>50` → `>=50` (`app/ai/fusion/signal_assembler.py:850-852`).
- [ ] Purge dead `lstm`/`test_drift_model_*` registry rows.

---

## Validation gate — Saturday afternoon, hard stop before 20:02 IST

- [ ] Full ML suite green (461 tests baseline — zero regressions).
- [ ] Four NEW tests:
  - [ ] Fundamentals survive normalization (non-zero variance in the final feature matrix).
  - [ ] Point-in-time leakage probe: mutating a future quarter's report must NOT change earlier rows.
  - [ ] Inference rank-grid round-trip parity (batch value == on-demand value for same symbol/date).
  - [ ] Session-rollback resilience: one failing symbol doesn't poison subsequent symbols.
- [ ] Smoke run: orchestrator `--fresh` with ~20-symbol universe through steps 1–4 (~30–60 min),
      confirming feature step produces live fundamentals end-to-end.

**Decision checkpoint ~17:00 Sat:**
- Smoke GREEN → do nothing; timer launches v1.2.0 with live fundamentals + feedback weights.
- Smoke RED → revert **WS2 only** (WS1 + WS3 still ride). Saturday still produces a healthy
  fresh-data, feedback-weighted challenger; fundamentals land the following week.

---

## Sequencing

| When | What |
|---|---|
| Wed night (07-16) | WS1 complete |
| Thu (07-17) | WS2a + WS2b |
| Fri (07-18 day) | WS2c + version bumps + WS3 |
| Sat (07-18, by ~17:00) | WS4 (if time) + validation gate + smoke run → decision |
| Sat 20:02 | Timer fires — hands off |
| Sun (07-19) | Review C2 promotion report; operator decides on promotion via `promote_model.py` (which also exercises the repaired A8 projection) |

## Out of scope this window (backlog)

- TFT (Workstream B) — still hardware-blocked (RTX 3050 4GB / WSL CUDA ceiling).
- 1-week timeframe model (trainer exists, never productionized).
- GRU HPO budget tuning beyond the source default (now 15 trials).
