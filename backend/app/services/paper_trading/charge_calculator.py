"""
NSE Statutory Charge Calculator
=================================
Computes the exact statutory transaction charges levied by NSE and the Indian
government on equity trades.  Used by the paper trading fill simulator to
produce realistic net P&L figures for ML feedback.

IMPORTANT — scope
-----------------
This module handles NSE equity cash segment ONLY (product types CNC and MIS).
NRML (F&O carry-forward) is reserved for a future extension; passing it raises
NotImplementedError to prevent silent mis-pricing.

Brokerage is intentionally excluded — paper trades have no broker, so the only
costs that matter are the statutory charges that any real trade would incur
regardless of broker.

Charge schedule (verified FY 2025-26)
--------------------------------------
Source: SEBI circular SEBI/HO/MRD/MRD-PoD-3/P/CIR/2024/74 (Jul 2024),
        effective October 1, 2024; NSE revised tariff filed with SEBI.

  STT — CNC delivery   : 0.1%  on each side (buy AND sell turnover separately)
  STT — MIS intraday   : 0.025% on sell-side turnover only

  NSE Exchange charge  : 0.00297% of turnover (each side)
                         (revised from 0.00335% effective Oct 1 2024)

  SEBI turnover charge : ₹10 per crore = 0.000001 (both sides)

  GST                  : 18% on (exchange_charges + sebi_charges)
                         Brokerage is ₹0, so GST base excludes it.

  Stamp duty — CNC buy : 0.015% of buy-side turnover
  Stamp duty — MIS buy : 0.003% of buy-side turnover
  Stamp duty — sell    : NIL (both CNC and MIS)

All arithmetic uses Python's Decimal with explicit ROUND_HALF_UP to avoid
IEEE-754 drift.  Results are returned rounded to 4 decimal places, matching
the NUMERIC(10,4) columns in paper_fills.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.exceptions import ValidationError

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Charge constants  (all Decimal, verified FY 2025-26)
# ──────────────────────────────────────────────────────────────────────────────

# STT
_STT_CNC_PCT: Decimal = Decimal("0.001")          # 0.1% each side
_STT_MIS_SELL_PCT: Decimal = Decimal("0.00025")   # 0.025% sell-side only

# NSE exchange transaction charge (uniform rate, post-Oct 2024)
_EXCHANGE_CHARGE_PCT: Decimal = Decimal("0.0000297")  # 0.00297%

# SEBI turnover charge: ₹10/crore = 10 / 10_000_000 = 0.000001
_SEBI_CHARGE_PCT: Decimal = Decimal("0.000001")

# GST applied to (exchange charges + SEBI charges)  — no brokerage base
_GST_RATE: Decimal = Decimal("0.18")

# Stamp duty — buy side only (Finance Act 2020, effective Jul 1 2020)
_STAMP_DUTY_CNC_BUY_PCT: Decimal = Decimal("0.00015")   # 0.015%
_STAMP_DUTY_MIS_BUY_PCT: Decimal = Decimal("0.00003")   # 0.003%

# Internal rounding quantum: 4 d.p. matches DB NUMERIC(10,4)
_DP4: Decimal = Decimal("0.0001")

# Derived zero sentinel
_ZERO: Decimal = Decimal("0")


# ──────────────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class FillCharges:
    """
    Immutable breakdown of statutory charges for a single fill.

    All monetary fields are Decimal rounded to 4 decimal places (paise
    precision to 2 significant digits beyond the paisa).

    Attributes
    ----------
    stt              : Securities Transaction Tax
    exchange_charges : NSE exchange transaction charge
    sebi_charges     : SEBI turnover charge
    gst              : Goods and Services Tax (on exchange + SEBI charges)
    stamp_duty       : Stamp duty (buy side only)
    total_charges    : Sum of all five charge components
    gross_amount     : fill_price × fill_quantity (pre-charges)
    net_amount       : gross_amount + total_charges on BUY,
                       gross_amount − total_charges on SELL
                       (represents actual cash movement)
    """

    stt: Decimal
    exchange_charges: Decimal
    sebi_charges: Decimal
    gst: Decimal
    stamp_duty: Decimal
    total_charges: Decimal
    gross_amount: Decimal
    net_amount: Decimal

    def as_dict(self) -> dict[str, float]:
        """Return all monetary fields as floats for JSON serialisation."""
        return {
            "stt": float(self.stt),
            "exchange_charges": float(self.exchange_charges),
            "sebi_charges": float(self.sebi_charges),
            "gst": float(self.gst),
            "stamp_duty": float(self.stamp_duty),
            "total_charges": float(self.total_charges),
            "gross_amount": float(self.gross_amount),
            "net_amount": float(self.net_amount),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _r(value: Decimal) -> Decimal:
    """Round to 4 decimal places with ROUND_HALF_UP."""
    return value.quantize(_DP4, rounding=ROUND_HALF_UP)


def _turnover(price: Decimal, quantity: int) -> Decimal:
    """Gross turnover = fill_price × fill_quantity."""
    return price * Decimal(quantity)


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def calculate_charges(
    transaction_type: str,
    product_type: str,
    fill_price: Decimal,
    fill_quantity: int,
) -> FillCharges:
    """
    Compute the full statutory charge breakdown for a single paper fill.

    Parameters
    ----------
    transaction_type : "BUY" or "SELL"
    product_type     : "CNC" (delivery) or "MIS" (intraday)
    fill_price       : Actual simulated fill price (must be positive)
    fill_quantity    : Number of shares filled (must be positive integer)

    Returns
    -------
    FillCharges
        Immutable breakdown with every charge component and the net cash
        impact of the fill.

    Raises
    ------
    ValidationError
        If transaction_type or product_type are invalid, or if fill_price /
        fill_quantity are non-positive.
    NotImplementedError
        If product_type is "NRML" (F&O — not yet implemented for equity).
    """
    # ── Input validation ──────────────────────────────────────────────────────
    tx = transaction_type.upper()
    pt = product_type.upper()

    if tx not in ("BUY", "SELL"):
        raise ValidationError(
            f"Invalid transaction_type '{transaction_type}'. Must be BUY or SELL."
        )
    if pt == "NRML":
        raise NotImplementedError(
            "NRML (F&O) charge calculation is not yet implemented. "
            "Equity paper trading supports CNC and MIS only."
        )
    if pt not in ("CNC", "MIS"):
        raise ValidationError(
            f"Invalid product_type '{product_type}'. Must be CNC or MIS."
        )
    if fill_price <= _ZERO:
        raise ValidationError(
            f"fill_price must be positive, got {fill_price}."
        )
    if fill_quantity <= 0:
        raise ValidationError(
            f"fill_quantity must be a positive integer, got {fill_quantity}."
        )

    is_buy = tx == "BUY"
    is_cnc = pt == "CNC"

    gross = _turnover(fill_price, fill_quantity)

    # ── STT ───────────────────────────────────────────────────────────────────
    # CNC: both sides taxed independently at 0.1%
    # MIS: sell side only at 0.025%
    if is_cnc:
        stt = _r(gross * _STT_CNC_PCT)
    else:
        # MIS — only sell side incurs STT
        stt = _r(gross * _STT_MIS_SELL_PCT) if not is_buy else _ZERO

    # ── Exchange transaction charge ───────────────────────────────────────────
    # Applied to turnover on both sides for both CNC and MIS
    exchange_charges = _r(gross * _EXCHANGE_CHARGE_PCT)

    # ── SEBI turnover charge ──────────────────────────────────────────────────
    sebi_charges = _r(gross * _SEBI_CHARGE_PCT)

    # ── GST ───────────────────────────────────────────────────────────────────
    # Base = exchange_charges + sebi_charges (brokerage is ₹0)
    gst = _r((exchange_charges + sebi_charges) * _GST_RATE)

    # ── Stamp duty ────────────────────────────────────────────────────────────
    # Applies on buy side only; sell side is NIL
    if is_buy:
        stamp_rate = _STAMP_DUTY_CNC_BUY_PCT if is_cnc else _STAMP_DUTY_MIS_BUY_PCT
        stamp_duty = _r(gross * stamp_rate)
    else:
        stamp_duty = _ZERO

    # ── Totals ────────────────────────────────────────────────────────────────
    total_charges = _r(stt + exchange_charges + sebi_charges + gst + stamp_duty)

    # Net cash impact:
    #   BUY  → you pay gross + charges  (cash outflow)
    #   SELL → you receive gross - charges  (cash inflow, reported positive)
    net_amount = _r(gross + total_charges) if is_buy else _r(gross - total_charges)

    charges = FillCharges(
        stt=stt,
        exchange_charges=exchange_charges,
        sebi_charges=sebi_charges,
        gst=gst,
        stamp_duty=stamp_duty,
        total_charges=total_charges,
        gross_amount=_r(gross),
        net_amount=net_amount,
    )

    logger.debug(
        "Charges computed: tx=%s pt=%s qty=%d price=%s "
        "total_charges=%s net=%s",
        tx, pt, fill_quantity, fill_price,
        total_charges, net_amount,
    )

    return charges


def estimate_round_trip_charges(
    product_type: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: int,
) -> tuple[FillCharges, FillCharges, Decimal]:
    """
    Compute the combined statutory charge drag for an entry + exit pair.

    Useful for the qty suggester and UI pre-trade cost display to show the
    user the full cost of opening and closing a position.

    Returns
    -------
    (entry_charges, exit_charges, total_drag)
        total_drag is the sum of both fills' total_charges.
    """
    entry = calculate_charges("BUY", product_type, entry_price, quantity)
    exit_ = calculate_charges("SELL", product_type, exit_price, quantity)
    total_drag = _r(entry.total_charges + exit_.total_charges)
    return entry, exit_, total_drag


# ──────────────────────────────────────────────────────────────────────────────
# Charge summary for display / audit
# ──────────────────────────────────────────────────────────────────────────────

def format_charge_breakdown(charges: FillCharges) -> str:
    """
    Human-readable breakdown string for logging and audit records.

    Example:
        STT=₹12.50  Exch=₹0.74  SEBI=₹0.25  GST=₹0.18  Stamp=₹1.88
        Total=₹15.55  Net=₹5015.55
    """
    return (
        f"STT=₹{charges.stt}  "
        f"Exch=₹{charges.exchange_charges}  "
        f"SEBI=₹{charges.sebi_charges}  "
        f"GST=₹{charges.gst}  "
        f"Stamp=₹{charges.stamp_duty}  "
        f"Total=₹{charges.total_charges}  "
        f"Net=₹{charges.net_amount}"
    )
