# Cortex ML — Full Implementation Plan

**Date:** 2026-05-17
**Status:** Implementation plan (no code changed)
**Predecessors:** `ML_AUDIT_REPORT.md` → `ML_REMEDIATION_PLAN.md` → *this document*
**Basis:** Direct reads of the actual source (orchestrator, checkpoint manager, evaluator, ensemble trainer, registry ×3, promoter, walk-forward, target generator, calibrator, ensemble predictor, ORM) + 2026 SOTA review.

> ## ⚠️ EXECUTION AMENDMENTS — 2026-05-18 (AUTHORITATIVE — supersede any conflicting text below, incl. §3 tooling table & Sources)
> Discovered during A0 execution. Where this conflicts with anything below, **this wins**. (Full task-level detail: `ML_IMPLEMENTATION_TASKS.md` top.)
> 1. **CPCV/DSR/PBO/backtest are ALL in-house** (`app/ml/evaluation/`). **skfolio and mlfinlab are NOT dependencies** — skfolio forces numpy 1.26→2.4 / sklearn 1.4→1.8 which breaks TF 2.21 GPU. The §3 "skfolio" row and the skfolio/mlfinlab Sources are void.
> 2. **PyTorch is already first-class** (FinBERT/`nlp_engine.py`, `tuner.py`, `trainer.py`, `onnx_converter.py`). The "torch confined to `app/ml/models/tft/`" hard boundary + CI import guard are **infeasible/void**; the real boundary is the `SequenceModelAdapter` **API**. `torch` on this box is **CPU-only** (driver CUDA ceiling 12.2; GPU reserved for TF 2.21).
> 3. **A0 added no new heavy deps.** Lineage = migration `0034` (done). Env re-pinned to verified reality: numpy 1.26.4 (<2.0, hard TF 2.21 constraint), pandas 3.0.2, sklearn 1.4.0, torch 2.11.0+cpu, onnx 1.21.0. TFT-compute (Workstream B) gated on a CPU-benchmark investigation.

### Locked decisions (owner-confirmed)

| # | Decision | Choice |
|---|---|---|
| 1 | Sequence model | Replace GRU with **TFT via `pytorch-forecasting`** (PyTorch Lightning) |
| 2 | Autonomy | **Scheduled retrain + mandatory human approval gate**; champion/challenger; no auto-promotion |
| 3 | Compute | **Local dev-box training only**, no cloud; validated bundle shipped to prod |
| 4 | Backtest bar | Cost-aware vectorized + **CPCV/Deflated-Sharpe** now; event-driven engine later (recommended & justified, §A3) |
| 5 | TFT framework | **`pytorch-forecasting`** — accept a 2nd DL runtime, isolated behind an adapter |
| 6 | Registry triplication | **Consolidate in P0** — one authority, one projection |
| 7 | Current `1.0.0` model | **Honest re-eval in P0; auto-demote to paper/shadow if net-of-cost DSR fails** |

---

## 1. Corrected Root-Cause Ledger

Deeper reading corrected several claims from the remediation plan. The implementation must target the *actual* defects:

