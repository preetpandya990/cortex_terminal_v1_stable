"""
Deflated Sharpe Ratio & Probability of Backtest Overfitting — Workstream A3
============================================================================
Implements the Bailey & López de Prado (2014) Deflated Sharpe Ratio (DSR) and
a CPCV-adapted Probability of Backtest Overfitting (PBO).

DSR corrects the classic Sharpe significance test for three sources of bias:
  1. Multiple-testing selection bias — trying N candidates and keeping the best.
  2. Non-normality of returns — skewness and heavy tails inflate the raw SR.
  3. Finite-sample variance — the SR estimator is noisy for small T.

PBO (for a single model evaluated on CPCV paths) is the fraction of OOS time
periods where the strategy earns a non-positive net-of-cost Sharpe.  PBO ≥ 0.5
signals the model underperforms in more periods than it succeeds.

References
----------
- Bailey, D. H. & López de Prado, M. (2014).
  "The Deflated Sharpe Ratio: Correcting for Selection Bias, Backtest
  Overfitting and Non-Normality."  SSRN 2460551.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
from scipy import stats

from app.ml.evaluation.backtest import (
    per_period_sharpe,
    path_sharpe,
    strategy_returns,
)

logger = logging.getLogger(__name__)

# Euler-Mascheroni constant γ (used in SR₀ derivation, Eq. 5 of B&LP 2014)
_EULER_MASCHERONI: float = 0.5772156649015328


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
                           *selected* (best) candidate strategy.
    n_obs                : Number of return observations T.
    skew                 : Skewness γ₃ of the strategy's return distribution.
    kurt_raw             : RAW (NOT excess) kurtosis γ₄ of the return
                           distribution.  Normal → γ₄ = 3.0.
                           Use ``scipy.stats.kurtosis(x, fisher=False)``.
    sr_std_across_trials : σ_SR — std of per-period Sharpe ratios across all N
                           candidate strategies / CPCV paths.
    n_trials             : N — number of strategies evaluated (selection pool).

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
# Probability of Backtest Overfitting
# ──────────────────────────────────────────────────────────────────────────────

def probability_of_backtest_overfitting(path_sharpes: np.ndarray) -> float:
    """
    CPCV-adapted Probability of Backtest Overfitting.

    For a single model evaluated across φ CPCV OOF paths, PBO is defined as
    the fraction of paths with a non-positive net-of-cost per-period Sharpe.

    Interpretation
    --------------
    PBO = 0.0  — strategy is profitable on every OOS path; robust.
    PBO = 0.5  — model succeeds on only half the OOS periods (chance).
    PBO ≥ 0.5  — model systematically underperforms; probable overfitting.

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
) -> dict[str, Any]:
    """
    Compute DSR and PBO from CPCV OOF path artifacts.

    Workflow
    --------
    1. For each OOF path: threshold ``proba`` at 0.5 → binary preds → net
       strategy returns via the cost-aware backtester.
    2. Compute per-period and annualised Sharpe for each path.
    3. Select the winner (max per-period Sharpe) as the DSR candidate.
    4. Compute DSR corrected for N-path selection bias and non-normality.
    5. Compute PBO = fraction of paths with non-positive per-period Sharpe.

    Parameters
    ----------
    cpcv_oof_paths        : List of path dicts from
                            ``CheckpointManager.load_cpcv_oof()['paths']``.
                            Each dict must contain:
                            - 'proba': ndarray[float32] of P(UP) scores
                            - 'forward_return': ndarray[float32] of h-bar returns
    mode                  : "long_only" or "long_short"
    notional              : ₹ position size for statutory charge calculation
    product_type          : "CNC" or "MIS"
    entry_price           : Representative entry price (₹) for charge calc
    slippage_bps          : One-way half-spread in basis points
    periods_per_year      : Trading sessions per year (252 for NSE equities)
    risk_free_rate_annual : Annual risk-free rate for Sharpe excess return

    Returns
    -------
    dict with keys:
      deflated_sharpe          float ∈ [0, 1]
      pbo                      float ∈ [0, 1]
      path_sharpes_annualised  list[float] — annualised SR per path
      path_sharpes_per_period  list[float] — per-period SR per path (for DSR)
      best_path_idx            int — index of the selected (max-SR) path
      best_per_period_sharpe   float
      sr0_benchmark            float — minimum "lucky" SR under H₀
      sr_std_across_paths      float — σ_SR
      n_trials                 int — number of OOF paths used
      n_obs_per_path           int — median observation count per path
      skewness                 float — γ₃ of combined return stream
      kurtosis_raw             float — γ₄ of combined return stream

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

        pred = (proba >= 0.5).astype(np.int8)
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

    pp_arr = np.array(per_period_srs, dtype=np.float64)
    all_returns = np.concatenate(all_net_rets)

    # Distribution moments of the combined return stream
    skew = float(stats.skew(all_returns))
    kurt_raw = float(stats.kurtosis(all_returns, fisher=False))

    # DSR: test the BEST (selected) path as the candidate strategy
    best_idx = int(np.argmax(pp_arr))
    best_pp_sr = float(pp_arr[best_idx])

    sr_std = float(pp_arr.std(ddof=1)) if len(pp_arr) > 1 else 0.0
    n_trials = len(pp_arr)
    n_obs = int(np.median(obs_counts))

    dsr = deflated_sharpe_ratio(
        sr=best_pp_sr,
        n_obs=n_obs,
        skew=skew,
        kurt_raw=kurt_raw,
        sr_std_across_trials=sr_std,
        n_trials=n_trials,
    )
    pbo = probability_of_backtest_overfitting(pp_arr)
    sr0 = _benchmark_sr0(sr_std, n_trials)

    logger.info(
        "DSR=%.4f  PBO=%.4f  best_pp_SR=%.4f  SR0=%.4f  "
        "N=%d  T=%d  skew=%.3f  kurt_raw=%.3f",
        dsr, pbo, best_pp_sr, sr0, n_trials, n_obs, skew, kurt_raw,
    )

    return {
        "deflated_sharpe": dsr,
        "pbo": pbo,
        "path_sharpes_annualised": [float(s) for s in annualised_srs],
        "path_sharpes_per_period": [float(s) for s in per_period_srs],
        "best_path_idx": best_idx,
        "best_per_period_sharpe": best_pp_sr,
        "sr0_benchmark": sr0,
        "sr_std_across_paths": sr_std,
        "n_trials": n_trials,
        "n_obs_per_path": n_obs,
        "skewness": skew,
        "kurtosis_raw": kurt_raw,
    }
