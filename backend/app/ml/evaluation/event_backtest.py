"""
F1 — Event-Driven Backtest Engine
==================================
Final pre-capital validation layer that sits behind the CPCV+DSR gate (A3/A6).

While the vectorized backtester (backtest.py, A3) assumes same-bar fills and
derives charges from a single representative round-trip fraction, this engine
simulates the execution lifecycle with greater fidelity across three dimensions:

  1. **Execution latency** — configurable bar lag between signal and fill
     (default 1 bar: signal at close T → entry fill at next bar T+1). This
     matches realistic CNC end-of-day NSE equity execution where today's model
     output drives tomorrow's order, not an intraday same-bar fill.

  2. **Per-trade charge accounting** — BUY and SELL legs computed separately by
     ``calculate_charges`` using the actual simulated fill prices (entry at the
     representative price; exit at entry × (1 + fwd_return)).  The vectorized
     model uses a single fraction at entry==exit; here the exit leg is re-priced
     per trade, giving a more faithful charge drag for trades with large moves.

  3. **Symbol-level temporal isolation** — when CPCV OOF paths carry per-row
     symbol metadata (the A5-era path format), each symbol's time series is
     simulated independently so the 1-bar lag never contaminates one symbol's
     signal with the next symbol's return.  Results are concatenated and the
     path-level Sharpe is computed on the pooled stream, matching the boundary
     that ``compute_dsr_and_pbo`` uses.

Agreement assessment
--------------------
The primary acceptance criterion:

    |event_mean_pp_sr − vec_mean_pp_sr|
    ─────────────────────────────────── ≤ tolerance
       max(|vec_mean_pp_sr|, 0.01)

where ``pp_sr`` = per-period (un-annualised) Sharpe, matching the input to the
DSR formula.  Production tolerance = 0.20 (20%).  Divergence beyond this emits
a WARNING in the C2 Promotion Report; it is NOT a hard promotion block — that
responsibility belongs entirely to the A3/A6 gate.

At ``latency_bars=0`` the engine degenerates to the vectorized model (same
signal–return alignment, same position mapping); the only residual difference
is charge re-pricing on each trade's actual exit price.  Agreement at latency=0
should be < 5% — the acceptance test uses this as the correctness proof.

At ``latency_bars=1`` (production default) signals shift one bar forward, so
the engine captures a realistic honesty premium; moderate divergence from the
vectorized estimate is expected and informative.

Performance
-----------
Charge coefficients per product type are derived once at call time from the
official ``calculate_charges`` function (exact Decimal arithmetic) and cached
as floats.  The inner simulation loop uses fully vectorised NumPy operations;
``SimulatedFill`` objects are built from the computed arrays with a single
Python pass over active-bar indices only — avoiding repeated Decimal calls in
the hot path while keeping per-fill audit detail available.

References
----------
- López de Prado, M. (2018). *Advances in Financial Machine Learning*, ch. 8
  (event-driven simulation philosophy).
- NSE statutory charge schedule: SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/74.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import numpy as np

from app.ml.evaluation.backtest import (
    binary_to_positions,
    per_period_sharpe,
)
from app.services.paper_trading.charge_calculator import calculate_charges

logger = logging.getLogger(__name__)

# Minimum price fraction used to floor simulated exit prices when a forward
# return ≤ -1.0 would imply a zero or negative price.  0.1% of entry keeps
# Decimal arithmetic well-defined while acknowledging a near-total-loss event.
_MIN_EXIT_FRACTION: float = 0.001

# Module-level coefficient cache: {product_type → {"buy": float, "sell": float}}
# Populated on first call to _get_charge_coefficients(); shared across all paths.
_COEFF_CACHE: dict[str, dict[str, float]] = {}


# ──────────────────────────────────────────────────────────────────────────────
# Charge coefficient derivation  (called once per product type, then cached)
# ──────────────────────────────────────────────────────────────────────────────

def _derive_coefficient(transaction_type: str, product_type: str) -> float:
    """Return the charge fraction per unit of turnover for one fill leg.

    Derived by calling ``calculate_charges`` once at a round reference price
    (₹1000, qty=1) using the official Decimal arithmetic, then dividing by
    turnover.  Because every statutory charge is strictly proportional to
    turnover (no fixed per-trade fees), this coefficient applies at any price
    and quantity.

    Coupling to ``calculate_charges`` ensures that any update to the charge
    schedule in ``charge_calculator.py`` is automatically reflected here
    without requiring a separate maintenance step.
    """
    ref_price = Decimal("1000")
    ref_qty   = 1
    ch = calculate_charges(transaction_type, product_type, ref_price, ref_qty)
    return float(ch.total_charges) / (float(ref_price) * ref_qty)


def _get_charge_coefficients(product_type: str) -> tuple[float, float]:
    """Return ``(buy_coeff, sell_coeff)`` for the given ``product_type``.

    Cached on first call.  Re-entrancy safe (module-level dict).

    Returns
    -------
    buy_coeff  : total charges as a fraction of buy-side turnover.
    sell_coeff : total charges as a fraction of sell-side turnover.
    """
    pt = product_type.upper()
    if pt not in _COEFF_CACHE:
        _COEFF_CACHE[pt] = {
            "buy":  _derive_coefficient("BUY",  pt),
            "sell": _derive_coefficient("SELL", pt),
        }
    return _COEFF_CACHE[pt]["buy"], _COEFF_CACHE[pt]["sell"]


# ──────────────────────────────────────────────────────────────────────────────
# Result types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SimulatedFill:
    """Immutable record of one completed round-trip simulated trade.

    Attributes
    ----------
    bar_signal : Index (into the original path array) where the signal fired.
    bar_entry  : Index where the entry fill was simulated
                 (bar_signal + latency_bars).
    direction  : +1 for long, −1 for short.
    entry_price: Representative entry price used for charge calculation (₹).
    exit_price : Simulated exit price = entry_price × (1 + fwd_return), floored.
    quantity   : Shares filled = max(1, int(notional / entry_price)).
    gross_return: direction × forward_return (pre-charges, pre-slippage).
    charge_drag : (entry_charges + exit_charges) / entry_notional.
    slippage_drag: 2 × slippage_bps / 10_000 (round-trip half-spread).
    net_return  : gross_return − charge_drag − slippage_drag.
    """

    bar_signal:    int
    bar_entry:     int
    direction:     int
    entry_price:   float
    exit_price:    float
    quantity:      int
    gross_return:  float
    charge_drag:   float
    slippage_drag: float
    net_return:    float


@dataclass(frozen=True, slots=True)
class PathSimulationResult:
    """Event-driven simulation result for a single CPCV OOF path.

    ``net_returns`` and ``gross_returns`` are 1-D float64 arrays of length
    ``n_effective = n_bars − n_symbols × latency_bars`` (flat bars are 0.0).
    Per-path Sharpe is computed on the pooled cross-symbol return stream,
    matching the boundary used by ``compute_dsr_and_pbo``.
    """

    n_bars:                  int    # total rows in the original path dict
    n_fills:                 int    # count of non-flat bars that generated a trade
    fills:                   tuple[SimulatedFill, ...]
    net_returns:             np.ndarray   # float64, length = n_effective
    gross_returns:           np.ndarray   # float64, length = n_effective
    net_sharpe_per_period:   float
    net_sharpe_annualised:   float
    gross_sharpe_annualised: float
    total_return:            float
    max_drawdown:            float
    win_rate:                float   # fraction of fills with net_return > 0


@dataclass
class EventBacktestReport:
    """Aggregated event-driven backtest result across all CPCV OOF paths.

    This is the primary output of :func:`run_event_backtest`.  Its
    :meth:`as_dict` representation is embedded in the C2 Promotion Report's
    ``event_backtest`` section (covered by the bundle SHA-256).
    """

    # ── Per-path statistics ────────────────────────────────────────────────────
    path_net_sharpes_per_period:   list[float]
    path_net_sharpes_annualised:   list[float]
    path_gross_sharpes_annualised: list[float]
    path_total_returns:            list[float]
    path_n_fills:                  list[int]
    path_win_rates:                list[float]
    path_max_drawdowns:            list[float]

    # ── Aggregated across paths ────────────────────────────────────────────────
    mean_net_sharpe_per_period:   float
    mean_net_sharpe_annualised:   float
    std_net_sharpe_per_period:    float
    total_fills:                  int
    mean_win_rate:                float
    mean_max_drawdown:            float
    mean_total_return:            float

    # ── Agreement with A3 vectorized ──────────────────────────────────────────
    vectorized_mean_sharpe_per_period: float | None  # None → not provided
    sharpe_divergence_absolute:        float | None
    sharpe_divergence_relative_pct:    float | None
    agreement_status:   str    # "PASS" | "WARNING" | "INSUFFICIENT_DATA"
    agreement_tolerance_pct: float

    # ── Configuration record ───────────────────────────────────────────────────
    latency_bars:     int
    mode:             str
    product_type:     str
    notional:         float
    entry_price:      float
    slippage_bps:     float
    periods_per_year: int
    n_paths:          int

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable representation for inclusion in the C2 report."""
        return {
            "path_net_sharpes_per_period":   self.path_net_sharpes_per_period,
            "path_net_sharpes_annualised":   self.path_net_sharpes_annualised,
            "path_gross_sharpes_annualised": self.path_gross_sharpes_annualised,
            "path_total_returns":            self.path_total_returns,
            "path_n_fills":                  self.path_n_fills,
            "path_win_rates":                self.path_win_rates,
            "path_max_drawdowns":            self.path_max_drawdowns,
            "mean_net_sharpe_per_period":    self.mean_net_sharpe_per_period,
            "mean_net_sharpe_annualised":    self.mean_net_sharpe_annualised,
            "std_net_sharpe_per_period":     self.std_net_sharpe_per_period,
            "total_fills":                   self.total_fills,
            "mean_win_rate":                 self.mean_win_rate,
            "mean_max_drawdown":             self.mean_max_drawdown,
            "mean_total_return":             self.mean_total_return,
            "vectorized_mean_sharpe_per_period":
                self.vectorized_mean_sharpe_per_period,
            "sharpe_divergence_absolute":    self.sharpe_divergence_absolute,
            "sharpe_divergence_relative_pct":
                self.sharpe_divergence_relative_pct,
            "agreement_status":             self.agreement_status,
            "agreement_tolerance_pct":      self.agreement_tolerance_pct,
            "latency_bars":                 self.latency_bars,
            "mode":                         self.mode,
            "product_type":                 self.product_type,
            "notional":                     self.notional,
            "entry_price":                  self.entry_price,
            "slippage_bps":                 self.slippage_bps,
            "periods_per_year":             self.periods_per_year,
            "n_paths":                      self.n_paths,
        }