| ID | Defect (precise, code-grounded) | Evidence |
|---|---|---|
| **RC-1** | Forward returns **are already persisted** (`eval_r.npy`). The defect is a **silent downgrade**: stale checkpoint → `gru_eval_returns=None` → orchestrator falls back Sharpe→accuracy and continues; evaluator returns 0.0 instead of raising. | `checkpoint_manager.py:445,531-559`; `production_training_orchestrator.py:1321-1326`; `evaluator.py:86-101` |
| **RC-2** | Walk-forward is **decorative**. Splits are built from a **dummy date range**; `_train_xgboost_with_optimization` **ignores `splits`** and uses a **stratified *random*** `train_test_split` (look-ahead leakage on time series); GRU uses a `TRAIN_FRAC` slice. No purge, no embargo, against an **h=5 overlapping** label. | `production_training_orchestrator.py:778-781,840-849,993-1006`; `walk_forward.py` (no purge/embargo); `target_generator.py:243` |
| **RC-3** | Backtest correctness: HOLD(`-1`) & DOWN(`0`) both treated as SHORT; **no transaction costs** though `charge_calculator` exists; trades every bar; overlapping-window Sharpe inflation. | `evaluator.py:234-287`; `ensemble_trainer.py:85`; `app/services/paper_trading/charge_calculator.py` |
| **RC-4** | Calibration **exists and is good** (`ConfidenceCalibrator`: Beta/XGBoost, Temperature/GRU; XGBoost auto-fits it). Real defects: (a) fit on the **early-stopping val set** (selection leakage), (b) **fusion layer ignores it** — `serializers.py:140` aliases raw confidence as "calibrated_confidence", (c) calibrator **not integrity-checksummed** in the registry. | `calibrator.py:15-16`; `xgboost_trainer.py:125`; `app/ai/fusion/serializers.py:140`; `model_registry.py:330-344` |
| **RC-5** | Sharpe is a **soft warning only**; per-type floors let a 53% model pass; `--skip-gates` bypasses gates **and** the human confirmation. | `model_registry.py:53,115-121`; `promote_model.py:275-280` |
| **RC-6** | Data attrition (2551→1198) ungated; resume path leaves `training_samples=0`; `_warn_config_drift` only warns when model-affecting keys (e.g. `n_features` 49→69) change. | training results JSON; `production_training_orchestrator.py:430`; `checkpoint_manager.py:615-629` |
| **RC-7** | **Three** registries / **two** tables / **two** state machines + a legacy duplicate; ungated promotion paths exist. | `app/ml/model_registry.py` (`ml_model_metadata`, dev→prod); `app/ai/governance/unified_model_registry.py` + legacy `app/ai/governance/model_registry.py` (`ai_ml_models`, shadow→live) |

---

## 2. Target Architecture & Boundaries

```
┌── Training (local dev box) ─────────────────────────────────────────────┐
│  Data → Features(69) → Targets(h=5, fwd_return) → CPCV splitter         │
│        (skfolio CombinatorialPurgedCV, timestamp purge+embargo)         │
│   ┌────────────┐                 ┌──────────────────────────────┐       │
│   │ XGBoost    │                 │ TFT (pytorch-forecasting)    │       │
│   │ (Keras-    │                 │ behind SequenceModelAdapter  │       │
│   │  free)     │                 │  — Lightning ckpt, ONNX      │       │
│   └─────┬──────┘                 └───────────────┬──────────────┘       │
│         │ OOF probs (per CPCV path)              │ OOF probs            │
│         └───────────────┬───────────────────────┘                      │
│                         ▼                                              │
│   Calibration (ConfidenceCalibrator, fit on CPCV OOF — leakage-safe)   │
│                         ▼                                              │
│   Ensemble weight optimiser (objective = Deflated Sharpe, CV-mean,     │
│                              L2 prior, must beat best standalone)      │
│                         ▼                                              │
│   Cost-aware backtest (charge_calculator) → DSR · PBO · ECE · Brier    │
│                         ▼                                              │
│   QualityGate (HARD: net-of-cost DSR, calibration, accuracy, samples, │
│                non-degradation, data-coverage)                         │
│                         ▼                                              │
│   Immutable bundle {artifacts+calibrators+manifest+lineage+report}    │
│                         ▼  human-signed Promotion Report               │
└─────────────────────────┼──────────────────────────────────────────────┘
                          ▼  ship bundle → prod box
        ModelPromoter (single authority) → ml_model_metadata
                          ║ (atomic projection)
                          ▼
        ai_ml_models governance row  ◄── DriftDetector (informs only)
```

**Hard boundary:** PyTorch is confined to `app/ml/models/tft/` behind a `SequenceModelAdapter` Protocol. XGBoost path, serving, fusion, and the orchestrator depend only on the adapter — never on `torch` directly. Serving keeps ONNX Runtime (TFT exported to ONNX); `torch` is **not** a serving runtime dependency.

---

## 3. Tooling & Dependencies (decisions, with rationale)

