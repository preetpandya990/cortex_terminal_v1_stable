# Cortex ML — World-Class Remediation & Self-Training Plan

**Date:** 2026-05-17
**Status:** Plan (no code changed)
**Owner decisions locked (this session):**

| Decision | Choice |
|---|---|
| Sequence/deep model | **Replace GRU with a Temporal Fusion Transformer (TFT).** Ensemble = XGBoost + TFT |
| Self-training autonomy | **Scheduled retrain + mandatory human approval gate** before any prod promotion |
| Compute / infra | **Local dev-box training only.** No cloud. Validated config/artifact is shipped to the prod machine after sign-off |
| Backtest realism bar | **Recommended below** (Workstream A3): cost-aware vectorized + Combinatorial Purged CV now; event-driven engine as a later phase |

This document is the engineering plan only. Nothing here has been implemented.

---

## 1. Guiding Principles & Non-Goals

**Principles**
- **Fail loud, never silent.** Every place that can silently degrade (missing returns, stale checkpoint, uncalibrated probabilities) must raise, not default to a plausible-looking zero.
- **No metric we cannot trust ships a model.** Financial validity gates promotion, not just classification accuracy.
- **Reproducibility is non-negotiable.** Every model artifact is traceable to exact data window, code commit, feature manifest, and config hash.
- **Right-sized for local-only ops.** No Kubernetes/Airflow/cloud. A lightweight, robust, scheduler-driven local pipeline with a human promotion gate — matching the agreed dev→prod workflow.
- **No band-aids.** Each fix addresses a root cause and is covered by a test that would have caught the original defect.

**Non-Goals (explicitly out of scope to avoid over-engineering)**
- No cloud/distributed training, no online/streaming weight updates, no autonomous auto-promotion.
- No feature-store rearchitecture; the existing feature pipeline is reused.
- No microservice split of the training stack.

---

## 2. Root-Cause Analysis (code-grounded)

These are the *actual* defects, verified by reading the source — not inferred from result JSONs.

### RC-1 — Financial metrics are 0.0 because forward returns never reach the evaluator
- `production_training_orchestrator.py:1321-1326`: when `self.gru_eval_returns is None` (an **old/stale checkpoint that predates forward-return persistence**) the orchestrator **silently falls back from `sharpe_ratio` to `accuracy`** for ensemble weight optimisation and logs only a warning.
- `production_training_orchestrator.py:1349`: `test_returns = self.gru_eval_returns` → `None` on that path.
- `evaluator.py:86-101`: when `returns is None`, **all financial metrics default to `0.0`** with no error.
- **Consequence:** the last shipped model (`1.0.0`, 2026-05-10) was selected with zero financial validation, and its ensemble weights (0.75/0.25) are the boundary artefact of an accuracy grid-search, not a Sharpe optimisation. The data exists — `target_generator.py:243-244` produces `forward_return` — only the persistence/plumbing and the silent fallback are broken.

### RC-2 — The backtest itself is not trustworthy even when returns are present
`evaluator.py:218-287` (`calculate_financial_metrics`):
- `np.where(predictions == 1, returns, -returns)` — **HOLD (`-1`) and DOWN (`0`) are both treated as a SHORT.** The ensemble *does* emit `-1` for sub-threshold confidence (`ensemble_trainer.py:85`), so every abstention becomes a phantom short position.
- **No transaction costs / slippage / STT / brokerage** — despite a production-grade `app/services/paper_trading/charge_calculator.py` already existing in the codebase.
- **Trades every bar** (`n_trades == n_samples`); no position sizing, no holding period.
- Targets are **h=5-bar forward returns** (`target_generator.py:58,243`) but Sharpe is annualised with `sqrt(252)` on **overlapping** daily observations → variance is understated, Sharpe is inflated, and there is look-ahead via overlapping windows.

