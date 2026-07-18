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
from datetime import date, timedelta

import numpy as np
import pandas as pd
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


#: v2.0.0 feature set (WS2a — ML_FIX_IMPLEMENTATION_PLAN.md).
#:
#: Drops pe_ratio / pb_ratio / ev_ebitda: they exist only in CompanyKeyRatios,
#: a single upserted *current* snapshot with no period_date — they cannot be
#: made point-in-time from existing data. roe / roce are reconstructed
#: point-in-time from statements instead:
#:
#:   roe  = net_profit / net_worth
#:   roce = op_profit  / total_asset      (documented proxy)
#:
#: Verified against real data 2026-07-17 (Thu-AM identity check):
#:   - net_worth == total_asset - total_liability holds EXACTLY on all 9,503
#:     balance-sheet rows, so every "capital employed" variant constructible
#:     from the available columns reduces to total_asset — the proxy is the
#:     only choice, not an approximation of a better one.
#:   - Spearman rank agreement vs the Upstox key-ratio snapshots across 2,218
#:     companies: roe 0.73, roce 0.78. v2 rank-normalizes cross-sectionally,
#:     so only this ordering matters.
FUNDAMENTAL_FEATURE_NAMES_V2: list[str] = [
    # Profitability (reconstructed point-in-time)
    "roe",
    "roce",
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

#: A statement becomes *known to the market* at period_date + this lag.
#: SEBI LODR gives listed companies 45 days for quarterly results and 60 days
#: for annual audited results; 90 days adds conservative headroom so the
#: feature can never lead the actual disclosure. The legacy single-date
#: function applies no lag (mild lookahead) — a documented v1/v2 divergence.
FUNDAMENTAL_REPORTING_LAG_DAYS: int = 90


def get_fundamental_feature_names(version: str = "1.0.0") -> list[str]:
    """Feature names for the requested feature-set contract.

    The legacy default keeps every existing caller byte-identical; only
    version-aware v2 call sites pass "2.0.0" explicitly.
    """
    if version == "2.0.0":
        return list(FUNDAMENTAL_FEATURE_NAMES_V2)
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


# ── v2: point-in-time feature series (WS2a) ────────────────────────────────────

def _num(value) -> float:
    """Decimal/None → float/NaN for pandas frames."""
    return float(value) if value is not None else np.nan


def _yoy_series(s: pd.Series) -> pd.Series:
    """Vectorized ``_yoy_growth`` over consecutive periods: prior==0 → NaN."""
    prior = s.shift(1)
    out = (s - prior) / prior.abs()
    return out.replace([np.inf, -np.inf], np.nan)


def _expanding_cagr(s: pd.Series) -> pd.Series:
    """Row-wise ``_cagr`` from the first period to each row.

    Matches the legacy single-date semantics exactly: n_years = number of
    intervals between the first available period and the current one.
    """
    arr = s.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan)
    if len(arr) >= 2:
        first = arr[0]
        for i in range(1, len(arr)):
            val = _cagr(first, arr[i], i)
            out[i] = val if val is not None else np.nan
    return pd.Series(out, index=s.index)


def _expanding_slope(s: pd.Series) -> pd.Series:
    """Row-wise ``_linear_slope`` over the valid values seen so far.

    Matches the legacy semantics: invalid periods are skipped (not treated
    as NaN inputs to the regression), and fewer than 2 valid points → NaN.
    """
    out = np.full(len(s), np.nan)
    valid: list[float] = []
    for i, v in enumerate(s.to_numpy(dtype=float)):
        if not math.isnan(v):
            valid.append(v)
        if len(valid) >= 2:
            slope = _linear_slope(valid)
            out[i] = slope if slope is not None else np.nan
    return pd.Series(out, index=s.index)


def _expanding_mean(s: pd.Series) -> pd.Series:
    """Row-wise mean of the valid values seen so far (legacy margin-avg semantics)."""
    return s.expanding(min_periods=1).mean()


