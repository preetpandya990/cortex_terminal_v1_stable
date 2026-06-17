# Cortex — Validation & Regime Integrity Remediation Plan

| | |
|---|---|
| **Scope** | Deflated Sharpe / PBO correctness, regime-label propagation, ATR-multiplier justification |
| **Status** | Plan for execution — implementation gated on the open dependencies in §6 |
| **Related findings** | PRD §9.2 (REQ-ML2.AC1 DSR, REQ-ML2.AC2 ATR), §9.9 (regime-segmented edge) |
| **Source artifacts reviewed** | `deflated_sharpe.py`, `backtest_engine.py`, `regime_service.py`, `target_generator.py`, `feature_pipeline.py` (audit-level), MLflow run metadata |

---

## 0. The one-line finding

The DSR pipeline is computing the right formulas on the wrong statistical objects: it treats the **CPCV path ensemble of a single model as if it were the strategy-selection pool**. The net effect is that the current Deflated Sharpe **flatters** the model rather than penalising it. Computed correctly, the number moves *down*, and the present evidence points to the model not yet demonstrating statistically significant skill. That is a finding to act on, not a number to tune upward.

Two adjacent items compound the picture: regime labels are not propagated into `model_replay` (so the "profit in any market condition" claim is currently unmeasurable), and the ATR SL/TP multiplier is an untuned, unjustified constant that also defines the label distribution.

---

## 1. Sequencing principle

Fix inputs before the verdict, and the verdict before the parameter. A DSR computed on leaky or mislabelled data is precise nonsense; the ATR decision depends on the validation harness counting trials correctly. Dependency-correct order:

- **Phase A — Input truth:** regime propagation (Finding 1) + label/exit correctness audit (Finding 3a).
- **Phase B — Verdict truth:** DSR / PBO correctness (Finding 2).
- **Phase C — Parameter decision:** ATR multiplier and probability threshold (Finding 3b), made *under* the corrected harness.

---

## 2. Phase A — Data & label integrity

### A1. Regime-label propagation (Finding 1)

**Diagnosis (confirmed).** `_compute_regime_from_candles()` (`regime_service.py:415`) is fully causal: it reads only the tail of the candle list (`closes[-1]`, `closes[-2]`, `closes[-21]`, rolling ADX/RSI/ATR/Bollinger), with no `shift(-n)`, no full-series statistics, and no forward data. There is **no lookahead leak**. The defect is purely structural: the detector is not wired into the replay path.

- Production runs the detector daily and writes `sig.regime_type` to the `ai_trading_signals` row; `signal_replay` reads that stored label back.
- `model_replay` hardcodes `"unknown"` (`backtest_engine.py:701`); the regime service is never invoked. The hybrid mode's pre-signal segment is likewise unlabelled.

**Fix.**

1. Add `regime_label` (and `regime_detector_version`) as first-class fields on `SimulatedTrade` and its `to_dict()`.
2. In `model_replay` and the hybrid pre-signal segment, invoke the **same causal detector** on the candle tail available at each decision bar — not a hardcoded label, and not a "latest known" DB lookup. If a DB read is used at all, it must be an as-of resolution to what was knowable at that timestamp.
3. Establish a **single source of truth**: one versioned detector instance/config shared across production, backtest, and ledger, so `signal_replay` and `model_replay` cannot drift. This removes the bug class, not just the instance.
4. Persist the detector version on every trade for reproducibility (NFR-8).

**Acceptance criteria.**

- Every `SimulatedTrade` in all replay modes carries a non-`unknown` `regime_label` sourced from the causal detector.
- `signal_replay` and `model_replay` produce identical regime labels for the same bar/instrument when run against the same candle history.
- Per-regime performance views report **per-regime N** alongside every metric (thin regimes must be visibly thin).

### A2. Label / exit correctness audit (Finding 3a)

**Diagnosis.** `atr_multiplier = 0.5` is hardcoded at `target_generator.py:57` and `feature_pipeline.py:249`. If the labelling scheme is triple-barrier, this multiplier *is* the barrier width — it defines the label distribution, not merely live exits. A 0.5×ATR stop is tight and is likely producing SL-heavy, noise-dominated exits before any thesis resolves.

**Audit (measurement only in this phase — the value decision is Phase C).**

- Confirm whether `target_generator` produces triple-barrier labels (TP / SL / vertical-time barrier).
- Produce the **exit-reason distribution** (TP vs SL vs time) and the **realised-R distribution** under the current 0.5×ATR.
- Confirm whether 0.5 is applied symmetrically to SL and TP. If symmetric, gross R:R is ~1:1 and after costs it is negative-skew.
- Confirm features are causal (label barrier evaluation may look forward along the price path; feature computation must not).

**Acceptance criteria.** A short report of the four items above exists and is attached to the training run. No value change is made here.

---

## 3. Phase B — Validation correctness (Finding 2)

### 3.1 Diagnosis — one conflation, four symptoms