### RC-3 — Ensemble weighting is statistically unsound
`ensemble_trainer.py:89-161`: grid search over `0.3–0.8` on a **single validation split**, no cross-validation, `scipy.optimize.minimize` imported but unused (dead code). Combined with RC-1 it optimised accuracy, not Sharpe, and landed on a boundary weight.

### RC-4 — No probability calibration
No calibration stage anywhere in training. `calibrated_confidence == confidence` (raw model output). Downstream Kelly/position sizing consumes a number that *claims* to be calibrated and is not — a capital-at-risk hazard.

### RC-5 — Quality gate cannot stop a bad model
`model_registry.py:35-53`: hard gates are accuracy / training_samples / degradation only. **Sharpe is a soft warning (`WARN_SHARPE_RATIO = 0.3`), never a hard gate.** Per-type accuracy floors give the GRU a *lower* floor, which is exactly how a 53%-accuracy near-random model passed. `promote_model.py` exposes `--skip-gates`.

### RC-6 — Data attrition and broken run metadata
Config targets 2551 symbols; `data_quality_report.n_symbols = 1198` (~53% lost) with no gate on attrition. GRU trained on only `gru_n_symbols = 200`; `gru_results.training_samples = 0` (resume path rebuilds train arrays but never repopulates the count) — run metadata is itself unreliable.

---

## 3. Target Architecture

```
                ┌────────────────────────────────────────────────────┐
   Features ───►│  XGBoost (tabular)         TFT (sequence, replaces  │
   (49→pruned)  │      │                      GRU — multi-horizon,    │
                │      │                      interpretable)          │
                │      ▼                          ▼                   │
                │   raw p_xgb                  raw p_tft               │
                │      └──────────┬───────────────┘                   │
                │                 ▼                                   │
                │      CV-optimised ensemble (financial objective)    │
                │                 ▼                                   │
                │      Calibration layer (isotonic/beta, PurgedKFold) │ ◄─ makes
                │                 ▼                                   │   calibrated_
                │      Calibrated P(up) ──► confidence, sizing        │   confidence real
                └────────────────────────────────────────────────────┘
                                  ▼
        Cost-aware CPCV backtest ─► Deflated Sharpe / PBO ─► QualityGate ─► Human sign-off ─► prod config
```

---

## 4. Workstreams

### Workstream A — Correctness & Trustworthy Evaluation  *(P0 — blocks everything)*

**A1. Make forward returns mandatory; delete the silent fallback.**
- Persist `forward_return` alongside `y` in *every* checkpoint that already persists eval arrays (`checkpoint_manager.save_gru_eval_arrays`), and version the checkpoint schema.
- On load, if returns are absent for a Sharpe-configured run: **raise `StaleCheckpointError`** with a clear remediation message. Remove the `gru_eval_returns is None → accuracy` fallback at `production_training_orchestrator.py:1321-1326`.
- `evaluator.py`: when `returns is None` and any financial metric is requested, **raise**, do not return zeros.
- *Test:* run with a deliberately stripped checkpoint → must fail loudly, never produce a 0.0-Sharpe "successful" run.

**A2. Cost-aware, correctness-fixed backtest.**
- Fix the HOLD/SHORT bug: positions ∈ {long, flat, short}; abstentions are **flat**, not short.
- Integrate the existing `charge_calculator` (STT, brokerage, exchange fees, slippage) into per-trade P&L. No new cost model — reuse the production one for consistency between backtest and paper trading.
- Respect the **h=5 holding period**: non-overlapping trade accounting (or block-based) so Sharpe annualisation is statistically valid; no overlapping-window inflation.
- Add turnover, exposure, and per-trade cost drag to the metrics surface.