| Need | Choice | Rationale / license |
|---|---|---|
| CPCV splitter | **`skfolio.model_selection.CombinatorialPurgedCV`** | BSD-3, scikit-learn-compatible, maintained. `mlfinlab` rejected: paid tiers = unacceptable supply-chain/licensing risk for a billion-dollar app |
| Deflated Sharpe Ratio, PBO | **Implement in-house** (`app/ml/evaluation/deflated_sharpe.py`) | ~40 LOC each from López de Prado's closed forms; avoids a paid dep; fully testable against published reference values |
| Sequence model | **`pytorch-forecasting` TFT** + PyTorch Lightning | Owner-confirmed; battle-tested reference impl; isolated behind adapter |
| Calibration | **Reuse existing `ConfidenceCalibrator`** | Already SOTA-grade (Kull 2017 / Guo 2017); do not rebuild — only change *where* it is fit and *who* consumes it |
| Cost model | **Reuse `charge_calculator`** | Production NSE FY25-26 schedule already used by paper trading → backtest/live cost parity |
| Scheduler | **APScheduler** (in-proc) invoked by **systemd timer** on dev box | Local-only constraint; no Airflow/K8s (would be over-engineering) |
| Experiment tracking | **MLflow local file store** (`./mlruns`) | No server, no cloud; right-sized lineage/metric history |

Add to `requirements.txt` with pinned versions (exact pins set at implementation time, compatibility-tested in P0-T0): `skfolio`, `pytorch-forecasting`, `lightning`, `torch` (CPU build acceptable for dev-box training of this data scale; GPU optional), `mlflow`, `apscheduler`. **No `mlfinlab`.**

---

## 4. Workstreams → Tasks

Each task: **Files** · **Change** · **Test that would have caught the bug** · **Exit**.
Severity: 🔴 P0 (correctness/safety, blocks all) · 🟠 P1 (TFT) · 🟡 P2 (self-train) · 🟢 P3 (hardening).

### Workstream A — Correctness & Trustworthy Evaluation 🔴

**A0 — Dependency & schema bring-up**
- Files: `requirements.txt`, new `app/ml/evaluation/__init__.py`.
- Change: pin/install deps; compatibility matrix test (torch/lightning/pytorch-forecasting/skfolio import + tiny smoke). One Alembic migration adding `MLModelMetadata.lineage JSONB` (git commit, data-window, config hash, calibrator checksum, CPCV/DSR/PBO) and `validation_metrics` population contract.
- Test: CI import-smoke; migration up/down round-trip on a scratch DB.
- Exit: clean env builds; migration reversible.

**A1 — Fail-loud returns & no silent downgrade**
- Files: `production_training_orchestrator.py:1321-1326`; `evaluator.py:63-101`; `checkpoint_manager.py` (add `SCHEMA_VERSION`, `StaleCheckpointError`).
- Change: delete the Sharpe→accuracy fallback; raise `StaleCheckpointError` when a Sharpe-configured run lacks `eval_r.npy`. `evaluator.evaluate(...)` raises `MissingReturnsError` if a financial metric is requested with `returns=None` (no 0.0 default). Add `checkpoint.json.schema_version`; on mismatch or on drift of **model-affecting** config keys (`n_features`, `sequence_length`, `include_fundamentals`, `model_version`) **hard-fail** with a remediation message (escalate `_warn_config_drift` from warn→raise for that key subset only).
- Test: stripped-`eval_r` checkpoint ⇒ run aborts loudly (asserts no 0.0-Sharpe "success"); config-key drift ⇒ raises.
- Exit: no code path yields financial metrics of 0.0 silently; stale/incompatible checkpoint cannot resume.

**A2 — Real purged CV that actually drives training/eval (kills RC-2)**
- Files: new `app/ml/evaluation/cpcv.py`; `walk_forward.py` (keep `Split` dataclass; deprecate dummy-date splitter); `production_training_orchestrator.py:_setup_walk_forward_validation`, `_train_xgboost_with_optimization`, `_train_gru_with_optimization`(→TFT), `_create_and_optimize_ensemble`, `_evaluate_all_models`.
- Change: build CPCV folds on **real timestamps** with **purge = label horizon h(5) + embargo** (timestamp-based, not row-index — note dead-zone dropping makes rows non-contiguous, see `target_generator.py:260-262`). Replace the stratified random `train_test_split` and `TRAIN_FRAC` slice with CPCV fold indices. All of HPO, ensemble weighting, calibration, and evaluation consume the same CPCV path partition. Persist fold definitions in the step-4 checkpoint (extend `save_splits`/`load_splits`).
- Test: leakage probe — inject a future-peeking feature; honest CPCV must collapse its OOF edge to ~0 (the current random split would not). Property test: no train timestamp within `[test_start − h − embargo, test_end + embargo]`.
- Exit: training/eval provably leakage-safe; "walk-forward" is no longer decorative.

