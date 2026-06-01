# Cortex ML System — Full Audit

**Date:** 2026-05-17
**Auditor:** Claude (read-only audit, no code changed)
**Model version:** 1.0.0 — last full train **2026-05-10 19:54**
**Scope:** `backend/app/ml/`, `backend/app/ai/`, `scripts/production_training_orchestrator.py`, production artifacts & training results.

---

## 1. Executive Summary

| Area | Verdict |
|---|---|
| Architecture | Solid — versioned registry, dual-model ensemble, ONNX/Treelite serving, checkpointed pipeline |
| Predictive accuracy | **Marginal** — ensemble 64.9%, XGBoost 65.4%, GRU 53.1% (near coin-flip) |
| Financial validation | **Broken/missing** — every trading metric (Sharpe, win rate, drawdown, profit factor) = 0.0 |
| Ensemble weighting | **Unreliable** — optimized on `sharpe_ratio` which was 0.0 → fell back to default 0.75/0.25 |
| Data coverage | **Degraded** — config targets 2551 symbols, only 1198 had usable data; GRU trained on 200 |
| Self / deep auto-training | **Not implemented** — fully manual; no scheduler, no retrain trigger, no online learning |

**Bottom line:** The infrastructure is production-grade, but the model is only modestly better than random on direction, has **no working financial backtest**, and has **zero autonomous retraining capability** today.

---

## 2. Performance

### Pipeline (run 2026-05-10)
- 10-step checkpointed orchestrator (`production_training_orchestrator.py`, 1780 lines): symbols → features → targets → splits → xgboost → gru → ensemble → eval → onnx → registry. Idempotent and resumable.
- Total samples: 2,950,200 (XGB train 1.54M / val 386K).
- Reported training duration: **452 s (~7.5 min)** — implausibly short for 100 XGBoost Optuna trials + GRU tuning over ~3M samples. Strongly indicates a **checkpoint-resumed run**, so the result file does not reflect a true cold full train. Treat duration/sample counts as unreliable.
- Memory: 4.2 GB RSS, 26% — comfortable.
- Serving: Treelite `.so` (XGBoost) + ONNX (GRU), Redis-cached. Latency design target <1 ms.

### Data quality red flags
- `n_symbols` config = 2551, `data_quality_report.n_symbols` = **1198** (≈53% attrition).
- GRU `training_samples` logged as **0** (metadata/logging bug or GRU step skipped on resume); only 30K validation samples.
- Evaluation set only **30,000 samples** for a 2551-symbol universe — thin for generalization claims.

---

## 3. Accuracy (test set, n=30,000)

| Model | Accuracy | Log Loss | F1 (up/down) | Notes |
|---|---|---|---|---|
| XGBoost | **0.654** | 0.633 | 0.648 / 0.660 | Strongest single model; balanced |
| GRU | **0.531** | 0.689 | 0.613 / 0.405 | ~Coin flip; collapses on "down" (recall 0.32) |
| **Ensemble** | **0.649** | 0.644 | 0.654 / 0.644 | **Worse than XGBoost alone** |

Key issues:
1. **Ensemble underperforms its best member.** Blending in a near-random GRU (53%) at weight 0.25 drags ensemble (64.9%) below standalone XGBoost (65.4%). The ensemble adds latency without accuracy.
2. **GRU is effectively non-predictive** — 53.1% accuracy, severe class bias (predicts "up" far more; "down" recall 0.32). Likely under-trained (`training_samples: 0`), tiny LR (1.2e-5), only 5 tuning trials, 200 symbols.
3. **No probability calibration.** Corroborates prior finding that `calibrated_confidence == confidence` (no Platt/isotonic). Confidence scores are raw model outputs — risky for downstream Kelly position sizing.
4. **Top features** (XGB gain): f12, f37, f18, f17, f39 dominate; f7 (137) and f6 (269) near-dead — feature pruning opportunity.

---

## 4. Financial Validation — **CRITICAL GAP**

Every trading metric in `evaluation_results` is **0.0** for all three models:
`sharpe_ratio, sortino_ratio, max_drawdown, win_rate, profit_factor, total_return, n_trades`.