**A3. Combinatorial Purged Cross-Validation (CPCV) + Deflated Sharpe — RECOMMENDED BACKTEST BAR.**
- Replace single-split evaluation with **CPCV**: N purged, embargoed blocks; multiple backtest paths; report the **distribution** of Sharpe, not a point estimate.
- Compute **Deflated Sharpe Ratio (DSR)** and **Probability of Backtest Overfitting (PBO)** as the headline trust metrics (López de Prado; strongest 2026 evidence against backtest overfitting).
- **Recommendation & justification:** cost-aware vectorized + CPCV/DSR is the *promotion gate now* — it is rigorous, leakage-safe, and fast enough for a local box. A **full event-driven engine is a later phase (Workstream F)** for final pre-capital validation. Building event-driven first would delay trustworthy selection for marginal selection-stage fidelity → that would be over-engineering at this stage. Phased is the correct sequencing.

**A4. Probability calibration (makes `calibrated_confidence` honest).**
- Fit calibrators on **out-of-fold predictions from the purged CV** (no temporal leakage). Default **isotonic**; **beta calibration** fallback when the calibration fold is small (<~200 effective, accounting for label concurrency) — per 2026 financial-ML guidance.
- Calibrate XGBoost and TFT independently, then the ensemble. Persist the calibrator as a first-class registry artifact with its own checksum.
- Gate on **Brier score + reliability-curve ECE**; `calibrated_confidence` is wired to the calibrated output and the misleading alias is removed.

**A5. Statistically sound ensemble weighting.**
- Optimise weights with the **CPCV objective (DSR)**, not single-split accuracy. Constrain weights, add an L2 prior toward equal weight to prevent boundary solutions, and **reject the ensemble if it does not beat the best standalone calibrated model on DSR** (directly prevents the current "ensemble worse than XGBoost-alone" failure). Delete dead `scipy.optimize` import or actually use a constrained optimiser.

**A6. Quality gate redesign.**
- Promote **Deflated Sharpe to a hard gate** (with cost-inclusive backtest), alongside accuracy, calibration (ECE/Brier), training_samples, and non-degradation vs current champion.
- Per-type floors stay, but **no model type may sit below a global "better-than-random + positive net-of-cost DSR" floor.**
- `--skip-gates` becomes break-glass: requires an explicit reason string, is audit-logged, and is impossible in the scheduled path.

**A7. Data integrity gate.**
- Treat the data-quality report as a **hard gate**: fail the run if usable-symbol coverage drops below a configured threshold (e.g., the 2551→1198 attrition would have blocked). Root-cause the attrition (source gaps vs. filter strictness) and fix at the data layer.
- Fix the resume-path metadata so `training_samples` reflects reality; add an invariant check (`samples > 0`).

**Exit criteria (A):** a cold full run produces non-zero, cost-inclusive, CPCV-distributed Sharpe/DSR/PBO; calibration curves pass; gates demonstrably block a deliberately bad model; no silent fallback path remains.

---

### Workstream B — Sequence Model Replacement: GRU → TFT  *(P1)*

- **Retire GRU** entirely (it is a net-negative ensemble member). Implement a **Temporal Fusion Transformer** (strongest 2026 evidence for interpretable, multi-horizon financial forecasting and superior risk-adjusted returns vs RNN/TCN baselines).
- Train on the **full symbol universe** (fix RC-6), not 200; proper HPO budget; class-imbalance handling; quantile/multi-horizon heads aligned to the h=5 target.
- Keep the ONNX export path (TFT → ONNX) so the existing low-latency serving (`ensemble_predictor.py`) is preserved; **inference latency budget must be re-validated** (<1 ms design target).
- TFT must independently pass A4 calibration and A6 gates before it is allowed into the ensemble; otherwise ship calibrated-XGBoost-only until it does (the gate, not optimism, decides).

**Exit criteria (B):** TFT beats GRU and is accretive to the ensemble on DSR under CPCV; calibrated; ONNX-served within latency budget; interpretability outputs (variable importance/attention) surfaced for governance.

---

### Workstream C — Scheduled Self-Training with Human Approval Gate  *(P2)*

