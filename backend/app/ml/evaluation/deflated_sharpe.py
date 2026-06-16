"""
Deflated Sharpe Ratio & Probability of Backtest Overfitting — Workstream A3
============================================================================
Implements the Bailey & López de Prado (2014) Deflated Sharpe Ratio (DSR) and
CPCV-path OOS stability diagnostics.

DSR corrects the classic Sharpe significance test for three sources of bias:
  1. Multiple-testing selection bias — trying N candidates and keeping the best.
  2. Non-normality of returns — skewness and heavy tails inflate the raw SR.
  3. Finite-sample variance — the SR estimator is noisy for small T.

Phase-B correction (June 2026)
-------------------------------
``compute_dsr_and_pbo`` previously treated the CPCV path ensemble of a single
model as if it were the strategy-selection pool.  The four concrete bugs were:

  * Candidate SR = argmax path Sharpe (should be the pooled-OOS Sharpe).
  * T = median path length (should be length of the full pooled-OOS stream).
  * Moments (skew/kurtosis) from the concatenated stream mixed with n_obs from
    the median path — populating the CLT variance factor with two different series.
  * n_trials = CPCV path count = 7 (should be the HPO search depth, not 7).

After the fix: ``sr``, ``T``, skew, and kurtosis all derive from the single
coherent **pooled-OOS return stream** (safe to concatenate because CPCV assigns
every panel row to exactly one path — no overlap, no deduplication required).

DSR is now reported as a sensitivity band over N:
  N=1        → pure Probabilistic Sharpe Ratio, zero selection correction.
               This is the single most honest number to report.
  N_upper=105 → worst-case, assuming maximum documented HPO search depth.
               Under N=105 the DSR is lower; true DSR sits somewhere in the band.

The band collapses to a point once ``tune_hyperparameters`` starts logging
per-trial Sharpes (``fwd_ret_val`` parameter) and the caller passes
``trial_sharpes`` here — enabling both the correct V and the effective N.

The metric previously called ``pbo`` has been renamed ``oos_loss_rate`` (fraction
of CPCV paths with non-positive net Sharpe — a useful OOS stability diagnostic,
but not the Bailey-Borwein-LdP CSCV PBO).  The ``pbo`` key is kept as a
**deprecated alias** for backward compatibility with ``ensemble_trainer`` and
``promotion_report``.

References
----------
- Bailey, D. H. & López de Prado, M. (2014).
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
  Overfitting and Non-Normality."  SSRN 2460551.
- Bailey, D. H., Borwein, J., López de Prado, M. & Zhu, Q. J. (2017).
  "The Probability of Backtest Overfitting."
- López de Prado, M. (2018). Advances in Financial Machine Learning —
  CPCV, purging/embargo, clustering for the effective number of independent trials.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from scipy import stats

from app.ml.evaluation.backtest import (
    TRAINING_DECISION_THRESHOLD,
    per_period_sharpe,
    path_sharpe,
    strategy_returns,
)

logger = logging.getLogger(__name__)

# Euler-Mascheroni constant γ (used in SR₀ derivation, Eq. 5 of B&LP 2014)
_EULER_MASCHERONI: float = 0.5772156649015328

# Documented upper bound on the XGBoost HPO search depth (AUC-PR trials).
# The true effective N is lower — AUC-PR is correlated with but ≠ Sharpe, so
# selection bias is attenuated — but cannot be computed precisely until
# per-trial return series are logged.  Used as the conservative upper bound
# of the DSR sensitivity band.
_N_HPO_TRIALS_UPPER: int = 105


# ──────────────────────────────────────────────────────────────────────────────
# SR₀ benchmark
# ──────────────────────────────────────────────────────────────────────────────

def _benchmark_sr0(sr_std: float, n_trials: int) -> float:
    """
    Benchmark Sharpe ratio SR₀ under H₀ of no real edge (B&LP 2014, Eq. 5).

    Models the expected maximum Sharpe from ``n_trials`` candidates when all
    have zero true edge — i.e. the "lucky" Sharpe that data-mining produces.

    Returns 0.0 for n_trials ≤ 1 (no selection bias) or sr_std == 0.
    """
    if n_trials <= 1 or sr_std <= 0.0:
        return 0.0

    N = n_trials
    gamma = _EULER_MASCHERONI
    e = math.e

    q1 = stats.norm.ppf(1.0 - 1.0 / N)
    q2 = stats.norm.ppf(1.0 - 1.0 / (N * e))
    return float(sr_std * ((1.0 - gamma) * q1 + gamma * q2))


# ──────────────────────────────────────────────────────────────────────────────
# Deflated Sharpe Ratio
# ──────────────────────────────────────────────────────────────────────────────

def deflated_sharpe_ratio(
    sr: float,
    n_obs: int,
    skew: float,
    kurt_raw: float,
    sr_std_across_trials: float,
    n_trials: int,
) -> float:
    """
    Deflated Sharpe Ratio (Bailey & López de Prado, 2014 — Eq. 6).

    DSR = Φ[ (Ŝ − SR₀) · √(T−1) / √(1 − γ₃·Ŝ + (γ₄−1)/4·Ŝ²) ]

    Parameters
    ----------
    sr                   : Per-period (NOT annualised) Sharpe ratio of the
                           selected candidate strategy's pooled OOS stream.
    n_obs                : Number of return observations T (from the same stream
                           as ``sr`` — all four must describe one series).
    skew                 : Skewness γ₃ of the strategy's return distribution.
    kurt_raw             : RAW (NOT excess) kurtosis γ₄.  Normal → γ₄ = 3.0.
                           Use ``scipy.stats.kurtosis(x, fisher=False)``.
    sr_std_across_trials : σ_SR — std of per-period Sharpe ratios across the N
                           candidate strategies (HPO trials, not CPCV paths).
    n_trials             : N — number of strategies in the selection pool.

    Returns
    -------
    float ∈ [0, 1] — probability the selected strategy's SR is genuine.
    DSR < 0.95 is the standard rejection threshold.
    Returns ``float('nan')`` when n_obs < 2.
    Returns 0.0 when the variance factor is non-positive (degenerate dist.).
    """
    if n_obs < 2:
        return float("nan")

    sr0 = _benchmark_sr0(sr_std_across_trials, n_trials)

    # CLT approximation of Var[Ŝ] = (1/T) · (1 − γ₃·Ŝ + (γ₄−1)/4·Ŝ²)
    var_factor = 1.0 - skew * sr + (kurt_raw - 1.0) / 4.0 * sr ** 2
    if var_factor <= 0.0:
        # Extreme non-normality renders denominator invalid; be conservative.
        return 0.0

    z = (sr - sr0) * math.sqrt(n_obs - 1) / math.sqrt(var_factor)
    return float(stats.norm.cdf(z))


# ──────────────────────────────────────────────────────────────────────────────
# Threshold sensitivity diagnostic  (Phase C)
# ──────────────────────────────────────────────────────────────────────────────

def compute_threshold_sensitivity(
    cpcv_oof_paths: list[dict[str, Any]],
    *,
    thresholds: tuple[float, ...] = (0.45, 0.50, 0.55, 0.60, 0.65),
    mode: str = "long_only",
    notional: float = 100_000.0,
    product_type: str = "CNC",
    entry_price: float = 1_000.0,
    slippage_bps: float = 5.0,
    periods_per_year: int = 252,
    risk_free_rate_annual: float = 0.05,
) -> dict[str, Any]:
    """
    Compute precision, recall, and pooled net Sharpe at multiple decision
    thresholds — a diagnostic for the promotion report.

    This function is for OBSERVABILITY, not for selection.  The production
    threshold is derived from the balanced-label design (see
    ``TRAINING_DECISION_THRESHOLD`` in backtest.py) and is NOT chosen by
    maximising any metric over this table.  Selecting a threshold based on this
    output would add trials to N in the Deflated Sharpe Ratio.

    Uses the pooled OOS design (all CPCV paths concatenated, non-overlapping by
    construction) so the per-threshold statistics describe the full OOS sample.

    Parameters
    ----------
    cpcv_oof_paths : List of path dicts — same format as ``compute_dsr_and_pbo``.
                     Each dict must contain 'proba', 'y' (true labels), and
                     'forward_return'.  If 'y' is absent the precision/recall
                     fields are set to None but Sharpe is still computed.
    thresholds     : Operating points to evaluate (default: 0.45–0.65 step 0.05).
    ...            : Cost-model params — same defaults as ``compute_dsr_and_pbo``.

    Returns
    -------
    dict with keys:
      ``operating_points``  — list of per-threshold dicts, each with:
          threshold          float
          precision          float | None  — TP / (TP + FP) for UP predictions
          recall             float | None  — TP / (TP + FN) for UP class
          f1                 float | None
          n_active_bars      int  — bars where the model predicted UP
          active_rate        float  — fraction of bars with active position
          pooled_net_sharpe  float  — net-of-cost per-period Sharpe
      ``baseline_threshold`` float  — TRAINING_DECISION_THRESHOLD (= 0.5)
      ``note``               str   — explanation for audit consumers
    """
    if not cpcv_oof_paths:
        return {"operating_points": [], "baseline_threshold": TRAINING_DECISION_THRESHOLD,
                "note": "No paths provided."}

    daily_rfr = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0

    # Pool all OOS data (non-overlapping CPCV paths)
    all_proba: list[np.ndarray] = []
    all_y: list[np.ndarray] = []
    all_fwd: list[np.ndarray] = []
    has_labels = True

    for i, path in enumerate(cpcv_oof_paths):
        proba = np.asarray(path["proba"], dtype=np.float64)
        fwd   = np.asarray(path["forward_return"], dtype=np.float64)
        all_proba.append(proba)
        all_fwd.append(fwd)
        if "y" in path:
            all_y.append(np.asarray(path["y"], dtype=np.int8))
        else:
            has_labels = False

    pooled_proba = np.concatenate(all_proba)
    pooled_fwd   = np.concatenate(all_fwd)
    pooled_y     = np.concatenate(all_y).astype(np.int8) if has_labels and all_y else None

    # Pre-compute unit cost once (same as compute_dsr_and_pbo defaults)
    from decimal import Decimal
    from app.services.paper_trading.charge_calculator import estimate_round_trip_charges
    ep_dec   = Decimal(str(entry_price))
    qty      = max(1, int(notional / entry_price))
    _, _, drag = estimate_round_trip_charges(product_type, ep_dec, ep_dec, qty)
    unit_cost = float(drag) / (float(ep_dec) * qty)
    unit_cost += float(slippage_bps) * 2.0 / 10_000.0

    operating_points: list[dict[str, Any]] = []

    for thr in sorted(thresholds):
        pred = (pooled_proba >= thr).astype(np.int8)
        n_active = int(pred.sum())
        active_rate = float(n_active / max(len(pred), 1))

        # Precision / recall against true labels (if available)
        precision = recall = f1 = None
        if pooled_y is not None:
            tp = int(((pred == 1) & (pooled_y == 1)).sum())
            fp = int(((pred == 1) & (pooled_y == 0)).sum())
            fn = int(((pred == 0) & (pooled_y == 1)).sum())
            precision = tp / max(tp + fp, 1)
            recall    = tp / max(tp + fn, 1)
            f1        = 2.0 * precision * recall / max(precision + recall, 1e-9)

        # Net-of-cost pooled Sharpe at this threshold
        net = np.where(pred == 1, pooled_fwd - unit_cost, 0.0)
        if int((pred == 1).sum()) >= 2:
            excess = net - daily_rfr
            std = float(excess.std(ddof=1))
            pooled_sharpe = float(excess.mean() / std) if std > 0 else float("nan")
        else:
            pooled_sharpe = float("nan")

        operating_points.append({
            "threshold":         round(thr, 4),
            "precision":         round(precision, 4) if precision is not None else None,
            "recall":            round(recall, 4) if recall is not None else None,
            "f1":                round(f1, 4) if f1 is not None else None,
            "n_active_bars":     n_active,
            "active_rate":       round(active_rate, 4),
            "pooled_net_sharpe": round(pooled_sharpe, 6) if math.isfinite(pooled_sharpe) else None,
        })

    logger.info(
        "Threshold sensitivity: %d points evaluated; "
        "baseline threshold=%.2f (TRAINING_DECISION_THRESHOLD)",
        len(operating_points), TRAINING_DECISION_THRESHOLD,
    )

    return {
        "operating_points":   operating_points,
        "baseline_threshold": TRAINING_DECISION_THRESHOLD,
        "note": (
            "DIAGNOSTIC ONLY — not for threshold selection. "
            "TRAINING_DECISION_THRESHOLD=0.5 is a principled prior derived from "
            "the balanced-label design (ATR dead-zone creates ~50/50 UP/DOWN). "
            "Selecting a threshold from this table would add trials to N in the DSR."
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# OOS stability diagnostic
# ──────────────────────────────────────────────────────────────────────────────

def probability_of_backtest_overfitting(path_sharpes: np.ndarray) -> float:
    """
    OOS loss rate: fraction of CPCV OOF paths with non-positive net Sharpe.

    Note: this is NOT the Bailey-Borwein-LdP CSCV Probability of Backtest
    Overfitting (PBO).  True PBO ranks every HPO configuration in-sample vs
    out-of-sample across all combinatorially symmetric split combinations and
    requires the per-trial return series matrix (logged by the convergent
    instrumentation fix in ``xgboost_trainer.tune_hyperparameters``).  This
    metric is a simpler and useful stability diagnostic: 0.0 means every CPCV
    path is profitable; ≥ 0.5 means the model underperforms on more paths than
    it succeeds.  See ``compute_dsr_and_pbo`` for the renamed ``oos_loss_rate``
    key; ``pbo`` is kept as a deprecated alias for backward compatibility.

    Parameters
    ----------
    path_sharpes : 1-D array of per-period Sharpe ratios, one per CPCV path.

    Returns
    -------
    float ∈ [0, 1], or ``float('nan')`` if path_sharpes is empty.
    """
    s = np.asarray(path_sharpes, dtype=np.float64)
    if s.size == 0:
        return float("nan")
    return float((s <= 0.0).sum() / s.size)


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrated computation from CPCV OOF artifacts
# ──────────────────────────────────────────────────────────────────────────────

def compute_dsr_and_pbo(
    cpcv_oof_paths: list[dict[str, Any]],
    *,
    mode: str = "long_only",
    notional: float = 100_000.0,
    product_type: str = "CNC",
    entry_price: float = 1_000.0,
    slippage_bps: float = 5.0,
    periods_per_year: int = 252,
    risk_free_rate_annual: float = 0.05,
    trial_sharpes: np.ndarray | None = None,
) -> dict[str, Any]:
    """
    Compute DSR and OOS stability diagnostics from CPCV OOF path artifacts.

    Workflow
    --------
    1. For each OOF path: threshold ``proba`` at 0.5 → binary preds → net
       strategy returns via the cost-aware backtester.
    2. Pool all path returns into one stream.  CPCV assigns every panel row to
       exactly one path (non-overlapping by construction), so concatenation
       gives the full de-duplicated OOS stream — no dedup step needed.
    3. Compute sr, T, skew, kurtosis all from this single pooled stream so the
       DSR's CLT variance factor operates on a coherent statistical object.
    4. Report DSR as a sensitivity band over N:
         N=1       → pure PSR (no selection correction; most conservative).
         N_eff     → from ``trial_sharpes`` (actual instrumented trial count)
                     with σ_SR computed across the real trial pool.
         N_upper   → _N_HPO_TRIALS_UPPER (maximum documented HPO depth).
       Until ``trial_sharpes`` are provided (requires the convergent
       instrumentation fix in ``tune_hyperparameters``), σ_SR is proxied by
       the std across CPCV paths — this underestimates trial diversity, so
       DSR at N_upper may be slightly optimistic vs the true worst-case.
    5. Compute OOS loss rate (fraction of paths with non-positive net Sharpe).

    Parameters
    ----------
    cpcv_oof_paths   : List of path dicts from
                       ``CheckpointManager.load_cpcv_oof()['paths']``.
                       Each dict must contain:
                       - 'proba': ndarray[float32] of P(UP) scores
                       - 'forward_return': ndarray[float32] of h-bar returns
    trial_sharpes    : Optional 1-D array of per-period net Sharpe ratios,
                       one per completed HPO trial (collected by
                       ``XGBoostTrainer.trial_sharpes`` after
                       ``tune_hyperparameters`` with ``fwd_ret_val``).
                       When provided: σ_SR and N_eff use the real trial pool.
                       When None: σ_SR is proxied by path std; DSR is shown
                       as a band over (N=1, N_upper).
    mode             : "long_only" or "long_short"
    notional         : ₹ position size for statutory charge calculation
    product_type     : "CNC" or "MIS"
    entry_price      : Representative entry price (₹) for charge calc
    slippage_bps     : One-way half-spread in basis points
    periods_per_year : Trading sessions per year (252 for NSE equities)
    risk_free_rate_annual : Annual risk-free rate

    Returns
    -------
    dict with keys:

    Primary corrected DSR
      deflated_sharpe       float ∈ [0,1]  — DSR at N=1 (pure PSR; most honest)
      pooled_oos_sharpe     float           — per-period SR of the pooled OOS stream
      n_obs_pooled          int             — T of the pooled OOS stream

    DSR sensitivity band
      dsr_sensitivity       dict — see below

    OOS stability (correctly named)
      oos_loss_rate         float ∈ [0,1]  — fraction of paths with SR ≤ 0
      pbo                   float          — DEPRECATED alias for oos_loss_rate

    CPCV path distribution
      path_sharpes_annualised  list[float]
      path_sharpes_per_period  list[float]
      n_cpcv_paths             int
      n_trials                 int          — DEPRECATED: was CPCV path count
      n_obs_per_path           int          — median observation count per path

    Moments (from the pooled OOS stream)
      skewness              float
      kurtosis_raw          float

    Informational (no longer drives DSR)
      best_path_idx         int   — argmax of per-path Sharpes (diagnostic only)
      best_per_period_sharpe float
      sr0_benchmark         float — SR₀ at N=1 (= 0.0 by definition)
      sr_std_across_paths   float — σ_SR proxy from CPCV paths (diagnostic)

    Trial variance (populated when trial_sharpes provided)
      sr_std_across_trials  float | None
      n_hpo_trials          int | None

    dsr_sensitivity keys
      n1           float   — DSR at N=1 (= deflated_sharpe)
      n_eff        float | None — DSR at actual trial N (requires trial_sharpes)
      n_upper      float   — DSR at N=_N_HPO_TRIALS_UPPER
      n1_value     int     — 1
      n_eff_value  int | None — actual trial count (requires trial_sharpes)
      n_upper_value int    — _N_HPO_TRIALS_UPPER
      sr_std_source str    — "trial_sharpes" or "cpcv_paths_proxy"
      trials_instrumented bool
      basis        str     — human-readable explanation of the N choices

    Raises
    ------
    ValueError if cpcv_oof_paths is empty or all paths have < 2 observations.
    """
    if not cpcv_oof_paths:
        raise ValueError("cpcv_oof_paths must be non-empty.")

    daily_rfr = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0

    per_period_srs: list[float] = []
    annualised_srs: list[float] = []
    all_net_rets: list[np.ndarray] = []
    obs_counts: list[int] = []

    for i, path in enumerate(cpcv_oof_paths):
        proba = np.asarray(path["proba"], dtype=np.float64)
        fwd_ret = np.asarray(path["forward_return"], dtype=np.float64)

        if len(proba) < 2:
            logger.warning("CPCV path %d has < 2 observations — skipping.", i)
            continue

        pred = (proba >= TRAINING_DECISION_THRESHOLD).astype(np.int8)
        net = strategy_returns(
            pred,
            fwd_ret,
            mode=mode,
            notional=notional,
            product_type=product_type,
            entry_price=entry_price,
            slippage_bps=slippage_bps,
        ).astype(np.float64)

        pp_sr = per_period_sharpe(net, daily_rfr=daily_rfr)
        ann_sr = pp_sr * (periods_per_year ** 0.5)

        per_period_srs.append(pp_sr)
        annualised_srs.append(ann_sr)
        all_net_rets.append(net)
        obs_counts.append(len(net))

    if not per_period_srs:
        raise ValueError(
            "All CPCV OOF paths had fewer than 2 observations. "
            "Cannot compute DSR or PBO."
        )

    # ── Pooled OOS candidate ──────────────────────────────────────────────────
    # CPCV assigns every panel row to exactly one path by construction (the
    # _path_of[(combo_id, g)] map in cpcv.py is injective over panel rows).
    # Simple concatenation therefore gives the complete, non-repeated OOS stream.
    pooled_returns = np.concatenate(all_net_rets)
    sr_candidate = per_period_sharpe(pooled_returns, daily_rfr=daily_rfr)
    T_candidate = len(pooled_returns)

    # All four statistical objects must come from the same series.
    skew = float(stats.skew(pooled_returns))
    kurt_raw = float(stats.kurtosis(pooled_returns, fisher=False))

    # Per-path array (for OOS loss rate and informational distribution)
    pp_arr = np.array(per_period_srs, dtype=np.float64)
    n_cpcv_paths = len(pp_arr)
    best_idx = int(np.argmax(pp_arr))
    best_pp_sr = float(pp_arr[best_idx])
    sr_std_paths = float(pp_arr.std(ddof=1)) if n_cpcv_paths > 1 else 0.0
    n_obs_median = int(np.median(obs_counts))

    # ── OOS loss rate (formerly "pbo" — correctly named) ─────────────────────
    oos_loss_rate = probability_of_backtest_overfitting(pp_arr)

    # ── DSR sensitivity band ──────────────────────────────────────────────────
    # N=1 is the pure Probabilistic Sharpe Ratio: no selection correction,
    # SR₀ = 0, zero dependence on σ_SR.  This is the most honest single figure.
    dsr_n1 = deflated_sharpe_ratio(
        sr=sr_candidate,
        n_obs=T_candidate,
        skew=skew,
        kurt_raw=kurt_raw,
        sr_std_across_trials=0.0,
        n_trials=1,
    )

    if trial_sharpes is not None and len(trial_sharpes) >= 2:
        # Correct σ_SR from the real HPO trial pool
        ts = np.asarray(trial_sharpes, dtype=np.float64)
        sr_std_for_band = float(ts.std(ddof=1))
        n_eff_actual: int | None = len(ts)
        sr_std_across_trials_out: float | None = sr_std_for_band
        n_hpo_trials_out: int | None = n_eff_actual
        trials_instrumented = True
        sr_std_source = "trial_sharpes"

        dsr_n_eff: float | None = deflated_sharpe_ratio(
            sr=sr_candidate, n_obs=T_candidate, skew=skew, kurt_raw=kurt_raw,
            sr_std_across_trials=sr_std_for_band, n_trials=n_eff_actual,
        )
        dsr_n_upper = deflated_sharpe_ratio(
            sr=sr_candidate, n_obs=T_candidate, skew=skew, kurt_raw=kurt_raw,
            sr_std_across_trials=sr_std_for_band, n_trials=_N_HPO_TRIALS_UPPER,
        )
        basis = (
            f"PSR at N=1 (zero selection correction — most conservative). "
            f"N_eff={n_eff_actual} from {n_eff_actual} instrumented HPO trials; "
            f"σ_SR from real trial pool (σ={sr_std_for_band:.4f}). "
            f"N_upper={_N_HPO_TRIALS_UPPER} (documented XGBoost HPO search depth)."
        )
    else:
        # σ_SR proxied by CPCV path std — understates trial diversity, so DSR
        # at N_upper may be slightly optimistic vs the true worst-case.
        sr_std_for_band = sr_std_paths
        n_eff_actual = None
        sr_std_across_trials_out = None
        n_hpo_trials_out = None
        trials_instrumented = False
        sr_std_source = "cpcv_paths_proxy"

        dsr_n_eff = None
        dsr_n_upper = deflated_sharpe_ratio(
            sr=sr_candidate, n_obs=T_candidate, skew=skew, kurt_raw=kurt_raw,
            sr_std_across_trials=sr_std_for_band, n_trials=_N_HPO_TRIALS_UPPER,
        )
        basis = (
            f"PSR at N=1 (zero selection correction — most conservative). "
            f"N_upper={_N_HPO_TRIALS_UPPER} (documented XGBoost HPO search depth). "
            f"σ_SR proxied by CPCV path std (σ={sr_std_paths:.4f}); "
            f"true trial σ_SR is likely higher, so DSR at N_upper may be optimistic. "
            f"Supply trial_sharpes (from XGBoostTrainer.tune_hyperparameters with "
            f"fwd_ret_val) to collapse the band to a single correct estimate."
        )

    dsr_sensitivity: dict[str, Any] = {
        "n1":                dsr_n1,
        "n_eff":             dsr_n_eff,
        "n_upper":           dsr_n_upper,
        "n1_value":          1,
        "n_eff_value":       n_eff_actual,
        "n_upper_value":     _N_HPO_TRIALS_UPPER,
        "sr_std_source":     sr_std_source,
        "trials_instrumented": trials_instrumented,
        "basis":             basis,
    }

    logger.info(
        "DSR (Phase-B corrected) — "
        "pooled_SR=%.4f  T=%d  N=1→DSR=%.4f  N=%d→DSR=%.4f  "
        "oos_loss_rate=%.4f  paths=%d  skew=%.3f  kurt_raw=%.3f",
        sr_candidate, T_candidate,
        dsr_n1, _N_HPO_TRIALS_UPPER, dsr_n_upper,
        oos_loss_rate, n_cpcv_paths, skew, kurt_raw,
    )

    return {
        # ── Primary corrected DSR (N=1 — most honest single number) ──────────
        "deflated_sharpe":        dsr_n1,
        "pooled_oos_sharpe":      float(sr_candidate),
        "n_obs_pooled":           T_candidate,

        # ── DSR sensitivity band ──────────────────────────────────────────────
        "dsr_sensitivity":        dsr_sensitivity,

        # ── OOS stability (correctly named — was "pbo") ───────────────────────
        "oos_loss_rate":          float(oos_loss_rate),
        "pbo":                    float(oos_loss_rate),   # DEPRECATED alias

        # ── CPCV path distribution ────────────────────────────────────────────
        "path_sharpes_annualised": [float(s) for s in annualised_srs],
        "path_sharpes_per_period": [float(s) for s in per_period_srs],
        "n_cpcv_paths":           n_cpcv_paths,
        "n_trials":               n_cpcv_paths,           # DEPRECATED: was path count
        "n_obs_per_path":         n_obs_median,

        # ── Moments (from the pooled OOS stream — corrected) ─────────────────
        "skewness":               float(skew),
        "kurtosis_raw":           float(kurt_raw),

        # ── Informational (no longer drives DSR) ─────────────────────────────
        "best_path_idx":          best_idx,               # argmax of path SRs
        "best_per_period_sharpe": float(best_pp_sr),
        "sr0_benchmark":          0.0,                    # SR₀ at N=1 = 0
        "sr_std_across_paths":    float(sr_std_paths),

        # ── Trial variance (populated when trial_sharpes provided) ────────────
        "sr_std_across_trials":   sr_std_across_trials_out,
        "n_hpo_trials":           n_hpo_trials_out,
    }
