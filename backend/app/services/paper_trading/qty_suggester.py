"""
Paper Trading — Quantity Suggester
=====================================
Computes the system-recommended share quantity for a paper order derived from
a TradeSuggestion, based on the portfolio's capital allocation and risk settings.

Risk-based sizing model
-----------------------
The formula implements a simplified Kelly-style position sizer anchored to a
user-defined percentage of current cash risked per trade:

    capital_at_risk  = current_cash × (risk_per_trade_pct / 100)
    raw_qty          = capital_at_risk / stop_distance
    stop_distance    = |entry_price − stop_loss|

    suggested_qty    = clamp(floor(raw_qty), low=1, high=max_affordable_qty)
    max_affordable   = floor(current_cash / entry_price)

Fallback (no stop_loss on suggestion)
--------------------------------------
When the source TradeSuggestion has no stop_loss, an exact position size
cannot be computed.  The fallback applies a conservative fixed-risk assumption:
    fallback_stop_distance = entry_price × FALLBACK_STOP_PCT

This is intentionally conservative (1% of entry price) and a warning note is
included in the response so the UI can surface it to the user.

Return value transparency
--------------------------
The response includes every intermediate value used in the calculation
(capital_at_risk, stop_distance, risk_pct_of_cash) so the UI can display a
clear rationale panel rather than showing an opaque number.  This is important
for building user trust in the system's suggestions.
"""
from __future__ import annotations

import logging
import math
from decimal import Decimal

from app.exceptions import ValidationError
from app.schemas.paper_trading import QtySuggestionResponse

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration constants
# ──────────────────────────────────────────────────────────────────────────────

# Fallback stop distance when TradeSuggestion has no stop_loss.
# Set conservatively at 1% of entry price so the suggested quantity remains
# sensible even without a defined stop.
_FALLBACK_STOP_PCT: Decimal = Decimal("0.01")

# Absolute minimum quantity — never suggest zero shares
_MIN_QTY: int = 1