Designed for the agreed **local-dev-train → ship-validated-config-to-prod** workflow. No cloud, no auto-promotion.

- **Scheduler:** a lightweight, supervised local scheduler (APScheduler or a `make train` invoked by systemd/cron on the dev box) running the existing checkpointed orchestrator on a fixed cadence (e.g., weekly, off-market). No new heavyweight orchestrator — the 10-step resumable pipeline already exists; we wrap it, not replace it.
- **Champion/Challenger:** each scheduled run produces a **challenger**. It is evaluated against the current **champion** on the *same* CPCV folds, cost-inclusive, with DSR + PBO + calibration deltas.
- **Human approval gate (mandatory):** the run emits a signed **Promotion Report** (metrics deltas, DSR distribution, PBO, calibration curves, data-quality, feature drift, config + data-window + code-commit hashes). A human reviews and explicitly approves. Only then is the immutable artifact bundle + config hash shipped to the prod machine via the existing `promote_model.py` (staging→production, with gates *on*, dry-run first).
- **Reproducibility:** every challenger bundle = {model artifacts, calibrator, feature manifest, config hash, data-window descriptor, git commit, metric report} with SHA-256 integrity (the registry already enforces checksums — extend coverage to calibrator + manifest).
- **Local experiment tracking:** MLflow in **local file/SQLite mode** (no server, no cloud) for run history, metric comparison, and artifact lineage — right-sized, not a platform build-out.
- **Rollback:** prod keeps the previous champion bundle; `promote_model.py rollback` restores it atomically. Rollback is a one-command, tested path.

**Exit criteria (C):** a scheduled run produces a reproducible challenger + Promotion Report, blocks itself on any failed hard gate, and a human can promote/rollback deterministically with full lineage.

---

### Workstream D — Drift & Performance Monitoring (feeds the human gate)

The user chose human-gated promotion, so drift **does not auto-act** — it **informs**.
- Upgrade governance (`drift_detector.py`) so drift/perf-dip **raises a high-priority alert and flags "challenger recommended"** in the next Promotion Report, instead of silently demoting a live model to retirement with no successor (current behaviour: `live→paper→shadow→retired`, no replacement).
- Track live calibration drift (realised vs predicted) and net-of-cost realised Sharpe from paper-trading outcomes, surfaced on the existing Prometheus metrics already added by the ML feedback system.

**Exit criteria (D):** drift produces actionable, human-routed signals; no model can silently decay to "retired" without a successor path.

---

### Workstream E — Reliability, Performance, Security, Observability (cross-cutting)

- **Reliability:** schema-versioned checkpoints; deterministic seeds; OOM-safe steps (existing retry logic retained); every gate has a regression test.
- **Performance:** training stays within the dev box's memory envelope (current peak ~4.2 GB RSS); TFT batch/seq sizing tuned to that envelope; ONNX-optimised inference path preserved and latency-tested per release.
- **Security:** model artifacts remain integrity-checked (SHA-256) end-to-end including calibrator + manifest; `--skip-gates` audit-logged and disabled in the scheduled path; ML encryption key handling unchanged but verified in the promotion bundle.
- **Observability:** structured run logs, MLflow local lineage, Promotion Report as the single source of truth for sign-off.

---

### Workstream F — Event-Driven Backtest (later phase, final-validation fidelity)

A true event-driven engine (order fills, partial fills, latency, portfolio constraints, position sizing) as the **final pre-capital validation** layer behind the CPCV gate. Scoped *after* A–C land so trustworthy selection is not delayed. Listed for completeness; not started until A–C exit criteria are met.

---

## 5. Sequencing & Exit Gates

| Phase | Workstreams | Outcome |
|---|---|---|
| **P0** | A1–A7 | Evaluation is trustworthy; bad models cannot ship; current `1.0.0` re-evaluated honestly (likely fails — that is the point) |
| **P1** | B | TFT replaces GRU; ensemble is provably accretive or XGBoost-only ships |
| **P2** | C, D | Scheduled retrain + human-gated promotion + drift-informed signals, fully local |
| **P3** | E hardening, F | Event-driven final validation; full reliability/security hardening |

