"""
Paper Trading — Hit Probability  P(hit TP before SL)
=====================================================
The single "live" per-position edge metric for the Portfolio Insight & Advise
layer.  Given a position's current price, take-profit (TP), and stop-loss (SL),
it answers one honest question:

    Starting from here, what is the probability price touches TP before SL?

Model
-----
Log-price is modelled as arithmetic Brownian motion with drift,
`d(ln S) = ν dt + σ dW`.  TP and SL are **absorbing barriers** and the position
closes at whichever is hit first, so *no time-horizon hyperparameter is needed* —
this is the horizon-free double-barrier (gambler's-ruin) result, the same scale-
function identity that underlies double-barrier option pricing in Black–Scholes.

For current price `S`, take `a = ln(SL/S) < 0` and `b = ln(TP/S) > 0` (LONG).
A single dimensionless **edge parameter** `ρ = 2ν/σ²` (drift-to-variance ratio)
governs the drift; the closed form is

    P = (1 − e^(−ρ·a)) / (e^(−ρ·b) − e^(−ρ·a)).

Zero-edge limit (`ρ → 0`): `P = −a / (b − a)` — the pure log-distance ratio
(closer to SL ⇒ lower P).  This is the honest anchor the estimate degrades to
whenever no ML signal is available.

ML fusion = the drift
---------------------
`ρ` is derived from the ensemble's calibrated `prob_up`:

    ρ = edge_lambda · (2·prob_up − 1)      ⇒      ρ ∈ [−edge_lambda, +edge_lambda]

Because `(2·prob_up − 1) ∈ [−1, 1]`, `edge_lambda` is *itself* the bound on ρ —
no separate clamp is required.  By design the geometric distance ratio dominates
the estimate and the ML edge only *nudges* P within that bounded band, so a
miscalibrated model can never manufacture a misleadingly confident number.
`edge_lambda` is the one hyperparameter (see `Settings.INSIGHT_EDGE_LAMBDA`).

SHORT positions
---------------
A SHORT is a LONG reflected about the price axis: profit is a *downward* move.
Reflecting log-price (`z = −ln(S)`) maps the stop above to `a = ln(S/SL) < 0`,
the target below to `b = ln(S/TP) > 0`, and flips the profit-direction edge to
`−(2·prob_up − 1)`.  Both sides then share one closed form.

Numerical stability
-------------------
The textbook expression overflows for large `|ρ·a|` / `|ρ·b|`.  We factor it into
two algebraically-exact `expm1` forms and pick the branch by the sign of ρ so that
**every** exponential argument is ≤ 0 — bounded in `(−1, 0]`, never overflowing,
with strictly non-zero denominators.  The two branches meet the zero-edge limit
continuously.

Contract
--------
Returns a probability in `[0, 1]`, or `None` when the metric is not defined:
  • either barrier missing            → None   (this is a *two-barrier* metric)
  • non-finite / non-positive price    → None
  • structurally invalid barriers      → None   (LONG needs TP > SL, SHORT TP < SL)
A missing/stale `prob_up` is **not** undefined — it degrades to the neutral
zero-edge estimate (ρ = 0); the caller flags staleness for the UI to de-emphasise.
Programmer-contract violations (`edge_lambda < 0`, unknown `side`) raise
`ValueError` so misconfiguration fails fast rather than returning a silent number.

Pure, dependency-light, and microsecond-cheap — safe to call per position on the
P&L recompute path.
"""
from __future__ import annotations

import math

from app.schemas.paper_trading import PositionSide

__all__ = ["hit_tp_before_sl"]


# ──────────────────────────────────────────────────────────────────────────────
# Tuning constant
# ──────────────────────────────────────────────────────────────────────────────

