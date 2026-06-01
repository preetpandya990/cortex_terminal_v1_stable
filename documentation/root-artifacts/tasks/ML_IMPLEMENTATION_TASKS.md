# Cortex ML — Implementation Task Breakdown (with code skeletons)

> ## ⚠️ EXECUTION AMENDMENTS — 2026-05-18 (AUTHORITATIVE — supersede any conflicting text below)
> Discovered during A0 execution against the live codebase/env. Where this block conflicts with anything below, **this block wins**.
> 1. **CPCV is IN-HOUSE** (`app/ml/evaluation/cpcv.py`); **skfolio is NOT used**. Installing skfolio cascades numpy 1.26→2.4 / sklearn 1.4→1.8 and breaks TF 2.21 GPU. Every `skfolio` import/usage snippet below is **void** — implement CPCV from the López de Prado spec (zero new deps), consistent with in-house DSR/PBO.
> 2. **Migration is `0034`**, `down_revision="0033"` (alembic head was 0033 not 0028; `0029` already taken). ✅ **DONE & verified** — `backend/alembic/versions/0034_ml_lineage.py`, `MLModelMetadata.lineage` JSONB, up/down round-trip green.
> 3. **`app/ml/evaluation/` ALREADY EXISTS** (`metrics.py`/`gates.py`/`shap_validator.py`, populated `__init__.py`). Do **not** recreate it. A2/A3 ADD modules + extend `__init__` additively; A3/A6 must reconcile with existing `EvaluationGate`/`MetricsCalculator` (no duplication — same single-authority principle as A8).
> 4. **"torch only under `app/ml/models/tft/`" + the grep CI guard are VOID.** `torch` is already first-class (FinBERT `nlp_engine.py`, `tuner.py`, `trainer.py`, `onnx_converter.py`). The boundary is the `SequenceModelAdapter` **API** (orchestrator/ensemble depend on the adapter, never on torch internals), **not** an import ban. Delete the `check_torch_isolation` guard from B1/E1.
> 5. **Env reality (locked & verified):** numpy `1.26.4` (pinned <2.0 — numpy 2.x breaks TF 2.21 GPU stack), pandas `3.0.2` (kept+verify), sklearn `1.4.0`, **torch `2.11.0+cpu`**, onnx `1.21.0`, TF[and-cuda] `2.21.0` (GPU verified True). `requirements.txt`/`requirements-ml-training.txt` re-pinned to truth (divergence footgun removed). Orphan `-cu13` purge = task **A0.1b**; non-ML prod-manifest drift = separate tracked follow-up.
> 6. **No new heavy deps in A0.** pytorch-forecasting/lightning/mlflow/apscheduler are deferred to their phases (B/C/E). **Workstream B (TFT) compute is gated on a CPU-benchmark investigation** — driver CUDA ceiling 12.2, RTX 3050 4 GB, torch is CPU-only on this box.

**Date:** 2026-05-17 (amended 2026-05-25) · **Status:** A1–A8 + **C1** + **C2** + **C3** + **D1** + **E1** + **E2** + **E3** + **F1** complete (461/461 ML suite green — zero regressions). Workstream A (correctness/P0) DONE; **Workstream B PAUSED** (TFT-on-CPU empirically infeasible: ~13 min/epoch at 200-sym scale → ≥100h for HPO budget; RTX 3050 4GB has only 336 MiB free at idle; cu13 orphan in venv compounds GPU install risk — revisit when hardware story changes). **C3 honest re-eval verdict on live 1.0.0: `DEMOTE_RECOMMENDED`** (both xgboost+gru fail 3 hard gates + have 3 structural findings each; report at `backend/incident_reports/reeval_1.0.0_20260523T131822Z.json`). **C1 scheduled retrain** wired (systemd `cortex-retrain.timer` Sat 20:00 IST + APScheduler fallback; `scheduled_retrain.py --once|--schedule|--dry-run`, `fcntl.flock` concurrency guard; no auto-promote — operator-only). **C2 Champion/Challenger Promotion Report** complete — signed immutable JSON artefact auto-generated at step 10; `promote_model.py production` requires `--report-path` as a hard gate. **D1 Advisory drift** complete — 3-signal composite (distribution shift + realised accuracy + realised IR), Prometheus gauges, `drift_advisory` in signed C2 report with operator guidance. **E2 Bundle integrity** complete — SHA-256 artifact manifest in `lineage` JSONB, audited break-glass JSON, calibrator integrity check on load. **E3 MLflow lineage** complete — `mlflow-skinny 3.12.0`, local file store `backend/mlruns/`, NDJSON run log, `mlflow_run_id` in signed C2 report. **F1 Event-Driven Backtest** complete — 1-bar latency engine, vectorized NSE CNC charge coefficients, per-symbol grouping, 20% relative agreement check vs A3 vectorized reference, embedded in C2 Promotion Report body (SHA-256 covered); 46/46 tests green. **ML overhaul all workstreams done (except B paused).** Operator still needs to apply C3 demotion + install systemd timer.
**Source of truth:** `ML_IMPLEMENTATION_PLAN.md` (§4 workstreams). This document decomposes it into execution-ready, unit-of-work tasks with file targets, code skeletons (key logic only — boilerplate marked `# TODO`), and pass-before/fail-after acceptance tests.
**Scope (owner-confirmed):** ML Workstreams A–F **plus only direct cross-cutting touchpoints** (fusion serializer calibration, registry consolidation + governance API, AUC-PR as the primary metric). No auth / Strategy-FSM / unrelated debt.

**Standing engineering rules (apply to every task):**
- Primary classification metric is **AUC-PR (`average_precision_score`)**, never raw accuracy — confirmed user standard for imbalanced trading ML. Accuracy is reported, never gated alone.
- Boundary discipline: orchestrator/ensemble/serving depend on the `SequenceModelAdapter` **API**, never on a sequence model's framework internals. (NB: a `torch` import-ban is infeasible — torch is already first-class for FinBERT/tuner/trainer; see Execution Amendment #4.)
- Every defect-fix task ships a regression test that **fails on current `main` and passes after**.
- Deterministic seeds; structured logging; no silent fallbacks; no `--skip-gates` in any automated path.
- Type-hinted, `from __future__ import annotations`, async where the surrounding module is async.

Legend — Phase: 🔴P0 🟠P1 🟡P2 🟢P3 · IDs match the tracked task list.

---

## Workstream A — Correctness & Trustworthy Evaluation 🔴

### A0 — Environment integrity + schema bring-up  ✅ **DONE & VERIFIED (2026-05-18)**
**As-built — diverged substantially from the original plan; here is what actually happened.**

The planned "pin/install skfolio + pytorch-forecasting + lightning + mlflow + apscheduler" was **abandoned on evidence**: a non-mutating dependency dry-run proved `skfolio` cascades `numpy 1.26→2.4` / `sklearn 1.4→1.8`, which breaks **TensorFlow 2.21 GPU**. Decision (owner-confirmed): **CPCV/DSR/PBO/backtest are in-house** (`app/ml/evaluation/`), **zero new heavy deps in A0**; pytorch-forecasting/lightning/mlflow/apscheduler deferred to their phases.

Investigation then revealed the live `.venv` was a partial, internally-contradictory env from prior makeshift GPU-training fixes. A0 became an **environment-integrity remediation**, split into:

- **A0.1 ✅** Root-caused the breakage (the original `numpy==2.1.3` spec breaks TF 2.21's `ml-dtypes`/ABI → silently disabled GPU). Verified-good locked set: **numpy 1.26.4 (<2.0), pandas 3.0.2, sklearn 1.4.0, torch 2.11.0+cpu, onnx 1.21.0, TF[and-cuda] 2.21.0 (GPU confirmed True)**. Fixed torch (was GPU-dead `2.11.0+cu130` on a CUDA-12.2 driver) → `2.11.0+cpu`; hit a regression purging nvidia libs, **rolled back from a snapshot**, recovered. `requirements.txt` ML/scientific block re-pinned to truth with honest comments; `requirements-ml-training.txt` rewritten so it no longer re-pins the core stack (that divergence was the root cause).
- **A0.1b ⏳ deferred (tracked)** — validated purge of the orphaned `-cu13` stack (naive purge proven to break TF GPU via WSL loader fragility; needs `tensorflow[and-cuda]` force-reinstall first).
- **A0.2 ✅** `backend/alembic/versions/0034_ml_lineage.py` (rev `0034`, down `0033` — **not** `0029`; head was `0033`) + `MLModelMetadata.lineage` JSONB. Up/down round-trip verified on the real dev DB; left at head `0034`.
- **A0.3 ✅ (by discovery)** `app/ml/evaluation/` **already existed** (`metrics.py`/`gates.py`/`shap_validator.py`). No `__init__.py` created — clobber averted. A2/A3 add modules + extend exports additively; A3/A6 must reconcile with the existing `EvaluationGate`/`MetricsCalculator`.
- **A0.4 ✅** Prepended authoritative **Execution Amendments** to both plan docs; fixed load-bearing literals.
- **A0.5 ✅** Acceptance gate PASS (TF GPU True · torch 2.11.0+cpu · lineage JSONB present · existing evaluation API intact · ML pins == venv).
- **Follow-up ⏳ tracked** — non-ML prod-manifest drift (`cryptography`/`aiohttp`/missing prod deps): deliberately out of ML-A0 scope, its own audit.

**Net:** zero env-mutation risk introduced to the P0 chain; GPU training repaired; lineage column live. Original `0029`/skfolio/`evaluation/__init__.py (new)` lines above are superseded by this record + the Execution Amendments header.

### A1 — Fail-loud returns; kill the silent Sharpe→accuracy downgrade  ✅ **DONE & VERIFIED (2026-05-18)**
**As-built.** Files actually changed: `app/ml/training/exceptions.py` (new), `app/ml/training/checkpoint_manager.py`, `app/ml/training/evaluator.py`, `scripts/production_training_orchestrator.py`, `tests/unit/test_ml_failloud_a1.py` (new).

**Design refinement vs the original skeleton (important):** `evaluate()` does **not** raise unconditionally on `returns is None` — that would break the legitimate classification-only caller `scripts/test_complete_post_training.py` (verified: it reads only `.accuracy`). Correct contract implemented instead:
- **`evaluator.py`** — `EvaluationResults` financial fields → `Optional[float]/Optional[int]`; `returns=None` ⇒ fields are **`None` ("not computed"), never fabricated `0.0`**. `print_results` made `None`-safe (`_fin()` → "n/a (no returns)"). Fixed a **pre-existing latent bug** the tests exposed: `print_results` hardcoded a 3×3 SELL/HOLD/BUY confusion matrix that `IndexError`'d on the real binary 2×2 → corrected to DOWN/UP.
- **`checkpoint_manager.py`** — `SCHEMA_VERSION = 2`; `_MODEL_AFFECTING_KEYS = {n_features, sequence_length, include_fundamentals, model_version}`; `_enforce_compat(saved_state, current_config)` raises `StaleCheckpointError` on schema absence/mismatch **or** model-affecting drift; called in `__init__` resume path **before** `_warn_config_drift` (kept warn-only for operational keys); `schema_version` written in `_init_fresh`. `load_gru` now **raises** `StaleCheckpointError` on a missing mandatory `eval_r.npy` (was a silent `r=None` + a now-false warning); return type tightened to `np.ndarray`. → the **root-cause gate**: a stale pre-v2 checkpoint can no longer resume at all.
- **`orchestrator`** — deleted the silent Sharpe→accuracy fallback; hard `MissingReturnsError` in both `_create_and_optimize_ensemble` and `_evaluate_all_models` (defense-in-depth behind the schema gate); imported `MissingReturnsError`.
- **`exceptions.py`** — `StaleCheckpointError`, `MissingReturnsError` as plain `RuntimeError` subclasses (not `CortexBaseError` — training is CLI-domain; matches `QualityGateError` precedent) with fail-loud contract docstrings.

**Verified:** `tests/unit/test_ml_failloud_a1.py` **9/9 green** (fresh writes schema_version; matching checkpoint resumes; pre-v2 / schema-mismatch / model-affecting-drift all abort; non-model-affecting drift does NOT abort; None financials vs real floats; print_results None-safe). `py_compile` + import smoke clean; grep proves **zero** residual silent-fallback paths. `test_complete_post_training.py` verified unaffected.

### A2 — Real Combinatorial Purged CV that actually drives training/eval  ✅ **DONE & VERIFIED (2026-05-19)**
**As-built.** Files changed: `app/ml/evaluation/cpcv.py` (in-house, no skfolio), `app/ml/evaluation/__init__.py` (extended), `app/ml/training/walk_forward.py` (deprecated), `app/ml/training/checkpoint_manager.py` (SCHEMA_VERSION 2→3, cv_plan + cpcv_oof persistence), `scripts/production_training_orchestrator.py` (wired throughout), `tests/unit/test_ml_cpcv.py` (24/24 green).

**Key as-built decisions (skfolio skeleton above is void — see Execution Amendment #1):**
- `PanelPurgedCPCV` is fully in-house in `app/ml/evaluation/cpcv.py`: operates on the global unique-timestamp integer-ordinal axis; enforces label-horizon purge (`[t, t+h]` overlap → train row excluded) + embargo; yields `CPCVSplit` dataclass with `train_idx`, `test_idx`, `test_paths`, `test_path_of_row`, `combo_id`. `purged_holdout_split` for HPO. `axis_fingerprint` (SHA256 of unique-ts axis) guards resume reproducibility.
- Checkpoint `SCHEMA_VERSION=3`. `save_cv_plan / load_cv_plan` (replaced `save_splits / load_splits`). `save_cpcv_oof / load_cpcv_oof / has_cpcv_oof` — new: persists the 7-path OOF bundle (`step_5_xgboost/cpcv_oof/meta.json` + `path_NNN_{proba,y,fwd_ret,ts}.npy`, timestamps as int64 view of datetime64[ns]).
- Orchestrator: `_build_cv_plan` constructs the plan from the global unique-ts axis + persists it; `_train_xgboost_with_optimization` rewritten — HPO once on `purged_holdout_split` (stratified-random look-ahead leak RC-2 GONE), reference fit → `best_rounds`, 28-combo fixed-round refits via `XGBoostTrainer.fit_fixed` → 7-path OOF bundle; production model = all-data refit. GRU: per-symbol chronological purged split (NOT global CPCV — deferred to Workstream B). `WalkForwardSplitter` deprecated with `DeprecationWarning + stacklevel=2`.
- Acceptance tests: `TestNoLabelLeakage` (AR(1) rho=0.7, h=5: naive KFold AP > CPCV OOF AP by >0.5 pp) + property test (no train code in `[s−h, e+emb]` for every test run). 24/24 green.

### A3 — Cost-aware backtest + Deflated Sharpe + PBO (the promotion bar)  ✅ **DONE & VERIFIED (2026-05-19)**
**As-built.** Files created/changed: `app/ml/evaluation/backtest.py` (new), `app/ml/evaluation/deflated_sharpe.py` (new), `app/ml/evaluation/__init__.py` (extended), `app/ml/training/evaluator.py` (fixed + extended), `tests/unit/test_ml_backtest_a3.py` (new, 36/36 green).

**Key as-built:**
- **`backtest.py`**: `binary_to_positions(pred, mode)` — long_only: DOWN→0 (flat), UP→+1, HOLD(−1 sentinel)→0; long_short: DOWN→−1, UP→+1, HOLD→0. Fixes the HOLD-as-short bug (RC-1). `round_trip_charge_fraction` — delegates exactly to `estimate_round_trip_charges` (Decimal arithmetic), returns float fraction of entry notional; CNC ≈ 0.22%. `strategy_returns(pred, fwd_ret, *, mode, notional, product_type, entry_price, slippage_bps)` — gross − slippage_drag − charge_drag, output float32. `per_period_sharpe` (un-annualised, ddof=1, for DSR input) + `path_sharpe` (annualised, for reporting).
- **`deflated_sharpe.py`**: `deflated_sharpe_ratio(sr, n_obs, skew, kurt_raw, sr_std_across_trials, n_trials)` — Bailey & López de Prado (2014) Eq. 5+6 exactly; uses RAW kurtosis γ₄ (normal=3.0, `scipy.stats.kurtosis(x, fisher=False)`), Euler-Mascheroni γ≈0.5772; N=1 edge case → SR₀=0; degenerate var_factor → 0.0; n_obs<2 → NaN. `probability_of_backtest_overfitting(path_sharpes)` — fraction of CPCV paths with per-period SR ≤ 0 (PBO ≥ 0.5 = overfitting signal). `compute_dsr_and_pbo(cpcv_oof_paths, ...)` — full orchestration: proba→pred→strategy_returns→per-period SR per path→DSR on best path (worst-case selection bias) → PBO; returns dict with 12 keys.
- **`evaluator.py`** fixes: `calculate_financial_metrics` replaced — uses `binary_to_positions` via `_cost_aware_returns` (RC-1 fixed, flat not short); `calculate_classification_metrics` now computes `auc_pr` via `average_precision_score` when proba provided. `EvaluationResults` extended with 4 Optional A3 fields at end (default None): `auc_pr`, `deflated_sharpe`, `pbo`, `path_sharpes`.
- **Acceptance:** 36/36 green — position mapping, cost drag (exact delegation to charge_calculator), flat=zero P&L, DSR matches analytic formula to 1e-10, N=1 edge case, overfitting scenario DSR<0.3, SR₀ monotone in N, PBO corners (0/1/exact fractions), compute_dsr_and_pbo integration, AUC-PR wired in evaluator.

### A4 — Leakage-safe calibration + honest consumer  ✅ **DONE & VERIFIED (2026-05-19)**
**As-built.** Files changed: `app/ml/inference/calibrator.py` (docstring), `app/ml/training/xgboost_trainer.py` (removed leaking auto-fit; new `fit_calibrator_on_oof()`; fixed `n_estimators` param-pollution bug), `app/ml/training/checkpoint_manager.py` (new `save_calibrator / load_calibrator / has_calibrator`), `scripts/production_training_orchestrator.py` (OOF pool fit wired in step-5; checkpoint persist + resume; calibrator copied to Treelite dir in step-9), `app/ai/fusion/serializers.py` (comment clarified; no logic change), `pytest.ini` (xgboost C-callback filter), `tests/unit/test_ml_calibration_a4.py` (new, **30/30 green**).

**Root cause fixed (selection leakage):** `XGBoostTrainer.train()` previously called `self._fit_calibrator(X_val, y_val_idx)` on the HPO holdout validation set — the same data Optuna used for early-stopping and trial selection. The calibrator parameters were therefore fitted on a biased distribution where the model had been pre-selected for good performance, systematically under-estimating calibration error on live data.

**Fix:** Removed the auto-fit entirely from `train()`. Added `fit_calibrator_on_oof(oof_proba, oof_y)` — takes pre-computed P(UP) from all φ CPCV OOF paths (each panel row appears in exactly one path → full panel, zero repetition) and fits the Beta calibrator on that leakage-free pool. Called in the orchestrator immediately after the 28-combo OOF assembly, before the final all-data refit.

**Bonus fix (param pollution):** `train()` was passing `n_estimators` in the XGBoost params dict, causing a C-level UserWarning from every training call. Fixed by popping it before `xgb.train()` (same pattern already used in `fit_fixed()`).

**Checkpoint persistence:** `CheckpointManager` gains `save_calibrator("xgboost"|"gru", cal)` / `load_calibrator()` / `has_calibrator()` — persists the calibrator to `step_5_xgboost/calibrator_xgb.pkl` or `step_6_gru/calibrator_gru.pkl` immediately after the OOF fit for crash-resilience (resume path reloads and re-attaches without re-running CPCV).

**Serving path:** After Treelite compilation in step-9, calibrator is copied to `treelite_dir / "calibrator_xgb.pkl"` — the exact path `RegistryModelLoader._try_load_calibrator` looks for. `EnsemblePredictor` already applies it before the weighted average (no change needed).

**Serializer:** `confidence_score` stored in DB is already the post-calibration ensemble probability (EnsemblePredictor calibrates per-model before weighting). `calibrated_confidence` in the serialized payload correctly reflects this. The field alias is now honest because the calibrator is properly fitted and loaded. Added a precise comment explaining the flow.

**Acceptance:** 30/30 green — `TestTrainNoAutoCalibrator` (train() leaves calibrator None; model is trained); `TestFitCalibratorOnOOF` (returns fitted cal, sets self.calibrator, ECE improves, rejects small pool, rejects mismatched lengths, model not required); `TestCalibrationSetDisjoint` (OOF ∩ HPO val = ∅; OOF covers full test partition exactly once); `TestCheckpointCalibratorRoundTrip` (round-trip for both model types; correct paths; type-gating; independence); `TestECEImprovement` (ECE decreases at n=200/1K/5K; GRU temperature also improves; target <0.05 achievable); `TestSerialiserCalibrationFields` (fields present + typed correctly); `TestCalibratorContract` (deterministic; output sums to 1).

### A5 — Ensemble weighting on net Deflated Sharpe; reject non-accretive ensembles  ✅ **DONE & VERIFIED (2026-05-20)**
**As-built — diverged from the skeleton above on two methodology points (owner-confirmed before coding).**

The skeleton's "objective = mean DSR across CPCV paths" was rejected on principle: DSR is a *probability* whose selection-bias term is parameterised by a *discrete* `n_trials`, so embedding it in a *continuous* weight optimiser is ill-posed and numerically flat (DSR saturates → zero gradient). The skeleton also assumed both members expose CPCV-path-aligned OOF — but A2 deliberately deferred combinatorial CPCV for the sequence model to Workstream B (28 GRU refits on a 4 GB RTX 3050 is infeasible), so only XGBoost has a 7-path OOF; GRU has only its per-symbol purged-val OOF.

**Owner decisions (2026-05-20, AskUserQuestion-confirmed):**
- **Joint OOF basis** = GRU per-symbol purged-val rows ∩ XGBoost CPCV OOF, joined on `(symbol, timestamp)` — no extra GRU CPCV refits (defer that to B); checkpoint `SCHEMA_VERSION` bump 3→4 to persist the join keys.
- **Backtest** = `long_only` / `CNC` / 5 bps / 252 sessions — matches the live paper-trading product and the A3 defaults so the weighting bar equals the promotion bar.
- **Inner objective** = mean per-period NET-of-cost Sharpe across per-symbol paths + L2 prior to equal weights (smooth, convex, well-posed). **Promotion bar** = A3 net Deflated Sharpe via the single `compute_dsr_and_pbo` authority — deflation belongs at evaluation/promotion (A6), not inside the continuous weight loop.

**As-built (files):**
- `app/ml/training/ensemble_trainer.py` — full rewrite. New `EnsembleNotAccretiveError(RuntimeError)` carrying `recommended_weights / ensemble_net_dsr / best_standalone_net_dsr / best_standalone`. New `optimize_weights_on_oof(p_xgb, p_gru, fwd_ret, path_id, l2=0.10, grid_points=201, backtest=None)` — fail-loud input validation (row-alignment, finite probabilities in [0, 1], ≥ 2 paths, non-empty), deterministic dense 1-D grid + parabolic refine over `w_xgb ∈ [0, 1]` (the thresholded objective is piecewise-constant in w; gradient methods are the wrong tool), L2 prior to equal weights. Standalone + ensemble net DSR computed via the A3 authority on per-symbol paths. Accretion gate: `ensemble_dsr ≤ best_standalone_dsr + 1e-9` ⇒ raises with one-hot recommended weights. **Calibration-aware** `predict()` (per-model calibrate then weight, matching production EnsemblePredictor). Old raw `optimize_weights` / `optimize_ensemble_weights` removed (they were the RC).
- `app/ml/training/checkpoint_manager.py` — `SCHEMA_VERSION 3 → 4` (pre-v4 hard-fails via `_enforce_compat`). `save_cpcv_oof` / `load_cpcv_oof` carry per-row `symbol` (`<U20`). `save_gru_eval_arrays` / `load_gru_eval_arrays` carry row-aligned `eval_sym.npy` + `eval_ts.npy` (`save_*` raises on row-misalignment — refuses to persist a corrupt join key). `save_ensemble(weights, diagnostics=None)` + new `load_ensemble_diagnostics` (returns `{}` if absent — never silent-wrong).
- `scripts/production_training_orchestrator.py` — XGBoost panel builds `sym_all` parallel to `ts_all` and routes it through each CPCV OOF path dict. GRU val build captures `create_sequences` timestamps + symbol per row, applies the same VAL_CAP `keep` mask. `_train_xgboost_with_optimization` cpcv_oof path dict gains `"symbol"`. New `self.gru_eval_symbol` / `self.gru_eval_timestamp` attrs; resume of step-6-done reloads them via `load_gru_eval_arrays`. GRU calibrator is now checkpoint-persisted symmetric to XGBoost (`cp.save_calibrator("gru", …)` after step-6) and reloaded on resume — pre-A5 absence is logged loudly. `_create_and_optimize_ensemble` rewritten: fail-loud preconditions (returns/keys/cpcv_oof/calibrators), assembles calibrated XGB OOF P(UP), runs `gru_trainer.model.predict` on `gru_eval_X` for GRU OOF, joins via `pd.MultiIndex.get_indexer` on `(symbol, ts_i64)` (uniqueness asserted on the XGB side), enforces `a5_min_join_coverage` floor (`0.90`), `pd.factorize(symbol)` produces deterministic per-symbol `path_id`, calls `optimize_weights_on_oof`, catches `EnsembleNotAccretiveError` → audited one-hot weights + `accretive=False` diagnostic (designed outcome, never silent). Step-7 save/load round-trip the diagnostics. `_register_models_in_registry` injects A5 outputs into per-model registry metrics — `deflated_sharpe`, `pbo`, `ensemble_net_dsr`, `ensemble_accretive` — so the A6 promotion gate hard-checks the *honest* net DSR (not the in-sample step-8 Sharpe). New `TrainingConfig` knobs `a5_min_join_coverage=0.90`, `a5_l2_prior=0.10`.
- `app/ml/training/__init__.py` — exports `EnsembleNotAccretiveError`; drops `optimize_ensemble_weights`.

**Performance note (billion-dollar-app concern):** the inner objective hoists the Decimal `round_trip_charge_fraction` out of the 200-step grid (it is constant in w) and inlines a vectorised numpy expression mathematically equivalent to A3 `strategy_returns + per_period_sharpe` (the A3 36/36 suite stays green; A3 remains the single authority for the *official* DSR via the outer `_net_dsr` calls).

**Acceptance:** `tests/unit/test_ml_ensemble_a5.py` 15/15 green — weak-member collapses (xgb-informative AND gru-informative directions, `w_star ≥ 0.95` / `≤ 0.05`, no boundary artefact); accretion gate raises with correct one-hot recommendation and audited `accretive=False` diagnostic; net DSR strictly monotone in slippage (cost-aware verified); `predict()` applies the per-model calibrator; weights in `[0, 1]` and sum to 1; fail-loud on misaligned rows / non-probabilities / `< 2` paths / empty; `SCHEMA_VERSION == 4`; CPCV OOF round-trip preserves symbol; GRU sub-A round-trip preserves (symbol, timestamp); `save_gru_eval_arrays` rejects misaligned join keys; `save_ensemble` diagnostics round-trip; absent diagnostics yields `{}` (never silent fabrication). **A1 + A2 + A3 + A4 + A5 = 114/114 green** — zero regressions. graphify updated.

### A6 — QualityGate redesign (DSR/calibration/coverage = hard gates; AUC-PR primary)  ✅ **DONE & VERIFIED (2026-05-20)**
**As-built.** Files changed: `app/ml/model_registry.py` (QualityGate rewrite + BreakGlassError + ModelPromoter break-glass audit), `scripts/promote_model.py` (break-glass protocol + --reason flag + always-prompt production confirmation + A6 metrics in status display), `scripts/production_training_orchestrator.py` (ece_after written into xgb/gru metrics dict at step 10), `tests/unit/test_ml_quality_gate_a6.py` (new, 45/45 green).

**Key as-built decisions:**
- **8 hard gates** in `QualityGate.validate()` (ordered by priority): (1) auc_pr per-type floors [xgb 0.55 / gru 0.50 / tft 0.52], (2) deflated_sharpe strictly > 0.0 (absent → -1.0 fail-safe), (3) ece_after ≤ 0.05 (absent → 1.0 fail-safe), (4) symbol_coverage ≥ 0.85 when present / skip-with-warning when absent (A7 not yet wired), (5) training_samples ≥ 100K, (6) accuracy per-type sanity floor (secondary), (7) metadata_completeness, (8/9) AUC-PR + accuracy regression vs champion.
- **Sharpe soft-warning block removed** entirely. Deflated Sharpe (net-of-cost, A3) replaces it as a hard gate.
- **BreakGlassError(ValueError)** — `skip_quality_gates=True` without a non-empty `break_glass_reason` raises immediately; no bypass path exists without a documented reason. Audit entry emitted as CRITICAL log via `app.ml.model_registry.audit` logger.
- **promote_model.py break-glass**: `--skip-gates` requires `--reason`; validated before DB connection. Break-glass confirmation requires typing `CONFIRM` verbatim (not `y`). Standard production promotion always prompts `[y/N]`. The scheduled path (C1) calls ModelPromoter directly — no CLI flag involved.
- **ece_after cross-cutting touchpoint**: written into `xgb_metrics` / `gru_metrics` dicts in orchestrator step-10 so the gate can check it without a separate DB query. Absent calibrator → None stored (gate defaults to 1.0 fail-safe at validation time).
- **Acceptance**: `test_53pct_zero_dsr_blocked` — GRU 53 % accuracy / zero DSR fixture fails auc_pr + deflated_sharpe + calibration_ece (3 gates). `test_skipgates_requires_reason_and_prompt` — BreakGlassError on None / whitespace reason. 45/45 green. **A1–A6 = 159/159 green — zero regressions.**
**Files:** `backend/app/ml/model_registry.py` (31-133), `backend/scripts/promote_model.py` (275-280)
**Depends:** A3, A4, A5
**Change:** Promote net-DSR + calibration (ECE/Brier) + data-coverage to **hard** gates; primary classification gate = **AUC-PR**, not accuracy; add `"tft"` floor + a global "positive net-of-cost DSR & AP>random" floor; `--skip-gates` → audited break-glass that never skips human confirmation and is impossible in the scheduled path.

```python
# model_registry.py QualityGate.validate(): add to hard gates
ap   = float(metrics.get("auc_pr", 0.0))           # primary
dsr  = float(metrics.get("deflated_sharpe", 0.0))  # net of cost
ece  = float(metrics.get("ece_after", 1.0))
cov  = float(metrics.get("symbol_coverage", 0.0))
for name, val, thr, ok in [
    ("auc_pr", ap, self.MIN_AP_BY_TYPE.get(t, 0.55), ap >= ...),
    ("deflated_sharpe", dsr, self.MIN_DSR, dsr >= self.MIN_DSR),
    ("calibration_ece", ece, self.MAX_ECE, ece <= self.MAX_ECE),
    ("symbol_coverage", cov, self.MIN_COVERAGE, cov >= self.MIN_COVERAGE)]:
    checks[name] = {"value": val, "threshold": thr, "passed": ok}
    if not ok: failed[name] = f"{name} {val:.4f} fails {thr:.4f}"
# Sharpe is NO LONGER a soft warning — remove WARN-only block (lines 115-121)
```
**Acceptance:** `test_53pct_zero_dsr_blocked` — the fixture that passes today must now fail ≥3 hard gates; `test_skipgates_requires_reason_and_prompt`.

### A7 — Data integrity gate  ✅ **DONE & VERIFIED (2026-05-20)**
**As-built.** Files changed: `app/ml/training/exceptions.py` (new `DataCoverageError`), `scripts/production_training_orchestrator.py` (6 changes), `tests/unit/test_ml_data_integrity_a7.py` (new, **23/23 green**).

**Key as-built decisions:**
- **`DataCoverageError(RuntimeError)`** in `exceptions.py` — typed, fail-loud, plain RuntimeError subclass (matches `StaleCheckpointError` / `MissingReturnsError` convention). Two documented triggers: (1) symbol coverage below threshold; (2) zero training_samples after checkpoint resume.
- **`TrainingConfig.min_symbol_coverage = 0.85`** — configurable, 15 % slack for genuine listing/delistings; position in config block annotated with the 2551→1198 motivating incident.
- **`_assert_symbol_coverage(*, usable, requested)`** — private method called at the end of `_select_symbols_and_assess_quality` (fresh path only). Computes `coverage = usable / max(requested, 1)` (ZeroDivisionError-safe), sets `self._symbol_coverage` *before* raising (so callers can inspect it), logs both counts + thresholds, raises `DataCoverageError` with an actionable message including both raw counts and the resolution hint.
- **Step-1 resume path** — re-derives `self._symbol_coverage = len(self.symbols) / max(self.config.n_symbols, 1)` from the loaded symbol list. No checkpoint format change required; backward-compatible with all pre-A7 checkpoints.
- **Step-2 resume path** — after loading `features_meta`, checks `self._calculate_total_samples() <= 0` and raises `DataCoverageError("training_samples=0 …")`. Guard only fires on the `load_features_meta()` branch (both step_2 + step_3 done); the `load_features()` branch already populates `_total_samples_count` via `_rebuild_features_meta_from_raw`.
- **`_register_models_in_registry` (step 10)** — `symbol_coverage = self._symbol_coverage` injected into both `xgb_metrics` and `gru_metrics` immediately after `ece_after`. A6's QualityGate gate-4 is now a true hard gate (drops the provisional "skip-with-warning when absent" behaviour).
- **`_generate_data_quality_report`** — enriched from a 2-key stub to a 5-key report: `status`, `n_symbols`, `n_symbols_requested`, `symbol_coverage`, `coverage_threshold`.

**Acceptance:** 23/23 green — `DataCoverageError` is `RuntimeError` subclass + message preserved; `TrainingConfig.min_symbol_coverage` defaults 0.85, overridable, serialises to dict; `_assert_symbol_coverage` passes at threshold (≥ inclusive), raises one below, captures both raw counts in error message, stores coverage on instance even on raise, honours custom threshold, guards zero-requested; step-2 zero-rows corrupt checkpoint raises `DataCoverageError(match="training_samples=0")`; valid step-2 resume (total_rows=150K) does NOT raise; `_symbol_coverage` initialised to None before step-1; injected correctly into xgb/gru metrics dict; data quality report contains all 5 A7 fields; report coverage is None before step-1 (never fabricated). **A1–A7 = 182/182 green — zero regressions.**

### A8 — Registry consolidation (one authority + atomic projection)  ✅ **DONE & VERIFIED (2026-05-23)**
**As-built.** Files changed/deleted: `app/ml/model_registry.py` (`ModelPromoter` rewrite + `_project_to_ai_ml_models` helper + `RegistryDeprecatedError`); `app/ai/governance/unified_model_registry.py` (`promote_model`/`demote_model` raise Deprecated; read methods retained); `app/ai/governance/drift_detector.py` (`_handle_drift_action` rewritten — advisory flag only); `app/api/v1/governance.py` (`/models/{name}/promote` routes through `ModelPromoter`; `/models/{id}/state` is governance-table-only admin force-transition); **deleted** `app/ai/governance/model_registry.py` (orphan ungated path) and `tests/unit/test_model_registry_old.py` (pre-A8 orphan test of retired API + Option-A encryption — tests dir is `.gitignore`d so plain `rm`); `tests/unit/test_ml_registry_a8.py` (new, **28/28 green**).

**Key as-built decisions:**
- **`ModelPromoter` is the sole promotion authority.** `_project_to_ai_ml_models(session, model, ml_status)` updates `ai_ml_models.deployment_state` via SQL `UPDATE` **before** `session.commit()`, so an induced commit-or-projection failure rolls **both** tables back together (3 atomic-dual-write tests verify: `test_project_called_before_commit`, `test_commit_failure_triggers_rollback`, `test_project_failure_triggers_rollback`). State map = `{production→live, staging→paper, development→shadow}` (exposed as `_ML_TO_GOVERNANCE_STATE` for test introspection). `promote_to_staging` and `rollback` also project atomically through the same helper.
- **`RegistryDeprecatedError(RuntimeError)`** retires four paths in-place rather than deleting their bodies (preserves import surface during the migration). `ModelRegistry.promote_to_production` + `rollback_model` raise it; the storage-side `register_model`/`get_*`/`list_*` are unchanged and still serve orchestrator step-10. `UnifiedModelRegistry.promote_model` + `demote_model` raise it; the read methods `get_active_models`/`get_model`/`register_model` are retained and still serve the governance API list/summary queries.
- **`DriftDetector._handle_drift_action()` never mutates `deployment_state`** — it writes `governance_metadata["challenger_recommended"]=True` plus a `drift_recommendation` struct (`current_state`, `recommended_state`, `flagged_at`, `reason="drift_threshold_exceeded"`); demotion ladder = `{live→paper, paper→shadow, shadow→shadow, retired→retired}`; returns `"drift_flagged"`. C2 Promotion Report will surface the flag for human action via `promote_model.py`. The pre-A8 autonomous `live→…→retired` cascade is gone — the single-authority invariant cannot be silently violated by the drift loop.
- **Governance API**. `POST /governance/models/{name}/promote` resolves `AIMLModel.model_type → MLModelMetadata.model_name`, requires `target_state ∈ {paper, live}` (downgrades use `/state`), looks up the latest ML record in the required pre-promotion status (`paper`→development, `live`→staging), and delegates to `ModelPromoter` — A6 hard gates fire, atomic projection lands in the same transaction. `POST /governance/models/{id}/state` is the admin force-transition: governance-table-only, transition-graph validated (`shadow→paper`, `paper↔shadow`, `paper→live`, `live→shadow`, `retired→shadow`), `admin_overrides` audit entry appended to `governance_metadata` with `forced_by` email — **never** mutates `ml_model_metadata`. `scripts/promote_model.py` unchanged from A6 (break-glass `--reason` already enforced).
- **Test-infra gotcha worth keeping (non-obvious).** The "no module imports the deleted file" guard cannot use substring grep — the test itself mentions the module path as a string literal and would self-flag. Solution: parse each `.py` with `ast.parse` and walk for `ast.Import` / `ast.ImportFrom` nodes referencing the orphan (all three absolute import forms covered). pytest's `AssertionRewritingHook` instruments `ast.parse` so a naïve walk over 376 files cost **186 s in pytest vs 2.5 s standalone** — added a cheap substring pre-filter (`if "model_registry" not in text: continue`) so only the ~19 candidate files reach AST. Final cost: **AST test 1.4 s, A8 suite 28/28 in 2.35 s.**

**Acceptance:** `tests/unit/test_ml_registry_a8.py` 28/28 green across 8 classes — `TestSingleAuthority` (orphan deleted + AST grep-guard + module not importable), `TestModelRegistryDeprecated` (`promote_to_production`/`rollback_model` raise; `register_model` unaffected), `TestUnifiedModelRegistryDeprecated` (`promote_model`/`demote_model` raise; reads work), `TestAtomicDualWrite` (project-before-commit + both failure modes trigger rollback), `TestStateMapping` (all three states map correctly), `TestDriftAdvisoryFlag` (no state mutation, flags set, existing metadata preserved, commit called, return value, paper→shadow ladder), `TestRegistryDeprecatedError` (RuntimeError subclass, message preserved, catchable), `TestStagingAtomicProjection` (`promote_to_staging` also projects atomically with `staging`). **A1–A8 = 210/210 green in 14.22 s — zero regressions.** Workstream A (correctness/P0) chain is complete.

---

## Workstream B — GRU → TFT via pytorch-forecasting 🟠  ⏸ **PAUSED 2026-05-23**

> **Pause rationale (2026-05-23).** Empirical CPU benchmark on this dev box (Intel i7-12650H, 16 cores, torch 2.11.0+cpu) measured a small TFT (139K params, hidden=32, attn=4, batch=256) at the current GRU panel scale (200 symbols × 1500 days × 60-seq × 69-features) at **~13 minutes per epoch** — projecting 50-epoch single training ≈ 11 h, 15-trial × 30-epoch HPO ≈ 100 h (4 days), full 2551-sym universe ≈ 6 days/training. None survive a weekly retrain budget. Local-GPU path also rejected: RTX 3050 4GB has only **336 MiB free at idle** (TF 1.5GB + Windows ~2.2GB), WSL CUDA driver ceilings at 12.2, and the cu13 nvidia orphan stack from A0 is still present in the venv (A0.1b deferred) so adding torch+cu12 risks the same TF GPU regression that triggered the A0 rollback. **Owner decision**: ship calibrated-XGBoost-only (the plan's B3-documented fallback), proceed to Workstream C, revisit B when hardware story changes (cloud GPU or dedicated workstation). pytorch-forecasting 1.7.0 + lightning 2.6.4 installed cleanly (cascade-safe per `pip --dry-run`; TF GPU still functional post-install) and kept in place for future re-take; bench script `backend/scripts/bench_tft_cpu.py` retained for re-runs when hardware changes.

### B1 — `SequenceModelAdapter` boundary + torch isolation
**Files:** `backend/app/ml/models/sequence_adapter.py` (new Protocol), `backend/app/ml/models/tft/__init__.py` (new), CI guard script
**Depends:** A2
```python
# app/ml/models/sequence_adapter.py
from typing import Protocol, runtime_checkable
@runtime_checkable
class SequenceModelAdapter(Protocol):
    def fit(self, panel: "pd.DataFrame", cpcv) -> None: ...
    def predict_proba(self, panel) -> "np.ndarray": ...        # (n, 2)
    def export_onnx(self, path: "Path") -> None: ...
    def fit_calibrator(self, oof_y, oof_p) -> None: ...
    def save(self, path: "Path") -> None: ...
    @classmethod
    def load(cls, path: "Path") -> "SequenceModelAdapter": ...
# CI: scripts/check_torch_isolation.sh -> grep -rl '\btorch\b' app --include=*.py
#     | grep -v '^app/ml/models/tft/' && exit 1
```
**Acceptance:** `test_adapter_protocol_conformance` (stub passes `isinstance` check); CI torch-isolation job fails if `torch` leaks outside `tft/`.

### B2 — TFT trainer + crash-resilient checkpoint parity
**Files:** `backend/app/ml/models/tft/trainer.py` (new), `checkpoint_manager.py` (TFT sub-checkpoints), orchestrator step-6 (`_train_gru_with_optimization`→`_train_sequence_model`, resume branch 415-450, replace `EpochCheckpointCallback`)
**Depends:** B1
```python
# app/ml/models/tft/trainer.py
from pytorch_forecasting import TemporalFusionTransformer, TimeSeriesDataSet
import lightning.pytorch as pl

class TFTAdapter:  # implements SequenceModelAdapter
    def fit(self, panel, cpcv):
        ds = TimeSeriesDataSet(panel, time_idx="t", target="target",
              group_ids=["symbol"], max_encoder_length=60, max_prediction_length=1,
              time_varying_unknown_reals=FEATURES)               # 69 features
        self._tft = TemporalFusionTransformer.from_dataset(ds, loss=...)  # binary
        ckpt = pl.callbacks.ModelCheckpoint(dirpath=cp.tft_dir, save_last=True)
        bridge = _CheckpointManagerBridge(cp)        # mirrors GRU sub-C per-epoch
        pl.Trainer(callbacks=[ckpt, bridge], max_epochs=..., deterministic=True
                  ).fit(self._tft, ds.to_dataloader(train=True))
```
**Acceptance:** `test_tft_resume_after_kill` — SIGKILL mid-epoch, resume continues from last epoch (parity with GRU sub-C); seed-reproducibility test.

### B3 — TFT → ONNX export + serving parity (HIGH RISK — researched)
**Files:** `backend/app/ml/models/tft/export.py` (new), orchestrator `_export_models_to_onnx`, `backend/app/ml/inference/ensemble_predictor.py` (existing ONNX backend)
**Depends:** B2
**Change:** Web research confirms there is **no clean documented TFT→ONNX path** (LightningModule wrapper, dynamic seq, custom interpretable ops). Approach: extract the underlying `nn.Module`, export the **inference subgraph** with fixed encoder length and a documented supported-op subset, validate numerically, gate latency.
```python
# app/ml/models/tft/export.py
def export_onnx(tft, path: Path, seq_len=60, n_feat=69):
    core = tft.eval()                       # strip Lightning hooks
    dummy = {"encoder_cont": torch.zeros(1, seq_len, n_feat), ...}  # fixed shapes
    torch.onnx.export(core, (dummy,), path, opset_version=17,
        input_names=[...], output_names=["proba"], dynamic_axes=None)
    # parity gate: |onnx_proba - torch_proba| < 1e-4 on 1k samples  (else FAIL build)
```
**Acceptance:** `test_onnx_torch_parity` (<1e-4); CI perf job asserts p99 inference within the existing latency budget; serving smoke through `EnsemblePredictor`. **If parity/latency fails → ship calibrated-XGBoost-only (A5 path); do not force TFT.**

### B4 — TFT must earn ensemble inclusion
**Files:** A5 ensemble gate, A6 quality gate (config: add `tft` floors)
**Depends:** B3, A5, A6
**Change:** TFT enters the ensemble only if it passes A4 calibration + A6 gates and is accretive on net DSR (A5); else calibrated-XGBoost-only.
**Acceptance:** covered by A5/A6 fixtures with the real TFT adapter.

---

## Workstream C — Scheduled Self-Training + Human Gate 🟡 (depends on A8)

### C1 — Local scheduled orchestration  ✅ **DONE & VERIFIED (2026-05-23)**
**As-built — diverged from the original sketch on the scheduler-architecture decision (deliberately chosen for dev-box realism).**

The original snippet showed an in-process `BlockingScheduler` with a decorator-wired job — fine for code clarity, weak for a real dev box (no resumption after reboot, requires a babysitter process, no journal integration).  The as-built is a **hybrid**: systemd timer is the primary fire mechanism (oneshot service, OnCalendar, Persistent=true catches missed weekends), with APScheduler `BlockingScheduler` retained as a **fallback** for hosts without systemd (macOS, Docker, etc.).  Both invoke the same in-process core `run_one_challenger()` — there is no logic duplication.  The orchestrator itself is unchanged: the wrapper subprocesses `scripts/production_training_orchestrator.py --fresh` (matches the plan's "wrap, don't replace" rule and keeps the orchestrator's existing checkpoint contract intact).

**Files changed/created:**
- `backend/app/ml/config.py` — new `SCHEDULED_RETRAIN` dict (cron cadence + tz, window_mode `expanding|rolling`, lookback_years, lock_file, log_dir).  Single source of truth — the systemd timer's `OnCalendar` and the APScheduler `add_job` both read these values (the test suite enforces this hasn't drifted).
- `backend/scripts/scheduled_retrain.py` (new, ~210 LOC) — `run_one_challenger(*, dry_run, project_root)` core function: acquires a POSIX `fcntl.flock` (LOCK_EX|LOCK_NB) on the configured lock file (concurrent-run-impossible; OS auto-releases on process exit so no stale locks); subprocess-invokes `scripts/production_training_orchestrator.py --fresh` with `cwd=project_root`, capturing per-run stdout/stderr to `logs/scheduled_retrain/scheduled_retrain_<utc>.log`.  Three CLI modes: `--once` (default; systemd-timer entrypoint), `--schedule` (APScheduler BlockingScheduler for non-systemd hosts), `--dry-run` (prints plan, **no subprocess, no lock, no state mutation**).  Exit codes carry intent: 0=success/dry-run, 1=lock held (skipped), 2=orchestrator subprocess non-zero.  Has **no `--promote` flag** by design — a test enforces this invariant against future contributors.
- `deploy/cortex-retrain.service` (new) — Type=oneshot, ExecStart points to `.venv/bin/python scripts/scheduled_retrain.py --once`, TimeoutStartSec=24h (orchestrator runs are multi-hour), Nice=10 + IOSchedulingClass=idle + CPUSchedulingPolicy=batch (yields to interactive work on the shared dev box), StandardOutput=journal so the systemd journal is the always-on safety net even when the per-run log file is missing.
- `deploy/cortex-retrain.timer` (new) — OnCalendar=`Sat *-*-* 20:00:00 Asia/Kolkata` (mirrors `SCHEDULED_RETRAIN` exactly — drift between them would silently break the cadence so the test suite asserts they match), Persistent=true (missed Saturday → runs on next boot), RandomizedDelaySec=300 (5 min jitter).
- `deploy/README.md` (new) — install/enable/verify/dry-run/monitor/disable instructions for both systemd and non-systemd paths.
- `backend/tests/unit/test_ml_scheduled_c1.py` (new, **20/20 green**) — 6 test classes: (a) `TestDryRunNoSideEffects` — the plan's primary acceptance: dry-run never spawns the orchestrator, never creates the lock file, never creates the log dir; (b) `TestRealInvocation` — orchestrator IS subprocessed with `--fresh`, with the project-root as cwd, per-run log file IS created, subprocess non-zero → wrapper exit 2; (c) `TestConcurrencyLock` — second invocation while first holds the lock returns 1 without spawning, lock is released cleanly after a successful run; (d) `TestSchedulerWiring` — `BlockingScheduler` is constructed with the configured timezone and `add_job` receives the right cron kwargs + `max_instances=1` + `coalesce=True` (belt-and-braces with the file lock); (e) `TestCliSurface` — `--once` and `--schedule` are mutually exclusive, no `--promote` flag exists (the human-gate invariant defended); (f) `TestSystemdUnits` — both unit files + README exist, service invokes the right command + `Type=oneshot`, timer's `OnCalendar` line matches the config's day/hour/timezone + has `Persistent=true`.

**APScheduler dependency**: `pip install apscheduler` (verified safe by dry-run — +2 leaves only: `APScheduler 3.11.2` + `tzlocal 5.3.1`; zero upgrades to locked numpy/sklearn/torch/pandas; TF GPU still True post-install).

**Acceptance:** `tests/unit/test_ml_scheduled_c1.py` 20/20 green.  Live dry-run smoke (`python scripts/scheduled_retrain.py --dry-run`) prints the cadence/window/paths plan, confirms no subprocess invoked + no lock acquired + no state mutated, exits 0.  **A1–A8 + C3 + C1 = 256/256 green in 19 s — zero regressions.** Operator install path documented in `deploy/README.md`.

### C2 — Champion/Challenger + signed Promotion Report + immutable bundle  ✅ **DONE & VERIFIED (2026-05-24)**
**As-built — diverged from the original skeleton on storage and trigger; owner-confirmed via AskUserQuestion before coding.**

The original skeleton used MLflow for report storage. Owner chose **local JSON file only** (consistent with C3's pattern — no mlflow dep, trivially auditable, lives alongside C3 re-eval reports in `incident_reports/`). The skeleton also implied a standalone script; owner chose **auto-appended to orchestrator step 10** so every training run automatically produces a report with no operator ceremony. `--report-path` was confirmed as a mandatory hard gate on `promote_model.py production` — a BLOCKED report or a tampered bundle stops promotion dead.

**Architecture decisions locked before coding:**
- Two-layer bundle integrity: per-component SHA-256 hashes in `bundle_manifest` + top-level `bundle_sha256 = SHA-256(canonical JSON of manifest)`.
- Circular-hash avoidance: `report_body_no_hash` assembled with sentinel `bundle_sha256=""`, body hash computed on that, manifest built (includes body hash), `bundle_sha256` derived from manifest, sentinel replaced in final report. Verification mirrors this exactly via `_extract_body_for_hash`.
- `_POST_HASH_FIELDS = {"bundle_manifest", "report_path"}` — added to the written file after hash computation, stripped during verification so they never influence the digest.
- `_CANONICAL_SEP = (",", ":")` + `sort_keys=True` + `default=str` in all `json.dumps` calls — deterministic serialization, reproducible hashes on both write and verify sides.
- `PromotionStatus` enum: `BLOCKED | AWAITING_HUMAN_SIGNOFF`.
- `enforce_status` parameter: `True` on standard promote path (BLOCKED → `BlockedChallengerError`), `False` for break-glass (`--skip-gates`) — bundle still verified both ways.
- Non-fatal in orchestrator: models are already registered by step 10; a C2 failure logs loudly but never aborts the run. Models cannot be promoted without a valid report, but they are registered.

**Files changed/created:**
- `backend/app/ml/evaluation/promotion_report.py` — **new core module**. Private helpers: `_sha256_bytes`, `_sha256_canonical`, `_sha256_file`, `_compute_bundle_sha256`, `_build_bundle_manifest`, `_extract_body_for_hash`, `_calibrator_path_for` (A4 convention: `calibrator_{name[:3]}.pkl` next to ONNX dir), `_hash_artifact`, `_verify_artifact_group`, `_build_operator_guidance`, `_log_summary`. Public API: `build_promotion_report(challengers, champions, report_dir, run_id)` → writes signed JSON to `incident_reports/` and returns the report dict; `load_and_verify_report(path, *, enforce_status=True)` → 4-layer verify (kind → version → bundle integrity → status gate) → raises `BundleChecksumError` or `BlockedChallengerError` on failure. `BundleChecksumError` and `BlockedChallengerError` are both `ValueError` subclasses.
- `backend/app/ml/evaluation/__init__.py` — added 5 new exports: `PromotionStatus`, `BundleChecksumError`, `BlockedChallengerError`, `build_promotion_report`, `load_and_verify_report`.
- `backend/scripts/production_training_orchestrator.py` — C2 block injected after step 10 `mark_done` in `run()`; non-fatal `try/except Exception` with loud `logger.error` + `traceback.format_exc()`; new `_generate_promotion_report(self, onnx_paths)` async method (queries DB for just-registered challengers by version, fetches production champions with `status='production', is_active=True`, calls `build_promotion_report` with `report_dir=Path(__file__).parent.parent / "incident_reports"` and `run_id=self.cp.run_id`).
- `backend/scripts/promote_model.py` — `--report-path` added as a required argument to the `production` subparser; `_load_and_display_promotion_report(report_path, *, challenger_version, skip_gates)` helper displays rich CLI summary (bundle integrity ✅, status, run_id, per-model AUC-PR/DSR/PBO/ECE with Δ vs champion) and exits non-zero on any failure; `promote_to_production` updated to call it before any DB operations; `enforce_status = not skip_gates` wires break-glass correctly.
- `backend/tests/unit/test_ml_promotion_c2.py` — **42 tests** across 6 classes: `TestBuildPromotionReport` (14), `TestBundleManifest` (5), `TestBundleChecksum` (5), `TestLoadAndVerifyReport` (6), `TestBlockedChallengerCannotPromote` (4), `TestBundleIntegrityEndToEnd` (8). The last two classes are the plan's acceptance tests.

**Acceptance:** `test_blocked_challenger_cannot_promote` ✅ — BLOCKED report raises `BlockedChallengerError` on standard path, succeeds with `enforce_status=False` (break-glass); `test_bundle_integrity_end_to_end` ✅ — tamper `.onnx`, calibrator `.pkl`, report body, or `bundle_sha256` field → `BundleChecksumError` with all violations listed; missing inference artefact detected; multiple violations all reported in a single raise. **42/42 green; A1–A8 + C1 + C2 + C3 = 298/298 green in 14.69s — zero regressions.**

### C3 — Honest re-evaluation of current `1.0.0` + paper/shadow demotion  ✅ **DONE & VERIFIED (2026-05-23)**
**As-built — diverged from the original skeleton on one methodology point (owner-confirmed before coding).**

The original skeleton's "auto-demote inside the script" path was rejected on principle: demoting from a script that *also* writes the report co-locates two distinct concerns (audit + action) and bypasses the operator's eyes-on-glass moment. The owner explicitly chose **report-only**, with the operator applying the demotion via a dedicated CLI subcommand. The script's job is to surface the verdict + concrete next-step CLI invocations; the operator's job is to type them.

Additional discovery while gathering context: the shipped `1.0.0` is structurally obsolete on three independent axes (49-feature schema vs current 69 with fundamentals; no persisted calibrator — predates A4; stored `sharpe_ratio=0.0` with `n_trades=0` matches the A1-fixed fabricated-zero pattern). The script captures all three as **structural findings** alongside the formal A6 gate verdict so the incident report is informative even when the gates alone would be sufficient.

**Files changed/created:**
- `backend/app/ml/model_registry.py` — **new `ModelPromoter.demote_to_staging(model_version, reason)`**. Atomic dual-write mirror of `promote_to_production`: sets `ml_model_metadata.{status='staging', is_active=False}` and projects `ai_ml_models.deployment_state='paper'` via `_project_to_ai_ml_models` **before** commit. Reason required (empty/whitespace → `ValueError`); status guard rejects non-production. Audit-logged at WARNING via the dedicated audit logger (not CRITICAL — demotion is the safety system working as designed, not a bypass). Reused the A8 invariants verbatim: project-before-commit + commit-failure → rollback covers both writes.
- `backend/scripts/promote_model.py` — **new `demote` subcommand**. Mirrors `rollback`'s shape (resolve model, show current status, require `[y/N]` confirmation, dry-run mode); `--version` + `--reason` required. Routes to `ModelPromoter.demote_to_staging()`. CLI surface: `python scripts/promote_model.py demote --version <ver> --reason '<rationale>'`.
- `backend/scripts/reeval_production_model.py` — **new C3 script** (~280 LOC). `_audit_one_model(meta, qg)` combines two complementary signals — A6 `QualityGate.validate` verdict + structural findings (obsolete schema, missing calibrator, fabricated 0.0 Sharpe). `_expected_calibrator_path` mirrors A4's serving convention (`calibrator_{first3letters}.pkl` next to the inference artefact). `_build_recommendation` emits **concrete, copy-pasteable** `promote_model.py demote` invocations with the specific `--reason` enumerating the failing gates. Exit codes carry the verdict (0=meets bar / 1=op error / 2=demote recommended) so the shell can react. **REPORT-ONLY** — never touches a DB write path; verified by post-run state check.
- `backend/tests/unit/test_ml_registry_a8.py` — **+8 tests** for `demote_to_staging` (atomic call order, commit/project rollback paths, reason required, status guard, is_active cleared, audit-log emission, projection uses `staging` → `paper`).
- `backend/tests/unit/test_ml_reeval_c3.py` — **+18 tests** across 5 classes (`TestA6GateVerdict`, `TestStructuralFindings`, `TestExpectedCalibratorPath`, `TestBuildRecommendation`, `TestRunReevalE2E`). The end-to-end class mocks `_fetch_active_production` + the engine factory so the test never touches Postgres but exercises the full report orchestration.

**Empirical verdict (live DB, 2026-05-23):** `scripts/reeval_production_model.py` against the production registry returned **`DEMOTE_RECOMMENDED` (exit 2)**:
- `1.0.0_xgboost`: 3 hard-gate failures (`auc_pr`, `deflated_sharpe`, `calibration_ece`) + 3 structural findings (obsolete-schema, calibrator-missing, fabricated-zero-Sharpe).
- `1.0.0_gru`: identical pattern — 3 hard-gate failures + 3 structural findings.
- Stored metrics confirm the fabricated-zero pattern (`sharpe_ratio=0.0, sortino_ratio=0.0, win_rate=0.0, n_trades=0, total_return=0.0`) — exactly what A1 closed.
- Report written to `backend/incident_reports/reeval_1.0.0_20260523T131822Z.json` (~7 KB, well-formed). Post-run DB state check confirmed: both `ml_model_metadata` records still `production/is_active=True` and both `ai_ml_models` records still `deployment_state=live` — the script is provably side-effect free.

**Acceptance:** `tests/unit/test_ml_reeval_c3.py` 18/18 green + `test_ml_registry_a8.py::TestDemoteToStaging` 8/8 green. **A1–A8 + C3 = 236/236 green in 8.7 s — zero regressions.** Next operator action is documented in the live report itself.

---

## Workstream D — Drift Informs the Gate 🟡

### D1 — Advisory drift → next Promotion Report (no autonomous demotion)  ✅ **DONE & VERIFIED (2026-05-24)**
**As-built.** Files changed: `app/ai/governance/drift_detector.py` (full rewrite — 3-signal composite), `app/ml/monitoring/metrics.py` (5 new Prometheus gauges), `app/ml/evaluation/promotion_report.py` (`drift_advisory` param + operator guidance), `scripts/production_training_orchestrator.py` (`_generate_promotion_report` wires DB-fetched advisory), `tests/unit/test_ml_drift_d1.py` (new, **41/41 green**).

**Three-signal composite:**
1. **Distribution shift** (kept from A8) — z-score of `MLPrediction.prediction` vs `training_prediction_stats` baseline; capped at 10σ; zero-std guard uses explicit `is not None` checks (not `or` idiom).
2. **Realised directional accuracy** — fraction of `PaperTradeOutcome.ml_direction_correct=True` since `deployed_at`; triggers when live accuracy drops >10 pp below training baseline AND `≥ _MIN_TRADES_FOR_REALISED_METRICS (20)`.
3. **Realised net information ratio** — mean/std of per-trade net returns (`net_pnl / (entry_price × quantity)`) since `deployed_at`; triggers when IR ≤ −0.50 AND min-samples met; zero-notional rows skipped (no ZeroDivisionError).

Any single signal → `drift_detected=True` → `_handle_drift_action()` → `governance_metadata["challenger_recommended"]=True` + structured `drift_recommendation` with `triggered_signals` list.

**Prometheus gauges (D1, 5 new):** `ml_model_drift_score`, `ml_model_drift_flagged`, `ml_model_live_realised_sharpe`, `ml_model_live_directional_accuracy`, `ml_model_live_trade_count` — all labelled by `model_name`; emitted on every check via `_emit_prometheus()` (silently swallows import errors for environments without prometheus_client).

**C2 Promotion Report integration:** `build_promotion_report()` gains `drift_advisory: dict[str, Any] | None = None`. The advisory is always serialised as `{}` when absent (field stable across versions) and is part of the report body covered by the bundle SHA-256 — tampered advisory data produces a checksum failure. `_build_operator_guidance()` appends a `── DRIFT ADVISORY ──` block with copy-pasteable `promote_model.py demote` invocations when any champion has `challenger_recommended=True`. Orchestrator `_generate_promotion_report()` fetches `AIMLModel.governance_metadata` + latest `AIDriftReport` for each champion and builds `drift_advisory` before calling `build_promotion_report`.

**Invariants preserved:** `_handle_drift_action()` signature made backward compatible (`distribution_metrics: dict | None = None`) so A8's `TestDriftAdvisoryFlag` suite (8 tests) passes unchanged. A8 single-authority contract verified: `deployment_state` is never touched.

**Acceptance:** `tests/unit/test_ml_drift_d1.py` 41/41 green across 8 classes — `TestDriftFlagsNotDemotes` (6 — the plan's primary acceptance test), `TestDistributionShiftSignal` (6), `TestRealisedDirectionalAccuracy` (6), `TestRealisedNetInformationRatio` (4), `TestCompositeSignalLogic` (4), `TestPrometheusMetricsEmission` (4), `TestDriftAdvisoryInPromotionReport` (7), `TestBaselineFallback` (4). **A1–A8 + C1 + C2 + C3 + D1 = 339/339 green in 12.21s — zero regressions.**

---

## Workstream E — Reliability / Security / Observability 🟢 (cross-cutting, continuous)

### E1 — Defect-driven regression suite & CI gates ✅ DONE & VERIFIED (2026-05-24)
**Files:** `backend/tests/ml/__init__.py` (new), `backend/tests/ml/test_regression_e1.py` (new, 58 tests)
**Change:** One test per RC-1..RC-7 that fails on current `main`, passes after; DSR/PBO reference-vector unit tests; kill-9 resume parity proxy; bundle integrity end-to-end; inference latency budget (calibrator p99 < 5ms/1k, strategy_returns 100k < 500ms).
**Result:** 56/56 unit tests green + 2/2 performance tests green (58 total). All classes: TestRC1–RC7, TestDsrPboReferenceVectors, TestKillResumeParityProxy, TestBundleIntegrityEndToEnd, TestInferenceLatencyBudget.

### E2 — Bundle integrity, audited break-glass, key handling  ✅ **DONE & VERIFIED (2026-05-25)**
**As-built.** Files changed: `app/ml/model_registry.py` (`ArtifactManifestError`, E2 helpers, `register_model` gains `calibrator_path` param + manifest build, `load_calibrator_artifact`, `ModelPromoter.__init__` gains `audit_dir`, `_emit_break_glass_audit`), `scripts/promote_model.py` (`_AUDIT_DIR` constant, all 3 `ModelPromoter` calls pass `audit_dir`), `scripts/production_training_orchestrator.py` (derives calibrator paths, passes to `register_model`), `tests/unit/test_ml_integrity_e2.py` (new, **33/33 green**).

**Key as-built decisions:**
- **Option B — sha256_checksum_only**: per-model `artifact_manifest` JSONB stored in existing `lineage` column; covers training/inference/calibrator/feature_manifest components with SHA-256 + size + path. `_MANIFEST_SCHEMA_VERSION=1`, `_KEY_SCHEME="sha256_checksum_only"` (no encryption; integrity-only). `_sha256_bytes/_sha256_canonical/_hash_file/_build_artifact_manifest` private helpers.
- **Break-glass audit**: self-signed JSON written to `backend/audit/` on every bypass — `audit_sha256` computed on canonical JSON with sentinel `""` before signing; any post-write tamper is detectable by recomputation. `ModelPromoter(session, audit_dir=...)` optional; `promote_model.py` always passes `_AUDIT_DIR`.
- **`load_calibrator_artifact(model)`**: sha256=="absent" guard FIRST (calibrator registered as absent — valid state), then path None, then file existence, then recompute + compare; raises `ArtifactManifestError` on mismatch.
- **`SCHEMA_VERSION stays 4`**: manifest stored in `lineage` JSONB, not in checkpoint state — no checkpoint format change.

**Acceptance:** `tests/unit/test_ml_integrity_e2.py` 33/33 green across 4 classes: `TestArtifactManifestRegistered` (valid passes, body tamper raises, bundle_sha256 tamper raises, BLOCKED raises on standard, BLOCKED allowed on break-glass), `TestCalibratorChecksummed` (sha256==absent guard, path None, existence, recompute+compare), `TestBreakGlassAudited` (audit file created, audit_sha256 covered, no audit without audit_dir, CRITICAL log fires, audit_dir created if absent), `TestKeyHandlingVerification` (key scheme is sha256_checksum_only, constant exported, encrypted flag False, param accepted but ignored, different features → different manifest SHA, manifest schema_version present, registered_at parseable ISO datetime). **A1–A8 + C1 + C2 + C3 + D1 + E1 + E2 = 347/347 green — zero regressions.**

### E3 — Observability: MLflow lineage + structured run logs  ✅ **DONE & VERIFIED (2026-05-25)**
**As-built.** Files changed: `app/ml/training/checkpoint_manager.py` (`mlflow_run_id` property + `save_mlflow_run_id()`), `scripts/production_training_orchestrator.py` (module constants, instance attrs, 6 new E3 private methods, wired into all 10 pipeline steps), `app/ml/evaluation/promotion_report.py` (`mlflow_run_id` param + included in signed body), `pytest.ini` (pydantic warning filter). New file: `tests/unit/test_ml_lineage_e3.py` (**43/43 green**).

**Key as-built decisions:**
- **`mlflow-skinny 3.12.0`** — NOT full `mlflow`. Full mlflow requires `pandas<3` which conflicts with the locked `pandas 3.0.2` and would break TF GPU. `mlflow-skinny` has no pandas transitive dependency. ENV CONSTRAINT preserved.
- **`pytest.ini` filter** — `ignore::UserWarning:pydantic` added (mlflow-skinny 3.12 triggers a pydantic v2 namespace warning on import of `PromptModelConfig`; not our code; suppressed without touching error-mode config).
- **Two-layer observability**: (1) MLflow local file store at `backend/mlruns/` — experiment `"cortex_ml_training"`, `_STEP_INDEX` dict (10 steps → 1-based integers for per-step metric time series), all `TrainingConfig` fields logged as string params on fresh run, artifacts = training_config.json + promotion_report.json + run_log.ndjson. Browsable via `mlflow ui --backend-store-uri mlruns/`. (2) NDJSON run log at `{checkpoint_dir}/run_log.ndjson` — operator-parseable event stream independent of MLflow availability, one JSON object per line, appendable.
- **Resume invariant**: `mlflow_run_id` persisted in `CheckpointManager._state` (same crash-resilient layer as the rest of checkpoint state). On resume `mlflow.start_run(run_id=stored_id)` reconnects — no duplicate runs, no lost metric history. `SCHEMA_VERSION stays 4` (mlflow_run_id is not a model-affecting key — backward-compatible with existing checkpoints).
- **Non-fatal design**: every MLflow call wrapped in `try/except Exception`. A broken MLflow install or any MLflow API error NEVER aborts training. The NDJSON log is written independently.
- **C2 Promotion Report integration**: `mlflow_run_id` added to `report_body_no_hash` (included BEFORE the bundle SHA-256 is computed) → tamper-detectable; empty string `""` when unavailable (field always present).
- **6 new orchestrator methods**: `_setup_mlflow()` (start/resume run, log params, write run_start log entry), `_log_mlflow_step_done(step, dur_s, metrics)` (duration metric + optional numerics at correct step index, structured log), `_log_mlflow_eval_metrics(eval_results)` (step-8 metrics per model), `_log_mlflow_registration_metrics(xgb, gru)` (step-10 metrics including accretion/coverage), `_finalize_mlflow(status, report_path)` (status tag, artifact logging, `end_run`), `_write_run_log_entry(entry)` (ISO-8601 timestamp prepend + NDJSON append).

**Acceptance:** `tests/unit/test_ml_lineage_e3.py` 43/43 green across 7 classes: `TestCheckpointMLflowRunId` (6), `TestSetupMLflow` (7), `TestStructuredRunLog` (6), `TestLogMLflowStepDone` (6), `TestFinalizeMLflow` (8), `TestPromotionReportMLflowRunId` (5), `TestMLflowExperimentConstants` (5). **E3 suite: 43/43; full ML suite: 194/194 — zero regressions.**

---

## Workstream F — Event-Driven Backtest 🟢 (after A–C; final-validation fidelity)

### F1 — Event-driven engine behind the CPCV gate  ✅ **DONE & VERIFIED (2026-05-25)**
**As-built.** Files created/changed: `app/ml/evaluation/event_backtest.py` (new), `app/ml/evaluation/__init__.py` (extended), `app/ml/evaluation/promotion_report.py` (`event_backtest` param), `scripts/production_training_orchestrator.py` (F1 block + `_run_event_backtest_f1()` method), `tests/unit/test_ml_event_backtest_f1.py` (new, **46/46 green**).

**Key as-built decisions (owner-confirmed via AskUserQuestion before coding):**
- **1-bar execution latency**: signal at bar T → fill at bar T+1 (realistic for NSE CNC end-of-day). Implemented as array slicing: `eff_pos = positions[:n-latency_bars]` / `eff_fwd_ret = fwd_ret[latency_bars:]`.
- **Per-symbol independent simulation**: each symbol's OOF rows extracted, sorted by timestamp (`np.argsort` stable), simulated independently, results concatenated for path-level Sharpe — matching `compute_dsr_and_pbo`'s aggregation boundary.
- **No partial fills**: all orders fully filled at reference entry price.
- **20% relative tolerance agreement check**: `|event_mean_pp_sr − vec_mean_pp_sr| / max(|vec_mean_pp_sr|, 0.01) ≤ 0.20` → `PASS`; else `WARNING`. Non-fatal — informational fidelity check only, not a promotion blocker.
- **Vectorized charge coefficients**: `buy_coeff` / `sell_coeff` derived once from `calculate_charges("BUY"/"SELL", product_type, ₹1000, qty=1)` (exact Decimal arithmetic), cached per product type in `_COEFF_CACHE`. Hot path is pure NumPy — no Decimal per fill. Coefficients auto-propagate if charge schedule changes. NSE CNC: `buy_coeff ≈ 0.001236`, `sell_coeff ≈ 0.001036`.
- **Vectorized charge formula**: LONG = `notional_ratio × (buy_coeff + sell_coeff × (1+ret))`; SHORT = `notional_ratio × (sell_coeff + buy_coeff × (1+ret))`. Entry+exit legs on same notional base.
- **Exit price**: `max(entry × (1 + fwd_ret), entry × 0.001)` — floor guards extreme-loss numerical stability.
- **C2 integration**: `event_backtest` added to `report_body_no_hash` in `build_promotion_report()` BEFORE the bundle SHA-256 is computed → cryptographically covered. Field is always `{}` when absent (never missing). Non-fatal in orchestrator: F1 block wrapped in `try/except Exception`, failure logs loudly but never aborts the run.

**Data contracts (CPCV OOF path dict keys):** `proba: float32[]`, `forward_return: float32[]`, `timestamp: datetime64[ns][]`, `symbol: <U20[]` — all present in A5+ checkpoint format.

**Public API:**
- `SimulatedFill` — `frozen=True, slots=True` dataclass: `bar_signal, bar_entry, direction, entry_price, exit_price, quantity, gross_return, charge_drag, slippage_drag, net_return`.
- `PathSimulationResult` — `frozen=True, slots=True` dataclass: `n_bars, n_fills, fills, net_returns, gross_returns, net_sharpe_per_period, net_sharpe_annualised, gross_sharpe_annualised, total_return, max_drawdown, win_rate`.
- `EventBacktestReport` — mutable dataclass with per-path stats, aggregated stats, agreement fields (`vectorized_mean_sharpe_per_period`, `sharpe_divergence_absolute`, `sharpe_divergence_relative_pct`, `agreement_status` ∈ `{"PASS","WARNING","INSUFFICIENT_DATA"}`), and config fields. `.as_dict()` returns a JSON-serialisable `dict[str, Any]`.
- `run_event_backtest(cpcv_oof_paths, *, vectorized_path_sharpes_per_period=None, latency_bars=1, mode="long_only", notional=100_000.0, product_type="CNC", entry_price=1_000.0, slippage_bps=5.0, periods_per_year=252, risk_free_rate_annual=0.05, agreement_tolerance=0.20) → EventBacktestReport`.

**Acceptance:** `tests/unit/test_ml_event_backtest_f1.py` 46/46 green across 9 test classes — `TestSimulatedFillAccounting` (8: coefficient derivation, CNC buy>sell, coefficient-vs-calculate_charges at rtol=1e-6 [at qty=1 = derivation reference], long/short charge positive, exit price floor, slippage magnitude, net=gross−charge−slippage), `TestLatencyShift` (5: zero/one/k/latency_eq_length/one-bar-captures-next-bar), `TestNetReturnStream` (4: all-down-long-only-zero, net<gross-on-active, float64 output, negative-fwd-gives-negative-gross), `TestPerSymbolGrouping` (4: single-symbol with/without meta matches, groups by symbol independently, stable timestamp sort within symbol, fallback without symbol metadata), `TestPathSimulation` (5: all-flat→no-fills, win-rate ∈ [0,1], max-drawdown ≥ 0, total-return consistent with net-returns, fills-tuple-length = n_fills), `TestAgreementCheck` (5: PASS within tolerance, WARNING beyond, INSUFFICIENT_DATA on no reference, INSUFFICIENT_DATA on empty list, near-zero-vec-sharpe-uses-floor), `TestRunEventBacktest` (8: returns report, empty paths raises, negative latency raises, n_paths matches, single-obs path skipped with warning, all-too-small raises, per-path stats lengths match, total fills = sum), `TestAcceptance` (3: zero-latency <5% divergence from A3 vectorized [engine correctness proof], one-bar latency agreement correctly computed PASS-or-WARNING, report fields complete), `TestAsDictSerialisation` (4: JSON-serialisable, roundtrips through json.dumps/loads, event_backtest field always present in promotion report, event_backtest={} when None).

**Test design fix (non-obvious — save the lesson):** coefficient verification test uses `qty=1` (the derivation reference) not `qty=100`. Reason: GST in `calculate_charges` compounds on the Decimal base — at qty=1, GST on exchange+SEBI rounds to 0.0055 (4dp), scaling to qty=100 gives 0.55 ≠ 0.5526 (≈2.2e-5 relative error). Verifying at qty=1 keeps `rtol=1e-6` honest (only float64 conversion error). Loosening the tolerance would be a band-aid masking a known but irrelevant quantization; the actual trading error is sub-cent.

**Full ML suite after F1:** `tests/unit/test_ml_backtest_a3.py + test_ml_calibration_a4.py + test_ml_cpcv.py + test_ml_data_integrity_a7.py + test_ml_drift_d1.py + test_ml_ensemble_a5.py + test_ml_event_backtest_f1.py + test_ml_failloud_a1.py + test_ml_integrity_e2.py + test_ml_lineage_e3.py + test_ml_promotion_c2.py + test_ml_quality_gate_a6.py + test_ml_reeval_c3.py + test_ml_registry_a8.py + test_ml_scheduled_c1.py` = **461/461 green in 18.64 s — zero regressions.**

**Operator actions still pending (pre-existing, separate from F1):**
- Apply C3 demotion: `python scripts/promote_model.py demote --version 1.0.0_xgboost --reason '...'` and same for `1.0.0_gru` (details in `backend/incident_reports/reeval_1.0.0_20260523T131822Z.json`).
- Install systemd timer: `sudo cp deploy/cortex-retrain.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now cortex-retrain.timer` (documented in `deploy/README.md`).

---

## Sequencing (critical path)

`A0→A1→A2→A3→A4→A5→A6` sequential. `A7` parallels A1+. `A8` parallels A3–A7, **gates C**. `B1` after A2; `B2→B3→B4`; `B4` needs A5/A6. `C1` after A1; `C2` needs A8+C1; `C3` needs A3/A4/A8. `D1` after A8 (parallel C2). `E` continuous from P0. `F1` after A–C. No phase starts before the prior phase's acceptance tests are green.

## Sources
- [skfolio CombinatorialPurgedCV API](https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html) · [skfolio model selection](https://skfolio.org/user_guide/model_selection.html)
- [pytorch-forecasting TFT](https://pytorch-forecasting.readthedocs.io/en/latest/api/pytorch_forecasting.models.temporal_fusion_transformer.html) · [torch.onnx](https://docs.pytorch.org/docs/stable/onnx.html)
- [Deflated Sharpe / backtest overfitting (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID4686376_code4361537.pdf?abstractid=4686376&mirid=1) · [Purging/Embargo — QuantInsti](https://blog.quantinsti.com/cross-validation-embargo-purging-combinatorial/)
- [Champion/Challenger — Snowflake](https://www.snowflake.com/en/developers/guides/ml-champion-challenger-model-deployment/) · [MLOps Principles](https://ml-ops.org/content/mlops-principles)
```