Each phase has explicit, test-backed exit criteria above. No phase starts before the prior phase's exit criteria pass. No model reaches prod without DSR (cost-inclusive) + calibration + human sign-off.

---

## 6. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| Honest backtest shows `1.0.0` is unprofitable net of costs | Expected and desirable — surfaces reality. Keep current model paper-only until a gated challenger wins |
| TFT misses the inference latency budget | ONNX export + latency gate in B's exit criteria; fall back to calibrated-XGBoost-only (gate decides) |
| Data attrition root cause is upstream/structural | A7 makes it a hard gate; fix at data layer, not by lowering the bar |
| CPCV is compute-heavy on a single dev box | Tune block count to memory envelope; CPCV is parallelism-friendly and still local-feasible |
| Local-only training limits retrain frequency | Acceptable per owner decision; weekly cadence + human gate matches the dev→prod workflow |

---

## 7. Assumptions & Open Questions

**Assumptions** (flag if wrong): h=5-bar daily horizon stays the modelling target; `charge_calculator` is the canonical cost model to reuse; weekly off-market cadence is acceptable; prod machine can load the same artifact format the dev box produces.

**Open questions for later (do not block P0):**
1. Retrain **cadence** (weekly vs monthly) and the exact data-window policy (expanding vs rolling) for scheduled runs.
2. Concrete **promotion thresholds** (min DSR, max PBO, max ECE, max accuracy drop) — propose during P0/A6 from the first honest CPCV distribution rather than guessing now.
3. Whether **interpretability outputs** (TFT attention/feature importance) must appear in the Promotion Report or also in the product governance UI.

---

## Sources

- [Purged cross-validation — Wikipedia](https://en.wikipedia.org/wiki/Purged_cross-validation)
- [Cross Validation in Finance: Purging, Embargoing, Combinatorial — QuantInsti](https://blog.quantinsti.com/cross-validation-embargo-purging-combinatorial/)
- [Backtest Overfitting in the Machine Learning Era (SSRN)](https://papers.ssrn.com/sol3/Delivery.cfm/SSRN_ID4686376_code4361537.pdf?abstractid=4686376&mirid=1)
- [Combinatorial Purged Cross-Validation method — Towards AI](https://towardsai.net/p/l/the-combinatorial-purged-cross-validation-method)
- [MLOps: Continuous delivery and automation pipelines — Google Cloud](https://docs.cloud.google.com/architecture/mlops-continuous-delivery-and-automation-pipelines-in-machine-learning)
- [Automated Model Retraining & Champion/Challenger — Snowflake](https://www.snowflake.com/en/developers/guides/ml-champion-challenger-model-deployment/)
- [MLOps Principles — ml-ops.org](https://ml-ops.org/content/mlops-principles)
- [Probability calibration — scikit-learn](https://scikit-learn.org/stable/modules/calibration.html)
- [Probability Calibration for Financial Machine Learning — MQL5](https://www.mql5.com/en/articles/21938)
- [Complete Guide to Platt Scaling — Train in Data](https://www.blog.trainindata.com/complete-guide-to-platt-scaling/)
- [Multi-Sensor Temporal Fusion Transformer for Stock Performance (Adaptive Sharpe) — MDPI](https://www.mdpi.com/1424-8220/25/3/976)
- [Hybrid Temporal Fusion Transformer GNN for Stock Market Prediction — MDPI](https://www.mdpi.com/2673-9909/5/4/176)
- [Time Series Foundation Models for Financial Markets — Kinlay](https://jonathankinlay.com/2026/02/time-series-foundation-models-for-financial-markets-kronos-and-the-rise-of-pre-trained-market-models/)
```