**A3 — Cost-aware backtest + DSR/PBO (the promotion bar)**
- Files: new `app/ml/evaluation/backtest.py`, `deflated_sharpe.py`; `evaluator.py:218-287` (replace `calculate_financial_metrics`).
- Change: positions ∈ {long, flat, short} — **abstentions are flat, not short** (fixes RC-3). Per round-trip cost via `estimate_round_trip_charges` at a configured representative notional → return-space drag (`total_drag / notional`); slippage parameter. Non-overlapping / block trade accounting honoring h=5 (no overlapping-window Sharpe inflation). Compute Sharpe **distribution across CPCV paths** → Deflated Sharpe Ratio (corrects for trials/skew/kurtosis) and PBO. Recommendation recorded here: **cost-aware CPCV+DSR is the promotion gate now; event-driven engine is P3 (§F)** — building event-driven first would delay trustworthy selection for marginal selection-stage fidelity (over-engineering at this stage).
- Test: DSR/PBO unit tests vs published reference vectors; cost test asserts net Sharpe < gross by the exact `charge_calculator` drag at a known notional; HOLD-as-flat regression test.
- Exit: every model reports cost-inclusive DSR + PBO over a path distribution; reference-validated.

**A4 — Leakage-safe calibration + honest consumer (kills RC-4)**
- Files: `calibrator.py` (fit contract: OOF only); `xgboost_trainer.py:125`; TFT adapter (temperature on TFT logits); `app/ai/fusion/serializers.py:140`; `model_registry.register_model` (checksum coverage).
- Change: fit calibrators on **CPCV out-of-fold predictions**, never the early-stopping val set. `serializers.py:140` must load and apply the persisted calibrator (per model_version) — `calibrated_confidence` becomes the *actual* calibrated value; raw stays separate. Calibrator artifact added to the integrity-checksummed bundle.
- Test: assert calibration set disjoint from any model-selection set; ECE-after < ECE-before on held-out path; serializer test proves `calibrated_confidence ≠ raw` when calibrator is non-identity.
- Exit: calibration leakage-safe; downstream Kelly sizing consumes a genuinely calibrated number.

**A5 — Ensemble weighting redesign (kills boundary-weight artifact)**
- Files: `ensemble_trainer.py:89-161`; `_create_and_optimize_ensemble`.
- Change: objective = **mean Deflated Sharpe across CPCV paths** (cost-inclusive), constrained weights + L2 prior toward equal; **reject ensemble unless it beats the best standalone *calibrated* model** on net DSR (directly prevents "ensemble worse than XGBoost-alone"). Remove dead `scipy.optimize` import or use a constrained optimiser intentionally.
- Test: synthetic — a deliberately weak second model ⇒ optimiser must converge to ~standalone (no boundary 0.75/0.25 from a flat objective); ensemble-rejection path covered.
- Exit: weights are CV-stable and economically optimal; ensemble can be auto-rejected.

**A6 — QualityGate redesign (kills RC-5)**
- Files: `model_registry.py:31-133`; `promote_model.py:275-280`.
- Change: promote **net-of-cost DSR** + **calibration (ECE/Brier)** + **data-coverage** to **hard gates** alongside accuracy/training_samples/non-degradation. Add `"tft"` to per-type floors; add a global "positive net-of-cost DSR & better-than-random" floor no type may bypass. `--skip-gates` → audited break-glass requiring a reason string; **never** skips the human confirmation; impossible in the scheduled path.
- Test: 53%-accuracy / zero-DSR fixture must be **blocked** (currently passes); break-glass requires reason and still prompts.
- Exit: a financially invalid model cannot be promoted by any non-audited path.

**A7 — Data integrity gate (kills RC-6)**
- Files: `production_training_orchestrator.py:_generate_data_quality_report`, step-1/3; resume metadata.
- Change: usable-symbol coverage below threshold ⇒ **hard fail** (2551→1198 would have blocked). Root-cause the attrition at the data layer (source gap vs filter strictness) — fix, don't lower the bar. Fix resume path so `training_samples` reflects reality; invariant `samples > 0`.
- Test: synthetic low-coverage corpus ⇒ run aborts; resumed run reports correct sample count.
- Exit: silent data degradation is impossible; run metadata trustworthy.