# ρ below this magnitude is treated as the driftless limit, where the closed form
# reduces to the pure log-distance ratio.  1e-9 sits far below any ρ the calibrated
# ML edge can produce yet safely above float rounding noise, so the drift and zero-
# edge branches join continuously with no discontinuity at ρ → 0.
_RHO_ZERO_TOL: float = 1e-9


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def hit_tp_before_sl(
    current_price: float,
    tp: float | None,
    sl: float | None,
    side: PositionSide | str,
    prob_up: float | None,
    *,
    edge_lambda: float,
) -> float | None:
    """
    Probability that price touches the take-profit before the stop-loss.

    Parameters
    ----------
    current_price : Live/last price of the instrument (must be finite and > 0).
    tp            : Take-profit barrier price. ``None`` ⇒ metric undefined ⇒ ``None``.
    sl            : Stop-loss barrier price.   ``None`` ⇒ metric undefined ⇒ ``None``.
    side          : ``PositionSide.LONG`` / ``"LONG"`` or ``PositionSide.SHORT`` /
                    ``"SHORT"``.  Determines profit direction and drift sign.
    prob_up       : Ensemble-calibrated probability the instrument's price rises,
                    in ``[0, 1]``.  ``None`` (or non-finite) ⇒ neutral ρ = 0 (the
                    estimate degrades to the pure distance ratio).
    edge_lambda   : Edge sensitivity (keyword-only). Bounds ρ to ``[−edge_lambda,
                    +edge_lambda]``. ``0.0`` disables the ML tilt entirely. Sourced
                    from ``Settings.INSIGHT_EDGE_LAMBDA``. Must be finite and ≥ 0.

    Returns
    -------
    float | None
        Probability in ``[0, 1]``, or ``None`` when the metric is undefined
        (missing barrier, non-positive/non-finite price, or a structurally
        invalid barrier configuration).

    Raises
    ------
    ValueError
        If ``edge_lambda`` is negative or non-finite, or ``side`` is not
        LONG/SHORT — programmer-contract violations that must fail fast.
    """
    # ── Programmer-contract validation (fail fast; caller/config errors) ───────
    if not math.isfinite(edge_lambda) or edge_lambda < 0.0:
        raise ValueError(
            f"edge_lambda must be a finite, non-negative float, got {edge_lambda!r}."
        )
    side_norm = (side.value if isinstance(side, PositionSide) else str(side)).strip().upper()
    if side_norm not in ("LONG", "SHORT"):
        raise ValueError(f"side must be LONG or SHORT, got {side!r}.")

    # ── Barrier availability — this is a *two-barrier* metric; abstain if either
    #    is absent (locked decision: return None, never a one-sided proxy) ───────
    if tp is None or sl is None:
        return None

    # ── Domain guards — log() needs strictly positive, finite prices ───────────
    if not (math.isfinite(current_price) and math.isfinite(tp) and math.isfinite(sl)):
        return None
    if current_price <= 0.0 or tp <= 0.0 or sl <= 0.0:
        return None

    # ── Reject structurally invalid barrier configs (corrupt data → abstain,
    #    never guess). LONG target sits above stop; SHORT target below stop ──────
    if side_norm == "LONG" and tp <= sl:
        return None
    if side_norm == "SHORT" and tp >= sl:
        return None

    # ── Log-distances + drift, reflected so both sides share one closed form ───
    #   LONG : profit is upward.   a = ln(SL/S) < 0, b = ln(TP/S) > 0.
    #   SHORT: profit is downward. Reflect the price axis (z = −ln S):
    #          a = ln(S/SL) < 0, b = ln(S/TP) > 0, and the edge flips sign.
    edge = _prob_up_to_edge(prob_up)
    if side_norm == "LONG":
        a = math.log(sl / current_price)
        b = math.log(tp / current_price)
        rho = edge_lambda * edge
    else:  # SHORT
        a = math.log(current_price / sl)
        b = math.log(current_price / tp)
        rho = edge_lambda * -edge

    # ── Absorbing-barrier guards: price already at/through a barrier ───────────
    # (Mutually exclusive here, since a valid config guarantees TP and SL straddle
    #  in log-space; both are honest terminal values, not fabrications.)
    if b <= 0.0:            # price has already reached/passed the take-profit
        return 1.0
    if a >= 0.0:            # price has already reached/passed the stop-loss
        return 0.0

    # ── Horizon-free double-barrier probability (numerically stable) ───────────
    return _double_barrier_prob(a, b, rho)


# ──────────────────────────────────────────────────────────────────────────────
# Internals
# ──────────────────────────────────────────────────────────────────────────────

def _prob_up_to_edge(prob_up: float | None) -> float:
    """
    Map calibrated ``prob_up`` to the signed edge ``2·prob_up − 1 ∈ [−1, 1]``.

    A missing or non-finite probability yields a neutral ``0.0`` (zero edge), so
    the estimate degrades gracefully to the pure log-distance ratio rather than
    fabricating a directional tilt.  ``prob_up`` is clamped to ``[0, 1]`` to absorb
    tiny calibration overshoot without distorting the edge.
    """
    if prob_up is None or not math.isfinite(prob_up):
        return 0.0
    p = min(1.0, max(0.0, prob_up))
    return 2.0 * p - 1.0


def _double_barrier_prob(a: float, b: float, rho: float) -> float:
    """
    Closed-form P(hit ``b`` before ``a``) for drifted ABM, numerically stable.

    Preconditions (guaranteed by the caller): ``a < 0 < b`` and ``rho`` finite.

    Every ``expm1`` argument below is ≤ 0 — the ratio stays finite for any finite
    ``rho`` with strictly non-zero denominators — so there is no overflow branch to
    guard.  The result is exact in ``[0, 1]``; the final clamp only removes float
    rounding dust.
    """
    if abs(rho) < _RHO_ZERO_TOL:
        # Driftless limit: pure log-distance ratio.
        p = -a / (b - a)
    elif rho > 0.0:
        # Factor e^(−ρ·a): both arguments (ρ·a, ρ·(a−b)) are < 0.
        p = math.expm1(rho * a) / math.expm1(rho * (a - b))
    else:
        # Factor e^(−ρ·b): both arguments (ρ·b, ρ·(b−a)) are < 0.
        p = 1.0 - math.expm1(rho * b) / math.expm1(rho * (b - a))
    return min(1.0, max(0.0, p))
