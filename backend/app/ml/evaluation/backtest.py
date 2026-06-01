"""
Cost-Aware Backtester — Workstream A3
======================================
Converts model predictions into realistic net strategy returns that account
for NSE statutory charges and estimated bid-ask slippage.

All arithmetic that touches the charge calculator uses Decimal internally;
strategy_returns outputs float32 for downstream numpy compatibility.

Position convention
-------------------
pred ∈ {0, 1}:  binary model output — 0 = DOWN, 1 = UP
pred ∈ {−1}:    explicit HOLD sentinel — always maps to FLAT

mode = "long_only"  → DOWN/HOLD → 0 (flat); UP → +1
mode = "long_short" → DOWN → −1 (short); HOLD → 0 (flat); UP → +1

References
----------
- SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/74 (NSE charge schedule, effective
  October 1 2024) — see charge_calculator.py for full schedule.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Literal

import numpy as np

from app.services.paper_trading.charge_calculator import estimate_round_trip_charges

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Position mapping
# ──────────────────────────────────────────────────────────────────────────────

def binary_to_positions(
    pred: np.ndarray,
    *,
    mode: Literal["long_only", "long_short"] = "long_only",
) -> np.ndarray:
    """
    Map raw model predictions to signed position sizes.

    Parameters
    ----------
    pred : Integer array with values in {0, 1} (binary: 0=DOWN, 1=UP) or
           {−1, 0, 1} (ternary).  Any value of −1 is treated as an explicit
           HOLD and is always mapped to 0, regardless of mode.
    mode : "long_only"  — UP → +1, DOWN → 0 (flat), HOLD → 0
           "long_short" — UP → +1, DOWN → −1 (short), HOLD → 0

    Returns
    -------
    np.ndarray[int8] with values in {−1, 0, +1}.
    """
    p = np.asarray(pred, dtype=np.int8)
    hold_mask = p == np.int8(-1)

    if mode == "long_only":
        pos = np.where(p == np.int8(1), np.int8(1), np.int8(0))
    else:
        # long_short: UP→+1, DOWN→−1, HOLD(−1 sentinel)→overridden below
        pos = np.where(
            p == np.int8(1), np.int8(1),
            np.where(p == np.int8(0), np.int8(-1), np.int8(0)),
        )

    pos[hold_mask] = np.int8(0)
    return pos


# ──────────────────────────────────────────────────────────────────────────────
# Statutory charge helpers
# ──────────────────────────────────────────────────────────────────────────────

def round_trip_charge_fraction(
    product_type: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: int,
) -> float:
    """
    Statutory charge drag as a fraction of entry-side notional.

    Delegates to ``estimate_round_trip_charges`` for exact Decimal arithmetic
    and returns a plain float for efficient vectorised application.

    Parameters
    ----------
    product_type : "CNC" (delivery) or "MIS" (intraday)
    entry_price  : Simulated entry fill price (Decimal)
    exit_price   : Simulated exit fill price (Decimal)
    quantity     : Number of shares (positive int)

    Returns
    -------
    float — e.g. ≈ 0.002233 for a CNC round-trip (≈ 0.22% of notional).
    """
    _, _, total_drag = estimate_round_trip_charges(
        product_type, entry_price, exit_price, quantity
    )
    entry_notional = float(entry_price) * quantity
    return float(total_drag) / entry_notional


# ──────────────────────────────────────────────────────────────────────────────
# Net strategy returns
# ──────────────────────────────────────────────────────────────────────────────

def strategy_returns(
    pred: np.ndarray,
    fwd_ret: np.ndarray,
    *,
    mode: Literal["long_only", "long_short"] = "long_only",
    notional: float = 100_000.0,
    product_type: str = "CNC",
    entry_price: float = 1_000.0,
    slippage_bps: float = 5.0,
) -> np.ndarray:
    """
    Compute per-period net strategy returns after statutory charges and
    bid-ask slippage.

    Each non-flat prediction is modelled as a one-period round-trip trade:
    enter at ``entry_price`` (representative, for charge calculation), hold
    for the label horizon h, exit at ``entry_price × (1 + fwd_ret)``.

    The statutory charge fraction is computed once at the representative
    entry price (flat-rate approximation valid when fwd_ret is small) and
    deducted proportionally on every active bar.

    Parameters
    ----------
    pred         : Integer array {0,1} or {−1,0,1}.
    fwd_ret      : h-bar forward returns, aligned with pred.
    mode         : "long_only" or "long_short" (see binary_to_positions).
    notional     : Position size in ₹, used to derive quantity for charge calc.
    product_type : "CNC" or "MIS".
    entry_price  : Representative entry price (₹) for statutory charge calc.
    slippage_bps : One-way half-spread estimate in basis points.

    Returns
    -------
    np.ndarray[float32] — net return per observation (0.0 for flat bars).
    """
    positions = binary_to_positions(pred, mode=mode)
    gross = positions.astype(np.float32) * np.asarray(fwd_ret, dtype=np.float32)

    active = np.abs(positions).astype(np.float32)  # 1.0 where a trade is made

    # Round-trip slippage: entry half-spread + exit half-spread
    slip = active * float(slippage_bps * 2.0 / 10_000.0)

    # Statutory round-trip charge (computed once at representative price)
    ep = Decimal(str(entry_price))
    qty = max(1, int(notional / entry_price))
    charge_frac = round_trip_charge_fraction(product_type, ep, ep, qty)
    charge = active * float(charge_frac)

    return (gross - slip - charge).astype(np.float32)


# ──────────────────────────────────────────────────────────────────────────────
# Sharpe helpers  (used by deflated_sharpe.py)
# ──────────────────────────────────────────────────────────────────────────────

def per_period_sharpe(returns: np.ndarray, *, daily_rfr: float = 0.0) -> float:
    """
    Per-period (un-annualised) Sharpe ratio for DSR input.

    Uses ddof=1 to match the CLT variance derivation in
    Bailey & López de Prado (2014).  Returns 0.0 for degenerate inputs.
    """
    r = np.asarray(returns, dtype=np.float64)
    if len(r) < 2:
        return 0.0
    excess = r - daily_rfr
    std = float(excess.std(ddof=1))
    if std == 0.0:
        return 0.0
    return float(excess.mean() / std)


def path_sharpe(
    returns: np.ndarray,
    *,
    periods_per_year: int = 252,
    risk_free_rate: float = 0.05,
) -> float:
    """
    Annualised Sharpe ratio for display and promotion-bar reporting.

    Note: this is NOT the Ŝ fed to the DSR formula — DSR uses
    ``per_period_sharpe`` to maintain unit consistency with T.
    """
    daily_rfr = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    pp = per_period_sharpe(returns, daily_rfr=daily_rfr)
    return pp * (periods_per_year ** 0.5)


# ──────────────────────────────────────────────────────────────────────────────
# CPCV robustness diagnostic  (used by A5 ensemble weighting)
# ──────────────────────────────────────────────────────────────────────────────

def compute_cpcv_path_net_sharpes(
    *,
    w_xgb: float,
    xgb_cal_matrix: np.ndarray,
    p_gru: np.ndarray,
    fwd_ret: np.ndarray,
    notional: float = 100_000.0,
    product_type: str = "CNC",
    entry_price: float = 1_000.0,
    slippage_bps: float = 5.0,
    risk_free_rate_annual: float = 0.05,
    periods_per_year: int = 252,
) -> list[float]:
    """Per-period net Sharpe for each CPCV path at a fixed ensemble weight.

    After ``optimize_weights_on_oof`` finds the optimal w_xgb on the bagged
    consensus, this function re-applies that weight to every individual CPCV
    combo-model path (columns of ``xgb_cal_matrix``) to produce a *distribution*
    of per-path net Sharpes.  This is López de Prado's intended use of CPCV
    paths — robustness testing after parameter selection, not for selection
    itself.  A5 logs the result as an informational diagnostic; it is not a
    promotion gate.

    Parameters
    ----------
    w_xgb           : XGBoost ensemble weight; GRU weight = 1 − w_xgb.
    xgb_cal_matrix  : Calibrated XGBoost P(UP) per CPCV path,
                      shape (n_joint_rows, n_cpcv_paths).
    p_gru           : Calibrated GRU P(UP), shape (n_joint_rows,).
    fwd_ret         : h-bar forward returns, shape (n_joint_rows,).
    notional, product_type, entry_price, slippage_bps
                    : Cost-model params — defaults match the locked A3/A5
                      long-only CNC 5 bps ₹100 K notional ₹1 000 entry.
    risk_free_rate_annual, periods_per_year
                    : Risk-free rate.  daily_rfr derived from the annual rate
                      via (1 + r)^(1/T) − 1 to stay consistent with A3.

    Returns
    -------
    list[float] — one per-period net Sharpe per CPCV path (length ==
    ``xgb_cal_matrix.shape[1]``).  A path with fewer than 2 active bars
    returns ``float("nan")`` rather than a misleading zero.
    """
    ep_dec    = Decimal(str(entry_price))
    qty       = max(1, int(notional / entry_price))
    daily_rfr = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0
    unit_cost = round_trip_charge_fraction(product_type, ep_dec, ep_dec, qty)
    unit_cost += float(slippage_bps) * 2.0 / 10_000.0

    w_gru   = 1.0 - w_xgb
    p_gru_f = np.asarray(p_gru, dtype=np.float64)
    ret_f   = np.asarray(fwd_ret, dtype=np.float64)
    n_paths = xgb_cal_matrix.shape[1]

    sharpes: list[float] = []
    for pi in range(n_paths):
        p_ens  = w_xgb * xgb_cal_matrix[:, pi] + w_gru * p_gru_f
        active = p_ens >= 0.5
        if int(active.sum()) < 2:
            sharpes.append(float("nan"))
            continue
        net = np.where(active, ret_f - unit_cost, 0.0)
        sharpes.append(per_period_sharpe(net, daily_rfr=daily_rfr))

    return sharpes