The formulas in `deflated_sharpe.py` are correct: `_benchmark_sr0` matches Bailey & López de Prado (2014) Eq. 5, and `deflated_sharpe_ratio` matches Eq. 6 with the correct PSR variance factor (raw kurtosis). The defect is that `compute_dsr_and_pbo` feeds those formulas the wrong populations.

| Input to `deflated_sharpe_ratio` | Current value | What DSR requires |
|---|---|---|
| `n_trials` | CPCV path count (= 7) | Number of **strategy-selection trials** |
| `sr` (candidate) | `pp_arr[argmax]` — the **best** path Sharpe | The selected model's **pooled OOS** Sharpe |
| `sr_std_across_trials` | std **across paths** of one model | std **across the N candidate strategies** |
| `n_obs` (T) | **median** path length | T of the **candidate series** |

The four symptoms share one root cause: the function treats the CPCV path ensemble as the selection pool. CPCV paths are repeated evaluations of one model on different splits; they involve no selection. Two consequences:

- Selecting the **max** path Sharpe reports the luckiest split as the strategy — an optimistic bias. CPCV's purpose is the *distribution* across paths (mean, variance, worst case), not its maximum.
- The candidate's `sr`, `T`, skew, and kurtosis are currently drawn from three different return populations (best path, median path, pooled stream). They must all describe **one coherent candidate series**.

### 3.2 On `N = 7`

The argument that the 100 XGBoost HPO trials should not inflate N — because they optimised AUC-PR, not Sharpe, and no Sharpe was selected across them — is partially valid but does not support `N = 7`:

- The apparent "1-of-7 selection" exists **only because of the `argmax` bug**. Replace the candidate with the pooled OOS Sharpe (the correct candidate) and there is no 7-way selection at all — there is one model evaluated on a distribution. `N = 7` is an artifact of a second error.
- AUC-PR is **correlated with** Sharpe, so selecting the best-AUC-PR model induces an attenuated, non-zero selection bias on Sharpe. Effective selection exposure therefore sits **between 7 and ~105**, not at either endpoint, and certainly not at 7.
- The principled estimate of effective N is obtained by **clustering the trials' return series** and counting effectively-independent clusters (López de Prado, 2018), or as a lighter approximation `N_eff = ρ̂ + (1 − ρ̂)·M` with `ρ̂` the average pairwise trial correlation. Neither is computable today because **no per-trial return series were logged** — that, not the choice of 7 vs 105, is the real blocker.

### 3.3 Diagnostic ordering — test the cheaper hypothesis first

Before resolving N, compute the **corrected candidate** (pooled OOS Sharpe, with T, skew, kurtosis from that same pooled stream) and evaluate it at **N = 1** — pure Probabilistic Sharpe Ratio against SR₀ = 0, with no multiple-testing penalty. If the model is not PSR-significant with *zero* selection correction, it fails regardless of N and the 7-vs-105 debate is moot.

Expectation: it fails there. The current code already misses the threshold while using the **maximum** path Sharpe and the **smallest** N — the most generous configuration available. The corrected (pooled, lower) candidate at N = 1 will be lower still.

### 3.4 `probability_of_backtest_overfitting` is not PBO

The current function returns the fraction of OOF paths with non-positive Sharpe — a "share of unprofitable paths" stability metric. That is useful but it is **not** the Bailey-Borwein-López de Prado PBO. Real PBO uses Combinatorially Symmetric Cross-Validation (CSCV): rank every configuration in-sample vs out-of-sample across all split combinations and estimate the probability that the in-sample-best config lands below the out-of-sample median.

**Fix.** Either rename the existing metric to `oos_loss_rate` (retain it — it is a legitimate diagnostic) **or** implement CSCV PBO. The latter requires the same per-trial return-series matrix that correct N and V require.

### 3.5 The convergent instrumentation fix (highest-leverage item)

One engineering change unblocks three deliverables. Persist **per-trial, per-period return series** — every retained HPO configuration, Sharpe-evaluated through the existing cost-aware backtester, logged to MLflow — and you simultaneously obtain:

1. **V** — the variance of Sharpes across real trials (for SR₀).
2. **Effective N** — via clustering of the trial return series.
3. **The CSCV matrix** — for a real PBO.

This converts the DSR from "approximated from the wrong population" to "auditable from the real search." It is the single most valuable change in this workstream.

### 3.6 Corrected `compute_dsr_and_pbo` — shape

> Illustrative; finalisation gated on the §6 dependencies (backtest signatures, OOF overlap semantics).