# Safety cap: suggested qty will never exceed this fraction of max_affordable
# to prevent the model from suggesting an all-in position even at low risk_pct.
# (e.g. if stop_distance is tiny, raw_qty could massively exceed affordable)
_MAX_QTY_CAP_FRACTION: Decimal = Decimal("1.0")  # 100% of affordable (hard cap)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def suggest_quantity(
    current_cash: Decimal,
    risk_per_trade_pct: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
    conviction_scale: float = 1.0,
) -> QtySuggestionResponse:
    """
    Compute the system-suggested quantity for a paper order.

    Parameters
    ----------
    current_cash       : Available cash in the portfolio (INR, must be > 0)
    risk_per_trade_pct : Portfolio risk setting (0 < x ≤ 10)
    entry_price        : Suggested entry price from TradeSuggestion (must be > 0)
    stop_loss          : Suggested stop-loss price from TradeSuggestion
                         (None triggers the conservative fallback)
    conviction_scale   : ML signal conviction in [0.0, 1.0] from EnsemblePredictor.
                         0.0 = signal confidence exactly at the regime threshold;
                         1.0 = maximum confidence (full Kelly sizing, default).
                         Scales raw_qty before the affordability clamp, converting
                         the binary BUY/HOLD gate into graduated position sizing.

    Returns
    -------
    QtySuggestionResponse
        Full breakdown including suggested_qty, max_affordable_qty,
        capital_at_risk, risk_pct_of_cash, stop_distance, and a note
        if the fallback stop or conviction scaling was applied.

    Raises
    ------
    ValidationError
        If entry_price ≤ 0, current_cash ≤ 0, risk_per_trade_pct is out of
        range, conviction_scale is out of range, or stop_loss ≥ entry_price.
    """
    _validate_inputs(current_cash, risk_per_trade_pct, entry_price, stop_loss, conviction_scale)

    note: str | None = None

    # ── Step 1: Resolve stop distance ─────────────────────────────────────────
    if stop_loss is not None:
        stop_distance = (entry_price - stop_loss).copy_abs()
        if stop_distance == Decimal("0"):
            raise ValidationError(
                "stop_loss equals entry_price — stop distance is zero. "
                "A valid stop_loss must differ from the entry price."
            )
    else:
        stop_distance = entry_price * _FALLBACK_STOP_PCT
        note = (
            "This suggestion has no stop_loss defined. "
            f"Quantity was estimated using a fallback stop distance of "
            f"{float(_FALLBACK_STOP_PCT * 100):.1f}% of the entry price "
            f"(₹{float(stop_distance):.2f}). "
            "Consider setting a manual stop before entering."
        )
        logger.warning(
            "Qty suggestion using fallback stop (no stop_loss on suggestion): "
            "entry=%.4f fallback_distance=%.4f",
            float(entry_price),
            float(stop_distance),
        )

    # ── Step 2: Capital at risk ────────────────────────────────────────────────
    # The maximum INR amount the user is willing to lose on this trade.
    capital_at_risk = current_cash * (risk_per_trade_pct / Decimal("100"))

    # ── Step 3: Raw quantity from risk formula ─────────────────────────────────
    raw_qty = capital_at_risk / stop_distance

    # ── Step 4: Apply conviction scaling ──────────────────────────────────────
    # Converts the binary BUY/HOLD confidence gate into graduated position
    # sizing.  A signal at exactly the regime threshold (conviction_scale=0.0)
    # results in the minimum 1-share position; a high-conviction signal
    # (conviction_scale=1.0) receives the full Kelly-computed quantity.
    # Scaling happens before the affordability clamp so the cap logic remains
    # independent of conviction.
    if conviction_scale < 1.0:
        raw_qty = raw_qty * Decimal(str(round(conviction_scale, 6)))
        if note is None:
            note = (
                f"Conviction scale of {conviction_scale:.2f} applied — position sized "
                f"to {conviction_scale * 100:.0f}% of the full Kelly recommendation "
                f"based on signal confidence relative to the volatility-regime threshold."
            )
        logger.info(
            "Conviction scaling applied: scale=%.4f raw_qty_before_scale=%.4f",
            conviction_scale,
            float(capital_at_risk / stop_distance),
        )

    raw_qty_floored = int(raw_qty.to_integral_value(rounding="ROUND_FLOOR"))

    # ── Step 5: Maximum affordable quantity ───────────────────────────────────
    # Hard upper bound: you can't buy more than cash allows.
    max_affordable_qty = int(
        (current_cash / entry_price).to_integral_value(rounding="ROUND_FLOOR")
    )
    max_affordable_qty = max(_MIN_QTY, max_affordable_qty)

    # ── Step 6: Final suggested quantity ──────────────────────────────────────
    # Clamped between 1 and max_affordable_qty.
    suggested_qty = max(_MIN_QTY, min(raw_qty_floored, max_affordable_qty))

    # ── Step 7: Derive actual risk metrics for the suggested quantity ──────────
    actual_capital_at_risk = stop_distance * Decimal(suggested_qty)
    risk_pct_of_cash = (
        (actual_capital_at_risk / current_cash * Decimal("100"))
        if current_cash > 0
        else Decimal("0")
    )

    # Flag if the suggestion was capped by affordability (only when no prior note)
    if raw_qty_floored > max_affordable_qty and note is None:
        note = (
            f"Suggested quantity was capped at {max_affordable_qty} shares "
            f"(max affordable with current cash of ₹{float(current_cash):,.2f}). "
            f"The risk formula recommended {raw_qty_floored} shares, which exceeds "
            "available capital."
        )
    elif raw_qty_floored < _MIN_QTY and note is None:
        note = (
            f"Stop distance (₹{float(stop_distance):.2f}) is too wide relative to "
            f"the capital at risk (₹{float(capital_at_risk):.2f}). "
            f"Minimum quantity of {_MIN_QTY} share(s) is suggested — "
            "consider a tighter stop or a larger risk percentage."
        )

    return QtySuggestionResponse(
        suggested_qty=suggested_qty,
        max_affordable_qty=max_affordable_qty,
        capital_at_risk=round(float(actual_capital_at_risk), 2),
        risk_pct_of_cash=round(float(risk_pct_of_cash), 4),
        entry_price=round(float(entry_price), 4),
        stop_loss=round(float(stop_loss), 4) if stop_loss is not None else round(float(entry_price - stop_distance), 4),
        stop_distance=round(float(stop_distance), 4),
        current_cash=round(float(current_cash), 2),
        risk_per_trade_pct=round(float(risk_per_trade_pct), 2),
        note=note,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ──────────────────────────────────────────────────────────────────────────────

def _validate_inputs(
    current_cash: Decimal,
    risk_per_trade_pct: Decimal,
    entry_price: Decimal,
    stop_loss: Decimal | None,
    conviction_scale: float = 1.0,
) -> None:
    """Raise ValidationError on any invalid input combination."""
    if current_cash <= Decimal("0"):
        raise ValidationError(
            f"current_cash must be positive, got {current_cash}."
        )
    if not (Decimal("0") < risk_per_trade_pct <= Decimal("10")):
        raise ValidationError(
            f"risk_per_trade_pct must be in range (0, 10], got {risk_per_trade_pct}."
        )
    if entry_price <= Decimal("0"):
        raise ValidationError(
            f"entry_price must be positive, got {entry_price}."
        )
    if not (0.0 <= conviction_scale <= 1.0):
        raise ValidationError(
            f"conviction_scale must be in [0.0, 1.0], got {conviction_scale}."
        )
    if stop_loss is not None:
        if stop_loss <= Decimal("0"):
            raise ValidationError(
                f"stop_loss must be positive, got {stop_loss}."
            )
        if stop_loss >= entry_price:
            raise ValidationError(
                f"stop_loss ({stop_loss}) must be strictly below entry_price ({entry_price}) "
                "for a long (BUY) suggestion. "
                "If this is a short position, stop_loss handling is not yet implemented."
            )