async def compute_fundamental_features_series(
    instrument_key: str,
    start_date: date,
    end_date: date,
    db: AsyncSession,
) -> pd.DataFrame:
    """
    Compute the 17 v2 fundamental features as a point-in-time series.

    Returns a DataFrame with ``effective_date`` (datetime64[ns], sorted asc)
    plus the FUNDAMENTAL_FEATURE_NAMES_V2 columns. Each row carries the
    feature values *as the market could have known them* on effective_date =
    period_date + FUNDAMENTAL_REPORTING_LAG_DAYS: every derived quantity
    (yoy, CAGR, expanding averages/trends) uses only statements published on
    or before that row's period. The caller maps daily rows onto this series
    with a backward ``merge_asof`` (see ``merge_fundamentals_asof``).

    Four bulk queries per symbol (income / balance / cash-flow / holdings —
    no key-ratios query; pe/pb/ev are not in v2) replace the legacy ~40
    per-date calls. No imputation happens here: NaN flows through to the
    cross-sectional rank pass, where missing → neutral 0.

    ``start_date`` documents the caller's window; rows are intentionally NOT
    trimmed to it — merge_asof(direction="backward") needs the last statement
    published *before* the window to value its opening rows.
    """
    empty = pd.DataFrame(columns=["effective_date", *FUNDAMENTAL_FEATURE_NAMES_V2])

    # ── 4 bulk queries (same filters as the legacy per-date function,
    #    minus the as-of cutoff) ──────────────────────────────────────────────
    is_rows = (await db.execute(
        select(CompanyIncomeStatement)
        .where(
            CompanyIncomeStatement.instrument_key == instrument_key,
            CompanyIncomeStatement.time_period    == "yearly",
            CompanyIncomeStatement.statement_type == "standalone",
            CompanyIncomeStatement.period_date    <= end_date,
        )
        .order_by(CompanyIncomeStatement.period_date.asc())
    )).scalars().all()

    bs_rows = (await db.execute(
        select(CompanyBalanceSheet)
        .where(
            CompanyBalanceSheet.instrument_key == instrument_key,
            CompanyBalanceSheet.statement_type == "standalone",
            CompanyBalanceSheet.period_date    <= end_date,
        )
        .order_by(CompanyBalanceSheet.period_date.asc())
    )).scalars().all()

    cf_rows = (await db.execute(
        select(CompanyCashFlow)
        .where(
            CompanyCashFlow.instrument_key == instrument_key,
            CompanyCashFlow.statement_type == "standalone",
            CompanyCashFlow.period_date    <= end_date,
        )
        .order_by(CompanyCashFlow.period_date.asc())
    )).scalars().all()

    sh_rows = (await db.execute(
        select(CompanyShareHoldings)
        .where(
            CompanyShareHoldings.instrument_key == instrument_key,
            CompanyShareHoldings.period_date    <= end_date,
        )
        .order_by(CompanyShareHoldings.period_date.asc())
    )).scalars().all()

    if not (is_rows or bs_rows or cf_rows or sh_rows):
        return empty

    # ── Per-source frames with vectorized per-period features ─────────────────
    frames: list[pd.DataFrame] = []

    if is_rows:
        inc = pd.DataFrame({
            "period_date": [r.period_date for r in is_rows],
            "_revenue":    [_num(r.revenue) for r in is_rows],
            "_op_profit":  [_num(r.op_profit) for r in is_rows],
            "_net_profit": [_num(r.net_profit) for r in is_rows],
        })
        inc["revenue_growth_yoy"] = _yoy_series(inc["_revenue"])
        inc["profit_growth_yoy"]  = _yoy_series(inc["_net_profit"])
        inc["revenue_cagr"]       = _expanding_cagr(inc["_revenue"])
        inc["profit_cagr"]        = _expanding_cagr(inc["_net_profit"])
        margin = inc["_op_profit"] / inc["_revenue"].replace(0.0, np.nan)
        inc["operating_margin"]     = margin.replace([np.inf, -np.inf], np.nan)
        inc["operating_margin_avg"] = _expanding_mean(inc["operating_margin"])
        frames.append(inc)

    if bs_rows:
        bal = pd.DataFrame({
            "period_date":      [r.period_date for r in bs_rows],
            "_net_worth":       [_num(r.net_worth) for r in bs_rows],
            "_total_asset":     [_num(r.total_asset) for r in bs_rows],
            "_total_liability": [_num(r.total_liability) for r in bs_rows],
        })
        nw = bal["_net_worth"]
        # log1p(|nw|) for nw >= 0, NaN otherwise — legacy semantics.
        bal["net_worth_log"]  = np.where(nw >= 0, np.log1p(nw.abs()), np.nan)
        bal["net_worth_cagr"] = _expanding_cagr(nw)
        debt_ratio = bal["_total_liability"] / bal["_total_asset"].replace(0.0, np.nan)
        bal["debt_ratio"]       = debt_ratio.replace([np.inf, -np.inf], np.nan)
        bal["debt_ratio_trend"] = _expanding_slope(bal["debt_ratio"])
        frames.append(bal)

    if cf_rows:
        cfl = pd.DataFrame({
            "period_date":    [r.period_date for r in cf_rows],
            "_operating_cf":  [_num(r.operating_cf) for r in cf_rows],
        })
        cfl["operating_cf_growth"] = _yoy_series(cfl["_operating_cf"])
        cfl["operating_cf_cagr"]   = _expanding_cagr(cfl["_operating_cf"])
        frames.append(cfl)

    if sh_rows:
        hold = pd.DataFrame({
            "period_date":          [r.period_date for r in sh_rows],
            "promoter_holding_pct": [_num(r.promoters_pct) for r in sh_rows],
            "fii_holding_pct":      [_num(r.fii_pct) for r in sh_rows],
        })
        hold["promoter_holding_change"] = hold["promoter_holding_pct"].diff()
        frames.append(hold)

    # ── Outer-merge on period_date, per-source ffill ───────────────────────────
    # Each source publishes on its own cadence; after the outer merge every
    # column is forward-filled independently, so a new balance sheet row
    # carries the latest known income figures (and vice versa) — exactly what
    # the market knows at that point.
    series = frames[0]
    for frame in frames[1:]:
        series = series.merge(frame, on="period_date", how="outer")
    series = series.sort_values("period_date").reset_index(drop=True)
    feature_cols = [c for c in series.columns if c != "period_date"]
    series[feature_cols] = series[feature_cols].ffill()

    # ── Reconstructed profitability ratios (post-merge, from ffilled inputs) ──
    # Non-positive denominators → NaN: roe against negative equity and roce
    # against a non-positive balance-sheet total are financially meaningless.
    # See the FUNDAMENTAL_FEATURE_NAMES_V2 docstring for the real-data
    # verification of both identities (2026-07-17).
    for col in ("_net_profit", "_op_profit", "_net_worth", "_total_asset"):
        if col not in series.columns:
            series[col] = np.nan
    nw = series["_net_worth"].where(series["_net_worth"] > 0)
    ta = series["_total_asset"].where(series["_total_asset"] > 0)
    series["roe"]  = (series["_net_profit"] / nw).replace([np.inf, -np.inf], np.nan)
    series["roce"] = (series["_op_profit"] / ta).replace([np.inf, -np.inf], np.nan)

    # ── Effective (market-knowledge) dates ─────────────────────────────────────
    series["effective_date"] = (
        pd.to_datetime(series["period_date"])
        + pd.Timedelta(days=FUNDAMENTAL_REPORTING_LAG_DAYS)
    )

    for col in FUNDAMENTAL_FEATURE_NAMES_V2:
        if col not in series.columns:
            series[col] = np.nan

    result = series[["effective_date", *FUNDAMENTAL_FEATURE_NAMES_V2]].copy()
    return result.sort_values("effective_date").reset_index(drop=True)