```python
# Candidate = the selected model's pooled OOS stream (NOT the max path).
candidate_returns = build_pooled_oos(all_net_rets)   # dedup overlapping obs
sr_candidate = per_period_sharpe(candidate_returns, daily_rfr=daily_rfr)
T_candidate  = candidate_returns.size                # coherent T
skew = stats.skew(candidate_returns)
kurt = stats.kurtosis(candidate_returns, fisher=False)

# N and V come from the SELECTION SEARCH, not from CPCV paths.
# Until per-trial Sharpes are logged, report DSR as a sensitivity band over N.
dsr_curve = {
    N: deflated_sharpe_ratio(
        sr=sr_candidate, n_obs=T_candidate, skew=skew, kurt_raw=kurt,
        sr_std_across_trials=sr_std_across_TRIALS, n_trials=N,
    )
    for N in (1, n_eff_lower, 105)        # N=1 is the pure-PSR reference
}

# CPCV paths feed the OOS distribution + (eventually) a real CSCV PBO — not N.
oos_loss_rate = (pp_arr <= 0).mean()      # renamed from "pbo"
```

### 3.7 Phase B acceptance criteria

- DSR candidate is the pooled OOS series; `sr`, `T`, skew, kurtosis all derive from that one series.
- `n_trials` is no longer the CPCV path count. DSR is reported as a sensitivity band over N (minimum N = 1) until effective N is computable from logged trials.
- The mislabelled metric is renamed or replaced with CSCV PBO.
- Per-trial, per-period return series are persisted to MLflow for every retained HPO trial going forward.
- The promotion report records: candidate pooled Sharpe, PSR at N = 1, DSR across the N band, the assumed N and its basis, V, skew, kurtosis, T, and `oos_loss_rate` / CSCV PBO.

---

## 4. Phase C — Parameter decisions (Finding 3b)

Made only after Phase B's harness counts trials correctly, because tuning interacts with N.

### 4.1 ATR SL/TP multiplier

- **Option A — principled prior (preferred):** derive barrier widths from volatility/horizon reasoning toward a target R:R, document the derivation, leave untuned. Adds **zero** trials to N.
- **Option B — tuned:** sweep widths inside nested CV; **every width tried increments M (and thus N)** in the DSR. Justified only if the out-of-sample gain survives the raised hurdle.

In both cases, MLflow provenance is the byproduct, not the deliverable. REQ-ML2.AC2 asks for a value **justified against validation under correct trial accounting** — not merely logged.

### 4.2 Decision threshold `proba >= 0.5`

`pred = (proba >= 0.5)` is a hardcoded operating point on an AUC-PR-optimised model. On an imbalanced problem, 0.5 is rarely the correct cut, and any threshold ever searched is itself a trial. Treat it as a Phase C decision alongside the ATR multiplier; do not leave it as an unexamined constant.

---

## 5. Risks & honest boundaries

- **The model may not clear the bar.** The most likely Phase B outcome is that the corrected DSR (and possibly PSR at N = 1) fails. That is the system telling the truth, consistent with the PRD's honesty principle — not a regression to patch around.
- **CPCV and DSR bound historical robustness only.** They correct for multiple testing and evaluate across regimes *present in the data*. Neither protects against a structurally novel regime unseen in the sample. This sentence belongs in the performance disclosures, not buried in code.
- **Thin per-regime samples.** Strict consensus produces few signals; segmenting by regime fragments the sample further, widening every confidence interval. Surface per-regime N so thin slices are not over-interpreted.

---

## 6. Open dependencies (required before production code)

1. **`backtest.py` signatures/semantics** for `per_period_sharpe`, `path_sharpe`, `strategy_returns` — needed to build the pooled candidate correctly.
2. **CPCV OOF checkpoint structure** (`load_cpcv_oof`) — whether path observations overlap. If they do, the pooled OOS stream must deduplicate before computing moments, or skew/kurtosis are corrupted.
3. **HPO trial recoverability** — can the retained Optuna trials be Sharpe-evaluated retrospectively, or is the study discarded so instrumentation begins from the next training run? This determines whether a real effective N is available now or an honest sensitivity band now with a real N next run.

---

## 7. Execution checklist

- [ ] A1 — `regime_label` + version on `SimulatedTrade.to_dict()`; causal detector wired into `model_replay` and hybrid pre-signal segment; single-source detector config.
- [ ] A1 — parity test: `signal_replay` vs `model_replay` identical labels on identical history.
- [ ] A2 — exit-reason, realised-R, and SL/TP-symmetry audit attached to the training run.
- [ ] B — pooled-OOS candidate construction (with overlap dedup) replacing `argmax` path.
- [ ] B — DSR reported as sensitivity band over N; PSR at N = 1 computed first.
- [ ] B — rename `oos_loss_rate` and/or implement CSCV PBO.
- [ ] B — persist per-trial, per-period return series to MLflow.
- [ ] C — ATR multiplier: principled prior or nested-CV tuning with trials counted.
- [ ] C — decision-threshold review.

---

## 8. References

- Bailey, D. H. & López de Prado, M. (2014). *The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality.* Journal of Portfolio Management 40(5).
- Bailey, D. H., Borwein, J., López de Prado, M. & Zhu, Q. J. (2017). *The Probability of Backtest Overfitting.* Journal of Computational Finance.
- López de Prado, M. (2018). *Advances in Financial Machine Learning* — CPCV, purging/embargo, and clustering for the effective number of independent trials.