# ──────────────────────────────────────────────────────────────────────────────
# Core simulation  (vectorised inner loop)
# ──────────────────────────────────────────────────────────────────────────────

def _simulate_sequence(
    proba:   np.ndarray,
    fwd_ret: np.ndarray,
    *,
    latency_bars:   int,
    mode:           str,
    entry_price:    float,
    notional:       float,
    quantity:       int,
    slippage_drag:  float,
    buy_coeff:      float,
    sell_coeff:     float,
    notional_ratio: float,
    daily_rfr:      float,
    periods_per_year: int,
) -> tuple[np.ndarray, np.ndarray, list[SimulatedFill]]:
    """Simulate one contiguous time series with a configurable latency shift.

    The inner return and charge computations are fully vectorised; the Python
    loop only constructs ``SimulatedFill`` objects for the active-bar subset.

    Parameters
    ----------
    proba        : P(UP) per bar, shape (n,).
    fwd_ret      : h-bar forward return per bar, shape (n,).
    latency_bars : Bars between signal and fill.  0 = same-bar (vectorized
                   parity); 1 = next-bar (production default).
    mode         : "long_only" or "long_short".
    entry_price  : Representative entry price (₹) for charge and exit-price calc.
    notional     : Position size (₹) for quantity derivation and drag normalisation.
    quantity     : max(1, int(notional / entry_price)) — pre-computed.
    slippage_drag: 2 × slippage_bps / 10_000 — pre-computed.
    buy_coeff    : Charge fraction per unit buy-side turnover — pre-computed.
    sell_coeff   : Charge fraction per unit sell-side turnover — pre-computed.
    notional_ratio: (entry_price × quantity) / notional ≈ 1.0 — pre-computed.

    Returns
    -------
    (net_returns, gross_returns, fills) where both arrays have length
    max(0, n − latency_bars).
    """
    n = len(proba)
    effective_n = n - latency_bars
    if effective_n <= 0:
        empty: np.ndarray = np.zeros(0, dtype=np.float64)
        return empty, empty, []

    # ── Signal → position (A3 threshold: proba ≥ 0.5 → UP/long) ─────────────
    pred      = (np.asarray(proba, dtype=np.float64) >= 0.5).astype(np.int8)
    positions = binary_to_positions(pred, mode=mode)

    # ── Latency shift: signal at bar i executed at bar i + latency_bars ───────
    eff_pos     = positions[:effective_n] if latency_bars > 0 else positions
    eff_fwd_ret = (
        np.asarray(fwd_ret, dtype=np.float64)[latency_bars:]
        if latency_bars > 0
        else np.asarray(fwd_ret, dtype=np.float64)
    )

    # ── Active-bar masks ──────────────────────────────────────────────────────
    long_mask  = eff_pos == np.int8(1)
    short_mask = eff_pos == np.int8(-1)
    active     = long_mask | short_mask

    gross_rets = np.zeros(effective_n, dtype=np.float64)
    net_rets   = np.zeros(effective_n, dtype=np.float64)

    if not active.any():
        return net_rets, gross_rets, []

    # ── Gross returns (vectorised) ────────────────────────────────────────────
    gross_rets[active] = eff_pos[active].astype(np.float64) * eff_fwd_ret[active]

    # ── Exit prices: entry × (1 + fwd_ret), floored at 0.1% of entry ─────────
    exit_prices = np.maximum(
        entry_price * (1.0 + eff_fwd_ret),
        entry_price * _MIN_EXIT_FRACTION,
    )

    # ── Charge drag (vectorised, coefficients derived from calculate_charges) ─
    # LONG  (BUY entry, SELL exit): charge = notional_ratio × [buy_coeff + sell_coeff × (1+ret)]
    # SHORT (SELL entry, BUY  exit): charge = notional_ratio × [sell_coeff + buy_coeff × (1+ret)]
    #
    # The entry-leg coefficient is constant (entry_price is fixed); only the
    # exit-leg varies with fwd_ret.  This is algebraically equivalent to the
    # per-fill Decimal arithmetic in calculate_charges but avoids Python-level
    # Decimal overhead in the inner loop.
    charge_drag = np.zeros(effective_n, dtype=np.float64)
    if long_mask.any():
        charge_drag[long_mask] = notional_ratio * (
            buy_coeff + sell_coeff * (1.0 + eff_fwd_ret[long_mask])
        )
    if short_mask.any():
        charge_drag[short_mask] = notional_ratio * (
            sell_coeff + buy_coeff * (1.0 + eff_fwd_ret[short_mask])
        )

    # ── Net returns ───────────────────────────────────────────────────────────
    net_rets[active] = gross_rets[active] - charge_drag[active] - slippage_drag

    # ── Build SimulatedFill records (Python loop over active indices only) ────
    active_idx = np.flatnonzero(active)
    fills: list[SimulatedFill] = [
        SimulatedFill(
            bar_signal=int(i),
            bar_entry=int(i) + latency_bars,
            direction=int(eff_pos[i]),
            entry_price=entry_price,
            exit_price=float(exit_prices[i]),
            quantity=quantity,
            gross_return=float(gross_rets[i]),
            charge_drag=float(charge_drag[i]),
            slippage_drag=slippage_drag,
            net_return=float(net_rets[i]),
        )
        for i in active_idx
    ]

    return net_rets, gross_rets, fills