**A8 — Registry consolidation (kills RC-7) — P0 prerequisite for C**
- Files: **delete** legacy `app/ai/governance/model_registry.py`; `app/ml/model_registry.py` (single authority); `unified_model_registry.py` (becomes a transactional **projection**, not an independent promoter); `app/ai/governance/drift_detector.py`; `governance.py` API; orchestrator step-10 sync block.
- Change: **one promotion authority** = `ModelPromoter` on `ml_model_metadata`. The `ai_ml_models` governance row is updated **atomically in the same unit of work** as a projection (`production/is_active`↔`live`, `staging`↔`paper`, `development`↔`shadow`). Remove the ungated `ModelRegistry.promote_to_production` and `UnifiedModelRegistry.bypass_gates` default; the only ungated path is the audited break-glass (A6). DriftDetector **recommends** (writes a flag/alert), never independently mutates a divergent table (consistent with the human-gate decision).
- Test: a single promotion call updates both tables atomically (rollback test on induced failure → neither table mutated); no remaining import of the deleted module; one-authority property test.
- Exit: one state machine, one promoter, one truth; governance table is a consistent projection.

### Workstream B — GRU → TFT via pytorch-forecasting 🟠

**B1 — Adapter boundary**
- Files: new `app/ml/models/sequence_adapter.py` (`SequenceModelAdapter` Protocol: `fit`, `predict_proba (n,2)`, `export_onnx`, `save/load`, `fit_calibrator`), `app/ml/models/tft/` package.
- Change: define the Protocol the orchestrator/ensemble depend on; **no `torch` import outside `app/ml/models/tft/`**.
- Test: import-isolation test (grep-guard in CI: `torch` only under `tft/`); Protocol conformance test with a stub.
- Exit: orchestrator references the adapter, not Keras/torch specifics.

**B2 — TFT trainer + crash-resilient checkpointing**
- Files: `app/ml/models/tft/trainer.py`; `checkpoint_manager.py` (TFT sub-checkpoints replacing GRU sub-A/B/C); `production_training_orchestrator.py` step-6 (`_train_gru_with_optimization`→`_train_sequence_model`, `_run_gru_fit`, resume branch 415-450, `EpochCheckpointCallback`).
- Change: Lightning `ModelCheckpoint` + a Lightning callback bridging to `CheckpointManager` (preserve resumability parity with the current per-epoch guarantee). HPO via Lightning + Optuna (mirror the XGBoost Optuna pattern; not Keras Tuner). Full symbol universe (fix the 200-symbol GRU root cause), proper trial/epoch budget within the dev-box memory envelope (~4.2 GB RSS baseline; tune `hidden_size/heads/batch`).
- Test: kill -9 mid-epoch ⇒ resume continues from last epoch (parity with GRU sub-C); deterministic-seed reproducibility.
- Exit: TFT trains resumably on the full universe under the local memory budget.

**B3 — ONNX export + serving parity**
- Files: `app/ml/models/tft/export.py`; `_export_models_to_onnx`; `ensemble_predictor.py` (ONNX backend already exists — feed TFT ONNX through it).
- Change: export TFT→ONNX; **opset/op-coverage validated** (TFT has attention/quantile heads — verify or provide a documented supported-subset export); parity test ONNX vs torch logits within tolerance; **re-validate <1 ms-class latency budget** (gate in exit criteria; if unmet, fall back to calibrated-XGBoost-only — the gate, not optimism, decides).
- Test: ONNX-vs-torch numerical parity; latency benchmark in CI perf job; serving smoke through `EnsemblePredictor`.
- Exit: TFT served via existing ONNX Runtime path within latency budget, `torch` absent from serving deps.

**B4 — TFT must earn its place**
- Files: A5 ensemble gate; A6 quality gate.
- Change: TFT enters the ensemble only if it passes A4 calibration + A6 gates **and** is accretive on net DSR (A5); else ship calibrated-XGBoost-only.
- Test: covered by A5/A6 fixtures with a TFT stub.
- Exit: no unproven model reaches production by default.

### Workstream C — Scheduled Self-Training + Human Gate 🟡 (depends on A8)

