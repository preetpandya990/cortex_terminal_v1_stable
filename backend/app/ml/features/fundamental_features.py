"""
Fundamental Feature Engineering
================================
Derives 20 cross-sectional ML features from the company_fundamentals_* DB tables.

All queries are point-in-time correct: only rows where period_date <= as_of_date
are considered, preventing lookahead bias in walk-forward training.

Cross-sectional semantics:
  These features are time-invariant within a training window — they represent
  company-level characteristics, not day-level signals. The feature pipeline
  broadcasts a single value to all time steps for a given instrument in the
  60-step sequence window.

History depth:
  The Upstox API returns up to ~4 years of history. Features that use multiple
  periods (CAGR, trend, YoY growth) require at least 2 data points and return
  NaN when fewer exist (e.g., newly listed companies). NaN values are handled
  by median imputation in the feature pipeline — never by filling with 0.

Feature list (20 total):
  Valuation     : pe_ratio, pb_ratio, roe, roce, ev_ebitda
  Revenue       : revenue_growth_yoy, revenue_cagr
  Profit        : profit_growth_yoy, profit_cagr
  Margins       : operating_margin, operating_margin_avg
  Balance Sheet : net_worth_log, net_worth_cagr, debt_ratio, debt_ratio_trend
  Cash Flow     : operating_cf_growth, operating_cf_cagr
  Holdings      : promoter_holding_pct, fii_holding_pct, promoter_holding_change
"""
from __future__ import annotations

import logging
import math
from datetime import date

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fundamentals import (
    CompanyBalanceSheet,
    CompanyCashFlow,
    CompanyIncomeStatement,
    CompanyKeyRatios,
    CompanyShareHoldings,
)

logger = logging.getLogger(__name__)

FUNDAMENTAL_FEATURE_NAMES: list[str] = [
    # Valuation
    "pe_ratio",
    "pb_ratio",
    "roe",
    "roce",
    "ev_ebitda",
    # Revenue growth
    "revenue_growth_yoy",
    "revenue_cagr",
    # Profit growth
    "profit_growth_yoy",
    "profit_cagr",
    # Margins
    "operating_margin",
    "operating_margin_avg",
    # Balance sheet
    "net_worth_log",
    "net_worth_cagr",
    "debt_ratio",
    "debt_ratio_trend",
    # Cash flow
    "operating_cf_growth",
    "operating_cf_cagr",
    # Shareholding
    "promoter_holding_pct",
    "fii_holding_pct",
    "promoter_holding_change",
]


def get_fundamental_feature_names() -> list[str]:
    return list(FUNDAMENTAL_FEATURE_NAMES)


# ── Pure computation helpers ───────────────────────────────────────────────────

def _cagr(first: float, last: float, n_years: float) -> float | None:
    """Compound annual growth rate over n_years periods. Returns None on invalid inputs."""
    if n_years <= 0 or first is None or last is None:
        return None
    try:
        f, l = float(first), float(last)
        if f == 0 or math.isnan(f) or math.isnan(l):
            return None
        if (l / f) < 0:
            return None
        return (l / f) ** (1.0 / n_years) - 1.0
    except (ZeroDivisionError, ValueError, OverflowError):
        return None


def _yoy_growth(current: float | None, prior: float | None) -> float | None:
    """Year-over-year growth rate: (current - prior) / abs(prior)."""
    if current is None or prior is None:
        return None
    try:
        c, p = float(current), float(prior)
        if p == 0 or math.isnan(c) or math.isnan(p):
            return None
        return (c - p) / abs(p)
    except (ZeroDivisionError, ValueError):
        return None


def _linear_slope(values: list[float]) -> float | None:
    """Slope of an OLS regression through the values (equal time spacing)."""
    n = len(values)
    if n < 2:
        return None
    try:
        x = np.arange(n, dtype=float)
        y = np.array(values, dtype=float)
        if np.any(np.isnan(y)):
            return None
        slope = float(np.polyfit(x, y, 1)[0])
        return slope
    except (np.linalg.LinAlgError, ValueError):
        return None


# ── Feature computation ────────────────────────────────────────────────────────