def _simulate_path(
    path: dict[str, Any],
    *,
    latency_bars:     int,
    mode:             str,
    notional:         float,
    product_type:     str,
    entry_price:      float,
    slippage_bps:     float,
    daily_rfr:        float,
    periods_per_year: int,
) -> PathSimulationResult:
    """Simulate one CPCV OOF path, grouping by symbol when metadata is present.

    When the path dict contains ``"symbol"`` and ``"timestamp"`` arrays (A5+
    format), each symbol's rows are extracted, sorted by timestamp, and
    simulated independently so the latency shift is applied within each
    symbol's chronological sequence.  Results are concatenated before
    path-level statistics are computed — matching the aggregation boundary
    that ``compute_dsr_and_pbo`` uses.

    When symbol metadata is absent, the full path is treated as one sequence.
    """
    proba_all   = np.asarray(path["proba"],          dtype=np.float32)
    fwd_ret_all = np.asarray(path["forward_return"], dtype=np.float32)
    n_total     = len(proba_all)

    # ── Pre-compute shared constants ──────────────────────────────────────────
    quantity      = max(1, int(notional / entry_price))
    notional_actual = float(entry_price) * quantity   # actual position value (₹)
    notional_ratio  = notional_actual / notional       # ≈ 1.0
    slippage_drag   = float(slippage_bps * 2.0 / 10_000.0)
    buy_coeff, sell_coeff = _get_charge_coefficients(product_type)

    sim_kw: dict[str, Any] = dict(
        latency_bars=latency_bars,
        mode=mode,
        entry_price=entry_price,
        notional=notional,
        quantity=quantity,
        slippage_drag=slippage_drag,
        buy_coeff=buy_coeff,
        sell_coeff=sell_coeff,
        notional_ratio=notional_ratio,
        daily_rfr=daily_rfr,
        periods_per_year=periods_per_year,
    )

    # ── Per-symbol or whole-path simulation ───────────────────────────────────
    all_net:   list[np.ndarray]     = []
    all_gross: list[np.ndarray]     = []
    all_fills: list[SimulatedFill]  = []

    symbol_arr = path.get("symbol")
    ts_arr     = path.get("timestamp")

    if (
        symbol_arr is not None
        and ts_arr  is not None
        and len(symbol_arr) == n_total
    ):
        # Per-symbol mode: sort within each symbol to guarantee chronological order
        symbols = np.asarray(symbol_arr)
        ts_i64  = np.asarray(ts_arr, dtype="datetime64[ns]").view(np.int64)

        for sym in np.unique(symbols):
            mask    = symbols == sym
            s_proba  = proba_all[mask]
            s_fwdret = fwd_ret_all[mask]
            s_ts     = ts_i64[mask]

            order    = np.argsort(s_ts, kind="stable")
            net_r, gross_r, sym_fills = _simulate_sequence(
                s_proba[order], s_fwdret[order], **sim_kw
            )
            all_net.append(net_r)
            all_gross.append(gross_r)
            all_fills.extend(sym_fills)
    else:
        net_r, gross_r, path_fills = _simulate_sequence(
            proba_all, fwd_ret_all, **sim_kw
        )
        all_net.append(net_r)
        all_gross.append(gross_r)
        all_fills.extend(path_fills)

    # ── Concatenate returns across symbols / whole path ───────────────────────
    net_returns   = np.concatenate(all_net)   if all_net   else np.zeros(0, np.float64)
    gross_returns = np.concatenate(all_gross) if all_gross else np.zeros(0, np.float64)

    # ── Path-level statistics ─────────────────────────────────────────────────
    net_pp_sr    = per_period_sharpe(net_returns,   daily_rfr=daily_rfr)
    gross_pp_sr  = per_period_sharpe(gross_returns, daily_rfr=daily_rfr)
    net_ann_sr   = net_pp_sr   * (periods_per_year ** 0.5)
    gross_ann_sr = gross_pp_sr * (periods_per_year ** 0.5)

    total_ret = (
        float(np.cumprod(1.0 + net_returns)[-1] - 1.0)
        if len(net_returns) > 0
        else 0.0
    )

    if len(net_returns) > 0:
        cum    = np.cumprod(1.0 + net_returns)
        cummax = np.maximum.accumulate(cum)
        max_dd = float(abs(((cum - cummax) / np.where(cummax > 0, cummax, 1.0)).min()))
    else:
        max_dd = 0.0

    if all_fills:
        fill_nets = np.array([f.net_return for f in all_fills])
        win_rate  = float((fill_nets > 0.0).sum() / len(fill_nets))
    else:
        win_rate = 0.0

    return PathSimulationResult(
        n_bars=n_total,
        n_fills=len(all_fills),
        fills=tuple(all_fills),
        net_returns=net_returns,
        gross_returns=gross_returns,
        net_sharpe_per_period=net_pp_sr,
        net_sharpe_annualised=net_ann_sr,
        gross_sharpe_annualised=gross_ann_sr,
        total_return=total_ret,
        max_drawdown=max_dd,
        win_rate=win_rate,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def run_event_backtest(
    cpcv_oof_paths: list[dict[str, Any]],
    *,
    vectorized_path_sharpes_per_period: list[float] | None = None,
    latency_bars:     int   = 1,
    mode:             Literal["long_only", "long_short"] = "long_only",
    notional:         float = 100_000.0,
    product_type:     str   = "CNC",
    entry_price:      float = 1_000.0,
    slippage_bps:     float = 5.0,
    periods_per_year: int   = 252,
    risk_free_rate_annual: float = 0.05,
    agreement_tolerance:   float = 0.20,
) -> EventBacktestReport:
    """Run the event-driven backtest across all CPCV OOF paths.

    Parameters
    ----------
    cpcv_oof_paths :
        List of CPCV OOF path dicts.  Same format consumed by
        ``compute_dsr_and_pbo``; each dict must contain at minimum:
          - ``"proba"``          : float32 array of P(UP) scores.
          - ``"forward_return"`` : float32 array of h-bar forward returns.
        Optionally:
          - ``"symbol"``    : str array of ticker symbols per row.
          - ``"timestamp"`` : datetime64[ns] array per row.
        Paths with fewer than 2 observations are skipped with a WARNING.

    vectorized_path_sharpes_per_period :
        Per-period Sharpe ratios from the A3 vectorized backtest —
        ``compute_dsr_and_pbo(paths, ...)["path_sharpes_per_period"]``.
        When provided, the agreement check compares event-driven mean
        per-period Sharpe to this reference.  When ``None`` or empty,
        ``agreement_status`` is set to ``"INSUFFICIENT_DATA"``.

    latency_bars :
        Bars between signal generation and fill execution.  Default 1 (signal
        at T → fill at T+1) matches CNC end-of-day NSE equity execution.
        Use 0 to validate engine correctness against the vectorized estimate
        (agreement should be < 5% at latency=0).

    mode :
        ``"long_only"``  — UP → long; DOWN/HOLD → flat (no short exposure).
        ``"long_short"`` — UP → long; DOWN → short; HOLD → flat.

    notional :
        Position size in ₹.  Used to derive ``quantity`` and normalise the
        charge drag fraction.  Default 100,000 (₹1 lakh) — matches A3/A5.

    product_type :
        ``"CNC"`` (delivery, default) or ``"MIS"`` (intraday).

    entry_price :
        Representative entry price (₹) for quantity derivation and as the
        base from which exit prices are re-simulated.  Default ₹1,000.

    slippage_bps :
        One-way half-spread estimate in basis points.  Round-trip slippage
        = 2 × slippage_bps / 10,000.  Default 5 bps (matches A3).

    periods_per_year :
        Trading sessions per year for Sharpe annualisation.  Default 252.

    risk_free_rate_annual :
        Annual risk-free rate for excess-return Sharpe calculation.  Default
        0.05 (5% — matches A3).

    agreement_tolerance :
        Relative divergence threshold for PASS.  Default 0.20 (20%).
        Divergence = |event_mean_pp_sr − vec_mean_pp_sr| / max(|vec_mean_pp_sr|, 0.01).

    Returns
    -------
    :class:`EventBacktestReport`

    Raises
    ------
    ValueError
        If ``cpcv_oof_paths`` is empty, if ``latency_bars`` < 0, or if all
        paths have fewer than 2 observations.
    """
    if not cpcv_oof_paths:
        raise ValueError("cpcv_oof_paths must be non-empty.")
    if latency_bars < 0:
        raise ValueError(f"latency_bars must be ≥ 0, got {latency_bars}.")

    daily_rfr = (1.0 + risk_free_rate_annual) ** (1.0 / periods_per_year) - 1.0

    # ── Simulate each path ────────────────────────────────────────────────────
    path_results: list[PathSimulationResult] = []
    for i, path in enumerate(cpcv_oof_paths):
        if len(path.get("proba", [])) < 2:
            logger.warning("F1: path %d has < 2 observations — skipping.", i)
            continue
        result = _simulate_path(
            path,
            latency_bars=latency_bars,
            mode=mode,
            notional=notional,
            product_type=product_type,
            entry_price=entry_price,
            slippage_bps=slippage_bps,
            daily_rfr=daily_rfr,
            periods_per_year=periods_per_year,
        )
        path_results.append(result)

    if not path_results:
        raise ValueError(
            "All CPCV OOF paths had fewer than 2 observations. "
            "Cannot compute event-driven backtest statistics."
        )

    # ── Aggregate path statistics ─────────────────────────────────────────────
    pp_srs       = [r.net_sharpe_per_period   for r in path_results]
    ann_srs      = [r.net_sharpe_annualised   for r in path_results]
    gross_ann_srs = [r.gross_sharpe_annualised for r in path_results]
    total_rets   = [r.total_return  for r in path_results]
    n_fills_list = [r.n_fills       for r in path_results]
    win_rates    = [r.win_rate      for r in path_results]
    max_dds      = [r.max_drawdown  for r in path_results]

    pp_arr       = np.array(pp_srs, dtype=np.float64)
    mean_pp_sr   = float(pp_arr.mean())
    mean_ann_sr  = float(np.mean(ann_srs))
    std_pp_sr    = float(pp_arr.std(ddof=1)) if len(pp_arr) > 1 else 0.0

    # ── Agreement check against A3 vectorized reference ───────────────────────
    vec_mean:           float | None = None
    div_abs:            float | None = None
    div_rel_pct:        float | None = None
    agreement_status:   str          = "INSUFFICIENT_DATA"

    if vectorized_path_sharpes_per_period:
        vec_mean  = float(np.mean(vectorized_path_sharpes_per_period))
        div_abs   = abs(mean_pp_sr - vec_mean)
        denom     = max(abs(vec_mean), 0.01)
        div_rel_pct = div_abs / denom * 100.0

        if div_rel_pct <= agreement_tolerance * 100.0:
            agreement_status = "PASS"
        else:
            agreement_status = "WARNING"
            logger.warning(
                "F1 agreement check: event_mean_pp_sr=%.4f  "
                "vec_mean_pp_sr=%.4f  divergence=%.1f%%  tolerance=%.1f%%  "
                "→ WARNING (not a promotion block — informational only)",
                mean_pp_sr, vec_mean, div_rel_pct, agreement_tolerance * 100.0,
            )

    report = EventBacktestReport(
        path_net_sharpes_per_period=   [float(s) for s in pp_srs],
        path_net_sharpes_annualised=   [float(s) for s in ann_srs],
        path_gross_sharpes_annualised= [float(s) for s in gross_ann_srs],
        path_total_returns=            [float(r) for r in total_rets],
        path_n_fills=                  [int(n)   for n in n_fills_list],
        path_win_rates=                [float(w) for w in win_rates],
        path_max_drawdowns=            [float(d) for d in max_dds],
        mean_net_sharpe_per_period=    mean_pp_sr,
        mean_net_sharpe_annualised=    mean_ann_sr,
        std_net_sharpe_per_period=     std_pp_sr,
        total_fills=                   sum(n_fills_list),
        mean_win_rate=                 float(np.mean(win_rates)),
        mean_max_drawdown=             float(np.mean(max_dds)),
        mean_total_return=             float(np.mean(total_rets)),
        vectorized_mean_sharpe_per_period= vec_mean,
        sharpe_divergence_absolute=        div_abs,
        sharpe_divergence_relative_pct=    div_rel_pct,
        agreement_status=               agreement_status,
        agreement_tolerance_pct=        agreement_tolerance * 100.0,
        latency_bars=                   latency_bars,
        mode=                           mode,
        product_type=                   product_type,
        notional=                       notional,
        entry_price=                    entry_price,
        slippage_bps=                   slippage_bps,
        periods_per_year=               periods_per_year,
        n_paths=                        len(path_results),
    )

    logger.info(
        "F1 event backtest complete: paths=%d  mean_pp_SR=%.4f  "
        "mean_ann_SR=%.4f  total_fills=%d  agreement=%s  latency_bars=%d",
        report.n_paths,
        report.mean_net_sharpe_per_period,
        report.mean_net_sharpe_annualised,
        report.total_fills,
        report.agreement_status,
        report.latency_bars,
    )

    return report