def merge_fundamentals_asof(
    features_df: pd.DataFrame,
    series_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Backward-asof merge of a v2 fundamental series onto a daily feature frame.

    ``features_df`` must carry a ``timestamp`` column (tz-aware or naive —
    OHLCV timestamps are tz-aware UTC; merge_asof cannot mix tz-ness, so a
    temporary naive normalized key is used). Rows before the first
    effective_date stay NaN — pre-history is genuinely unknown, and the
    cross-sectional rank pass maps NaN to neutral 0.
    """
    if series_df.empty:
        out = features_df.copy()
        for col in FUNDAMENTAL_FEATURE_NAMES_V2:
            out[col] = np.nan
        return out

    left = features_df.copy()
    key = pd.to_datetime(left["timestamp"])
    if key.dt.tz is not None:
        key = key.dt.tz_localize(None)
    left["_asof_key"] = key.dt.normalize()

    right = series_df.copy()
    right["effective_date"] = pd.to_datetime(right["effective_date"])

    merged = pd.merge_asof(
        left.sort_values("_asof_key", kind="stable"),
        right.sort_values("effective_date", kind="stable"),
        left_on="_asof_key",
        right_on="effective_date",
        direction="backward",
    )
    return merged.drop(columns=["_asof_key", "effective_date"])