async def compute_fundamental_features(
    instrument_key: str,
    as_of_date: date,
    db: AsyncSession,
) -> dict[str, float | None]:
    """
    Compute all 20 fundamental features for one instrument as of as_of_date.

    Returns a dict with exactly FUNDAMENTAL_FEATURE_NAMES as keys.
    Missing data yields None; the feature pipeline applies median imputation.
    """
    feats: dict[str, float | None] = {k: None for k in FUNDAMENTAL_FEATURE_NAMES}

    # ── Key ratios ─────────────────────────────────────────────────────────────
    kr_result = await db.execute(
        select(CompanyKeyRatios)
        .where(CompanyKeyRatios.instrument_key == instrument_key)
    )
    kr = kr_result.scalar_one_or_none()
    if kr:
        feats["pe_ratio"] = float(kr.pe)    if kr.pe    is not None else None
        feats["pb_ratio"] = float(kr.pb)    if kr.pb    is not None else None
        feats["roe"]      = float(kr.roe)   if kr.roe   is not None else None
        feats["roce"]     = float(kr.roce)  if kr.roce  is not None else None
        feats["ev_ebitda"] = float(kr.ev_ebitda) if kr.ev_ebitda is not None else None

    # ── Income statement ───────────────────────────────────────────────────────
    is_result = await db.execute(
        select(CompanyIncomeStatement)
        .where(
            CompanyIncomeStatement.instrument_key == instrument_key,
            CompanyIncomeStatement.time_period    == "yearly",
            CompanyIncomeStatement.statement_type == "standalone",
            CompanyIncomeStatement.period_date    <= as_of_date,
        )
        .order_by(CompanyIncomeStatement.period_date.asc())
    )
    is_rows = is_result.scalars().all()

    if len(is_rows) >= 1:
        latest = is_rows[-1]

        # Operating margin from most recent period
        if latest.revenue and float(latest.revenue) != 0 and latest.op_profit is not None:
            feats["operating_margin"] = float(latest.op_profit) / float(latest.revenue)

        # Operating margin average across all available periods
        margins = []
        for r in is_rows:
            if r.revenue and float(r.revenue) != 0 and r.op_profit is not None:
                margins.append(float(r.op_profit) / float(r.revenue))
        feats["operating_margin_avg"] = float(np.mean(margins)) if margins else None

    if len(is_rows) >= 2:
        prior  = is_rows[-2]
        latest = is_rows[-1]

        feats["revenue_growth_yoy"] = _yoy_growth(latest.revenue, prior.revenue)
        feats["profit_growth_yoy"]  = _yoy_growth(latest.net_profit, prior.net_profit)

        n_years = len(is_rows) - 1
        feats["revenue_cagr"] = _cagr(
            is_rows[0].revenue, is_rows[-1].revenue, n_years
        )
        feats["profit_cagr"] = _cagr(
            is_rows[0].net_profit, is_rows[-1].net_profit, n_years
        )

    # ── Balance sheet ──────────────────────────────────────────────────────────
    bs_result = await db.execute(
        select(CompanyBalanceSheet)
        .where(
            CompanyBalanceSheet.instrument_key == instrument_key,
            CompanyBalanceSheet.statement_type == "standalone",
            CompanyBalanceSheet.period_date    <= as_of_date,
        )
        .order_by(CompanyBalanceSheet.period_date.asc())
    )
    bs_rows = bs_result.scalars().all()

    if len(bs_rows) >= 1:
        latest_bs = bs_rows[-1]

        if latest_bs.net_worth is not None:
            nw = float(latest_bs.net_worth)
            feats["net_worth_log"] = math.log1p(abs(nw)) if nw >= 0 else None

        if (latest_bs.total_asset and float(latest_bs.total_asset) != 0
                and latest_bs.total_liability is not None):
            feats["debt_ratio"] = float(latest_bs.total_liability) / float(latest_bs.total_asset)

    if len(bs_rows) >= 2:
        n_years   = len(bs_rows) - 1
        feats["net_worth_cagr"] = _cagr(
            bs_rows[0].net_worth, bs_rows[-1].net_worth, n_years
        )

        debt_ratios = []
        for r in bs_rows:
            if r.total_asset and float(r.total_asset) != 0 and r.total_liability is not None:
                debt_ratios.append(float(r.total_liability) / float(r.total_asset))
        feats["debt_ratio_trend"] = _linear_slope(debt_ratios)

    # ── Cash flow ──────────────────────────────────────────────────────────────
    cf_result = await db.execute(
        select(CompanyCashFlow)
        .where(
            CompanyCashFlow.instrument_key == instrument_key,
            CompanyCashFlow.statement_type == "standalone",
            CompanyCashFlow.period_date    <= as_of_date,
        )
        .order_by(CompanyCashFlow.period_date.asc())
    )
    cf_rows = cf_result.scalars().all()

    if len(cf_rows) >= 2:
        feats["operating_cf_growth"] = _yoy_growth(
            cf_rows[-1].operating_cf, cf_rows[-2].operating_cf
        )
        n_years = len(cf_rows) - 1
        feats["operating_cf_cagr"] = _cagr(
            cf_rows[0].operating_cf, cf_rows[-1].operating_cf, n_years
        )

    # ── Share holdings ─────────────────────────────────────────────────────────
    sh_result = await db.execute(
        select(CompanyShareHoldings)
        .where(
            CompanyShareHoldings.instrument_key == instrument_key,
            CompanyShareHoldings.period_date    <= as_of_date,
        )
        .order_by(CompanyShareHoldings.period_date.asc())
    )
    sh_rows = sh_result.scalars().all()

    if len(sh_rows) >= 1:
        latest_sh = sh_rows[-1]
        feats["promoter_holding_pct"] = (
            float(latest_sh.promoters_pct) if latest_sh.promoters_pct is not None else None
        )
        feats["fii_holding_pct"] = (
            float(latest_sh.fii_pct) if latest_sh.fii_pct is not None else None
        )

    if len(sh_rows) >= 2:
        prev_sh = sh_rows[-2]
        curr_sh = sh_rows[-1]
        if curr_sh.promoters_pct is not None and prev_sh.promoters_pct is not None:
            feats["promoter_holding_change"] = (
                float(curr_sh.promoters_pct) - float(prev_sh.promoters_pct)
            )

    return feats