**C1 — Scheduled orchestration (local)**
- Files: new `scripts/scheduled_retrain.py`; systemd timer unit (doc'd, dev box); `app/ml/config.py` (cadence/window knobs).
- Change: APScheduler/systemd invokes the existing checkpointed orchestrator off-market on a fixed cadence; expanding vs rolling window from config. No new orchestrator — wrap, don't replace.
- Test: dry-run scheduled invocation produces a challenger run dir without promoting.
- Exit: hands-off challenger generation, fully local.

**C2 — Champion/Challenger + signed Promotion Report**
- Files: new `app/ml/evaluation/promotion_report.py`; `ModelPromoter` (challenger compare); MLflow local logging.
- Change: each run = **challenger**, evaluated on the **same CPCV folds** as the champion; report = metric deltas, DSR distribution, PBO, ECE/Brier, data-quality, drift flags, `lineage` (git commit, data-window, config hash, calibrator checksum). Promotion requires explicit human sign-off, then `promote_model.py` (gates ON, dry-run first) ships the **immutable bundle** to prod.
- Test: challenger that fails any hard gate ⇒ report marks "BLOCKED", promotion impossible; bundle checksum verified end-to-end (artifact+calibrator+manifest).
- Exit: reproducible, signed, gated promotion; deterministic rollback (`ModelPromoter.rollback`, tested).

**C3 — `1.0.0` honest re-evaluation (owner-confirmed scope)**
- Files: new `scripts/reeval_production_model.py`.
- Change: run the **current `1.0.0`** through the new cost-aware CPCV pipeline. If net-of-cost DSR fails the A6 bar, **auto-demote to paper/shadow** (no live capital) via the consolidated promoter and emit an incident report; a gated successor is required to restore live.
- Test: end-to-end on `1.0.0`; assert demotion path fires on sub-threshold DSR.
- Exit: the unvalidated live model is no longer a silent capital risk.

### Workstream D — Drift Informs the Gate 🟡

- Files: `app/ai/governance/drift_detector.py`; Prometheus metrics (ML feedback infra already present).
- Change: drift/perf-dip ⇒ high-priority alert + "challenger recommended" flag in the next Promotion Report; **no autonomous demotion of a live model without a successor** (replaces the silent live→…→retired decay). Track live calibration drift (realised vs predicted) + realised net-of-cost Sharpe from paper-trading outcomes (`MLPredictionOutcome`).
- Test: injected drift ⇒ alert + report flag, **no** state mutation.
- Exit: drift is actionable and human-routed; no silent decay.

### Workstream E — Reliability / Security / Observability 🟢 (cross-cutting)

- Schema-versioned checkpoints; deterministic seeds across XGBoost+TFT; OOM-safe steps retained; **every hard gate has a regression test that fails on the original defect**.
- Bundle integrity SHA-256 over **all** artifacts incl. calibrators + feature manifest + `lineage`; break-glass audited; ML key handling verified in the bundle.
- Structured run logs + MLflow local lineage; Promotion Report is the single sign-off artifact; CI perf job guards inference latency.

### Workstream F — Event-Driven Backtest 🟢 (post-A–C; final-validation fidelity)

True event-driven engine (fills, partial fills, latency, portfolio constraints, sizing) as final pre-capital validation **behind** the CPCV gate. Not started until A–C exit criteria pass (sequencing rationale in A3).

---

## 5. Sequencing, Dependencies, Parallelism

```
P0 (correctness, blocks all)
  A0 ─► A1 ─► A2 ─► A3 ─► A4 ─► A5 ─► A6 ─► A7
                      └────────────► A8  (parallel w/ A3-A7; gates C)
P1  B1 ─► B2 ─► B3 ─► B4        (B1 may start after A2; B4 needs A5/A6)
P2  C1 ─► C2 ─► C3              (needs A6 + A8; C3 needs A3/A4)
P2  D                          (needs A8; parallel w/ C2)
P3  E (continuous from P0)      F (after A–C)
```
**Critical path:** A0→A1→A2→A3→A4→A5→A6 (sequential, correctness-ordered). A8 parallels A3–A7. B1 can begin once A2 lands. No phase starts before the prior phase's exit criteria pass. **No model reaches prod without cost-inclusive DSR + leakage-safe calibration + human sign-off.**

Per-phase exit gate = all task "Exit" rows green + their regression tests in CI.

---

## 6. Risk Register

| Risk | Likelihood | Mitigation |
|---|---|---|
| Honest re-eval shows `1.0.0` unprofitable net of costs | High (expected) | C3 auto paper/shadow demotion + incident report; this is the system working |
| TFT ONNX export gaps (attention/quantile ops) | Medium | B3 documented supported-subset export + parity gate; fallback = calibrated-XGBoost-only |
| 2nd DL runtime bloats env/ops | Medium | Hard adapter boundary; `torch` CPU-only for dev training; absent from serving deps; CI import-isolation guard |
| CPCV compute heavy on single box | Medium | Tune fold/path count to memory envelope; CPCV is embarrassingly parallel; still local-feasible |
| Registry consolidation breaks governance UI | Medium | A8 atomic projection + contract tests on `governance.py`; staged behind tests before C |
| Data attrition root cause is structural/upstream | Medium | A7 hard-gates it; fix at data layer, never by lowering the bar |
| `skfolio`/`pytorch-forecasting` version skew | Low | A0 pinned compatibility matrix + import-smoke in CI |

---

## 7. Test Strategy (defect-driven)

Every RC has a regression test that **fails on today's code and passes after the fix**: RC-1 stale-checkpoint loud-fail; RC-2 future-peek leakage probe; RC-3 HOLD-as-flat + exact cost-drag; RC-4 calibration-set disjointness + serializer truthfulness; RC-5 53%/zero-DSR blocked; RC-6 low-coverage abort + sample-count; RC-7 atomic dual-table promotion. Plus: DSR/PBO reference-vector unit tests, ONNX parity, latency perf gate, kill-9 resume parity, bundle-integrity end-to-end.

---

## 8. Open Questions / Assumptions

**Assumptions** (flag if wrong): h=5 daily horizon retained; `charge_calculator` is the canonical cost model + a representative-notional assumption is acceptable for return-space cost drag; weekly off-market cadence; prod box loads the same bundle format the dev box emits; CPU-only torch acceptable for dev-box TFT training at this data scale.

**To resolve during P0 (non-blocking):**
1. Concrete numeric promotion thresholds (min net DSR, max PBO, max ECE, max accuracy drop) — set from the **first honest CPCV distribution**, not guessed now.
2. Representative notional (or use `qty_suggester`) for cost-drag normalization.
3. Retrain window policy (expanding vs rolling) and exact cadence.
4. Whether TFT interpretability (variable-selection/attention) must surface in the Promotion Report and/or the product governance UI.

---

## Sources

- [skfolio CombinatorialPurgedCV (BSD-3)](https://skfolio.org/generated/skfolio.model_selection.CombinatorialPurgedCV.html) · [skfolio](https://skfolio.org/)
- [mlfinlab combinatorial CV (licensing reference)](https://github.com/hudson-and-thames/mlfinlab/blob/master/mlfinlab/cross_validation/combinatorial.py)
- [Purged cross-validation — Wikipedia](https://en.wikipedia.org/wiki/Purged_cross-validation) · [Purging/Embargo/Combinatorial — QuantInsti](https://blog.quantinsti.com/cross-validation-embargo-purging-combinatorial/)
- [Backtest Overfitting in the ML Era (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID4686376_code4361537.pdf?abstractid=4686376&mirid=1)
- [pytorch-forecasting TemporalFusionTransformer](https://pytorch-forecasting.readthedocs.io/en/stable/tutorials/stallion.html) · [TFT paper (1912.09363)](https://arxiv.org/abs/1912.09363)
- [Nixtla neuralforecast TFT](https://nixtlaverse.nixtla.io/neuralforecast/docs/tutorials/forecasting_tft.html)
- [scikit-learn probability calibration](https://scikit-learn.org/stable/modules/calibration.html) · [Calibration for Financial ML — MQL5](https://www.mql5.com/en/articles/21938)
- [Champion/Challenger automated retraining — Snowflake](https://www.snowflake.com/en/developers/guides/ml-champion-challenger-model-deployment/) · [MLOps CI/CD/CT — Google Cloud](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning) · [MLOps Principles](https://ml-ops.org/content/mlops-principles)
```