- The backtest/PnL evaluation is **not wired** into the training pipeline. The model is selected and shipped on classification accuracy only.
- The ensemble weight optimizer is configured with `ensemble_optimization_metric: "sharpe_ratio"`. Since Sharpe is always 0.0, the optimizer had no signal and produced the **trivial default 0.75/0.25** split — i.e., the ensemble weights are effectively unoptimized.
- **There is no evidence the model is profitable.** Directional accuracy ≈65% does not imply positive expectancy after costs/slippage; this is currently unmeasured.

---

## 5. Governance, Drift & Feedback (what exists)

- `app/ai/governance/drift_detector.py` + `app/ml/monitoring/drift_detector.py`: KS-test feature/prediction drift detection.
- On drift, governance only **demotes**: live → paper → shadow → retired. It **does not trigger retraining or promote a replacement** — a drifting model silently decays to "retired" with no successor.
- ML feedback system (regime detector, retry infra, `ml_feedback_errors`) and walk-forward validation exist but are **offline/advisory** — they generate alerts and backtests, not automated retrains.
- Model registry: SHA-256 integrity, quality gates, lineage, ONNX export — good foundation.

---

## 6. Self / Deep Auto-Training Capability — **NOT IMPLEMENTED**

Current state:
- Training is **100% manual**: operator runs `python scripts/production_training_orchestrator.py`.
- **No scheduler** (no cron, APScheduler, Celery beat, Airflow) and **no API endpoint** to trigger training.
- **No online/incremental learning** — XGBoost retrains from scratch; GRU has no warm-start/continual-learning loop.
- **No closed feedback loop**: realized trade outcomes and drift signals are stored but never feed an automated retrain/redeploy.
- Walk-forward module *simulates* periodic retraining offline but is not an autonomous production loop.

### Future scope — roadmap to autonomous deep self-training

**Phase 1 — Close the loop (foundational)**
- Wire the existing PnL/backtest engine into `step_8_evaluation` so Sharpe/profit-factor are real; make ensemble weighting and model promotion gate on financial metrics, not just accuracy.
- Add probability calibration (isotonic/Platt) as a pipeline step; make `calibrated_confidence` truthful.

**Phase 2 — Scheduled retraining**
- Add a scheduler (Celery beat / APScheduler / Airflow DAG) to run the orchestrator on a cadence (e.g., weekly off-market) with the checkpoint system already in place.
- Auto-register → shadow-deploy → champion/challenger A-B → auto-promote only if challenger beats champion on out-of-sample Sharpe + accuracy.

**Phase 3 — Drift-triggered retraining**
- Upgrade drift handler: on drift, **enqueue a retrain job** and keep current model live until a validated replacement passes quality gates (instead of blind demotion to "retired").

**Phase 4 — Continual / deep self-training**
- GRU: incremental fine-tuning with warm-start + replay buffer of recent regimes; expand symbol/trial budget (current 200 symbols / 5 trials is the root cause of GRU weakness).
- Add Bayesian/Optuna hyper-search persistence across runs (study storage) so each retrain *improves* rather than restarts blind.
- Regime-aware model zoo: separate specialists per market regime, router selects at inference.
- Guardrails: automatic rollback on live-metric regression, canary traffic %, human-in-the-loop approval for promotion.

---

## 7. Prioritized Recommendations

| # | Priority | Action |
|---|---|---|
| 1 | **P0** | Wire real financial backtest into evaluation; stop shipping on accuracy alone |
| 2 | **P0** | Fix ensemble weighting (currently default-fallback because Sharpe=0) |
| 3 | **P1** | Investigate/repair GRU (training_samples=0, 53% acc) or drop it from the ensemble — XGBoost alone is currently better |
| 4 | **P1** | Add probability calibration step; make `calibrated_confidence` real |
| 5 | **P1** | Fix data attrition (2551→1198 symbols) — half the universe is being lost |
| 6 | **P2** | Implement Phase 1–2 of self-training roadmap (scheduler + champion/challenger) |
| 7 | **P2** | Change drift handler to trigger retrain instead of silent demotion |
| 8 | **P3** | Feature pruning (drop near-zero-importance f6/f7); expand eval set beyond 30K |

---
*Audit only — no source files were modified. Metrics sourced from `models/production/training_results_20260510_195435.json` and `checkpoints/step_8_evaluation/results.json`.*
