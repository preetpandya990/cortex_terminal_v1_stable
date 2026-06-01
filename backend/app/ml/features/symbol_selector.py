"""
Symbol Selector — NSE Instrument Eligibility for ML Training

Selects instruments that meet data-quality and history requirements for the
production walk-forward training pipeline.

Design principles
-----------------
* **Relative completeness** — a symbol is measured against its own listing
  history, not against the full training window.  A stock listed in 2020 with
  clean data since IPO is as useful as one listed in 2016; excluding it
  introduces survivorship bias (1.6–2.6% performance gap, per empirical quant
  literature).

* **Tiered history minimum** — two eligibility tracks:

  - *Established track* (≥ ``MIN_BARS_FOR_TRAINING`` = 730 bars ≈ 2 years):
    symbols with enough history to fill at least one full initial training fold
    in the Panel Purged CPCV.

  - *Short-history track* (``MIN_BARS_SHORT_HISTORY`` = 252–729 bars ≈ 1–<2 years):
    recently listed symbols that have cleared the ``MIN_IPO_QUARANTINE_DAYS``
    post-IPO stabilisation window (default 180 calendar days).  These symbols
    contribute only to the most recent CPCV folds — the Panel Purged CV handles
    unbalanced panels naturally by operating on a shared timestamp axis, so a
    symbol with fewer rows simply contributes fewer labelled samples.

    The 252-bar floor exceeds the academic minimum of 36–60 observations per
    symbol for stable panel coefficient estimation by 4× (López de Prado /
    Joint Estimation of Conditional Mean and Covariance for Unbalanced Panels,
    arxiv 2410.21858, 2024).  The 180-day quarantine sits at the conservative
    end of the 6–12 month industry standard, and is in practice non-binding
    for the 252-bar minimum: a stock needs ≈ 365 calendar days to accumulate
    252 trading-day bars at NSE's ratio, which already exceeds 180 days.

* **No volume floor at selection time** — volume thresholds are enforced by the
  feature pipeline's ATR dead-zone and winsorisation; the selector stays
  orthogonal to signal-level filtering.

Reference: walk-forward validation design from Advances in Financial Machine
Learning (López de Prado); survivorship-bias treatment per QuantConnect /
Two Sigma engineering blogs; unbalanced panel CV per arxiv 2410.21858.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd
from sqlalchemy import func, select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upstox_data import UpstoxOHLCV

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

# Established track: minimum bars to fill at least one full initial training
# fold in the Panel Purged CPCV (matches orchestrator initial_train_days=730).
MIN_BARS_FOR_TRAINING: int = 730   # ≈ 2 years of daily bars

# Short-history track: minimum bars for recently listed symbols.
# ≈ 1 year of NSE trading days.  Exceeds the academic 36–60 observation
# minimum for stable panel estimation by 4× (arxiv 2410.21858, 2024).
MIN_BARS_SHORT_HISTORY: int = 252

# Short-history track: post-IPO stabilisation window.
# Symbols whose first bar in the DB is more recent than this are excluded to
# avoid IPO-lock-up effects, anchor-investor exits, and price-discovery noise.
# In practice non-binding for MIN_BARS_SHORT_HISTORY=252 (a stock needs ≈ 365
# calendar days to accumulate 252 NSE trading-day bars, which already exceeds
# 180 days), but kept as an explicit, auditable safety gate.
MIN_IPO_QUARANTINE_DAYS: int = 180

# Minimum fraction of own-period trading days that must be present.
# 95 % relative completeness is the industry standard (Two Sigma / QuantConnect).
# Applied identically to both established and short-history tracks.
MIN_RELATIVE_COMPLETENESS_PCT: float = 95.0

# Exchange ratio of trading days to calendar days for NSE (252 / 365).
_TRADING_DAY_RATIO: float = 252 / 365   # ≈ 0.6904


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_top_liquid_symbols(
    db: AsyncSession,
    n: int = 50,
    timeframe: str = "1D",
    lookback_days: int = 3650,
    min_data_points: int = MIN_BARS_FOR_TRAINING,
) -> list[str]:
    """
    Return up to *n* symbols that have sufficient data for ML training.

    Selection strategy
    ------------------
    Single aggregation query (no per-symbol round-trips).  The hard gate is
    ``min_data_points`` bars within the lookback window.  Symbols are ordered
    by average daily volume so the most liquid instruments appear first in logs;
    the full qualifying set (up to *n*) is returned, not only the top by volume.

    Parameters
    ----------
    db:
        Async database session.
    n:
        Maximum number of symbols to return.
    timeframe:
        OHLCV timeframe label stored in the DB (``'1D'`` for daily).
    lookback_days:
        Calendar-day window to search for bars.  Anchored to the latest
        available bar, not to today, to handle ingestion lag gracefully.
    min_data_points:
        Minimum bar count within the lookback window.  Defaults to
        ``MIN_BARS_FOR_TRAINING`` (730) so every returned symbol can fill at
        least one initial training fold.

    Returns
    -------
    list[str]
        Instrument keys ordered by average volume descending.
    """
    logger.info(
        "Selecting top %d liquid symbols from timeframe '%s'  (min_bars=%d, lookback=%d days)",
        n, timeframe, min_data_points, lookback_days,
    )

    # Anchor to the latest available bar rather than datetime.now() so the
    # window is stable regardless of ingestion lag.
    result_max = await db.execute(
        select(func.max(UpstoxOHLCV.timestamp)).where(UpstoxOHLCV.timeframe == timeframe)
    )
    latest_date = result_max.scalar()

    if latest_date is None:
        logger.error("No OHLCV data found for timeframe '%s'", timeframe)
        return []

    end_date   = latest_date
    start_date = end_date - timedelta(days=lookback_days)

    stmt = (
        select(
            UpstoxOHLCV.instrument_key,
            func.avg(UpstoxOHLCV.volume).label("avg_volume"),
            func.count(UpstoxOHLCV.id).label("data_points"),
            func.min(UpstoxOHLCV.timestamp).label("first_bar"),
            func.max(UpstoxOHLCV.timestamp).label("last_bar"),
        )
        .where(
            and_(
                UpstoxOHLCV.timeframe == timeframe,
                UpstoxOHLCV.timestamp >= start_date,
                UpstoxOHLCV.timestamp <= end_date,
            )
        )
        .group_by(UpstoxOHLCV.instrument_key)
        .having(func.count(UpstoxOHLCV.id) >= min_data_points)
        .having(func.avg(UpstoxOHLCV.volume) > 0)
        .order_by(func.avg(UpstoxOHLCV.volume).desc())
    )

    result = await db.execute(stmt)
    rows = result.fetchall()

    if not rows:
        logger.error(
            "No symbols found for timeframe '%s' with ≥%d bars in the last %d days",
            timeframe, min_data_points, lookback_days,
        )
        return []

    # Relative completeness is informational at this stage; the hard gate above
    # already enforces the bar-count minimum.
    approx_trading_days_in_window = lookback_days * _TRADING_DAY_RATIO

    qualified: list[dict[str, Any]] = []
    for row in rows:
        own_period_days  = max((row.last_bar - row.first_bar).days, 1)
        expected_in_own  = own_period_days * _TRADING_DAY_RATIO
        relative_pct     = (row.data_points / max(expected_in_own, 1)) * 100
        window_pct       = (row.data_points / approx_trading_days_in_window) * 100

        qualified.append({
            "symbol":           row.instrument_key,
            "avg_volume":       float(row.avg_volume or 0),
            "data_points":      int(row.data_points),
            "relative_completeness_pct": float(relative_pct),
            "window_completeness_pct":   float(window_pct),
        })

    top_symbols = [s["symbol"] for s in qualified[:n]]

    logger.info(
        "Coarse filter: %d symbols qualify (≥%d bars, avg_volume>0)  →  returning top %d",
        len(qualified), min_data_points, len(top_symbols),
    )
    logger.info("Top 10 by volume:")
    for i, s in enumerate(qualified[:10], 1):
        logger.info(
            "  %2d. %-40s  bars=%d  vol=%s  rel_completeness=%.1f%%",
            i, s["symbol"], s["data_points"],
            f"{s['avg_volume']:>12,.0f}",
            s["relative_completeness_pct"],
        )

    return top_symbols


async def get_recently_listed_symbols(
    db: AsyncSession,
    n: int = 50,
    timeframe: str = "1D",
    lookback_days: int = 3650,
    min_bars: int = MIN_BARS_SHORT_HISTORY,
    max_bars: int = MIN_BARS_FOR_TRAINING - 1,
    ipo_quarantine_days: int = MIN_IPO_QUARANTINE_DAYS,
) -> list[str]:
    """
    Return up to *n* recently listed symbols eligible for the short-history
    training track.

    A symbol qualifies when ALL of the following hold:

    1. ``min_bars`` ≤ bar count within the lookback window < ``max_bars`` — sits
       in the short-history band (252–729 bars by default), strictly below the
       established track floor so the two passes never overlap.
    2. ``avg_volume > 0`` — at least some trading activity.
    3. ``first_bar ≤ today − ipo_quarantine_days`` — the oldest bar in the DB
       predates the quarantine cutoff, confirming the symbol has been live long
       enough to have cleared the IPO stabilisation window.

    Symbols are ordered by average volume descending so the most liquid recent
    listings appear first; callers can apply a per-tier cap by passing a smaller
    *n*.

    Parameters
    ----------
    db:
        Async database session.
    n:
        Maximum number of symbols to return.
    timeframe:
        OHLCV timeframe label stored in the DB (``'1D'`` for daily).
    lookback_days:
        Calendar-day window to search for bars.  Anchored to the latest
        available bar to handle ingestion lag gracefully.
    min_bars:
        Inclusive lower bound on bar count (default ``MIN_BARS_SHORT_HISTORY``).
    max_bars:
        Inclusive upper bound on bar count (default ``MIN_BARS_FOR_TRAINING − 1``).
        Keeping this strictly below the established track floor prevents
        duplication between the two selection passes.
    ipo_quarantine_days:
        Minimum calendar days since the symbol's first bar in the DB
        (default ``MIN_IPO_QUARANTINE_DAYS``).

    Returns
    -------
    list[str]
        Instrument keys ordered by average volume descending.
    """
    logger.info(
        "Short-history track: selecting recently listed symbols  "
        "(bars=[%d, %d]  ipo_quarantine=%d days  lookback=%d days)",
        min_bars, max_bars, ipo_quarantine_days, lookback_days,
    )

    result_max = await db.execute(
        select(func.max(UpstoxOHLCV.timestamp)).where(UpstoxOHLCV.timeframe == timeframe)
    )
    latest_date = result_max.scalar()
    if latest_date is None:
        logger.error("No OHLCV data found for timeframe '%s'", timeframe)
        return []

    end_date   = latest_date
    start_date = end_date - timedelta(days=lookback_days)

    # IPO quarantine cutoff: the first bar must be at least this old.
    quarantine_cutoff = end_date - timedelta(days=ipo_quarantine_days)

    stmt = (
        select(
            UpstoxOHLCV.instrument_key,
            func.avg(UpstoxOHLCV.volume).label("avg_volume"),
            func.count(UpstoxOHLCV.id).label("data_points"),
            func.min(UpstoxOHLCV.timestamp).label("first_bar"),
            func.max(UpstoxOHLCV.timestamp).label("last_bar"),
        )
        .where(
            and_(
                UpstoxOHLCV.timeframe  == timeframe,
                UpstoxOHLCV.timestamp  >= start_date,
                UpstoxOHLCV.timestamp  <= end_date,
            )
        )
        .group_by(UpstoxOHLCV.instrument_key)
        .having(func.count(UpstoxOHLCV.id) >= min_bars)
        .having(func.count(UpstoxOHLCV.id) <= max_bars)
        .having(func.avg(UpstoxOHLCV.volume) > 0)
        .having(func.min(UpstoxOHLCV.timestamp) <= quarantine_cutoff)
        .order_by(func.avg(UpstoxOHLCV.volume).desc())
    )

    result = await db.execute(stmt)
    rows = result.fetchall()

    if not rows:
        logger.info(
            "Short-history track: no symbols found  "
            "(bars=[%d, %d]  ipo_quarantine=%d days)",
            min_bars, max_bars, ipo_quarantine_days,
        )
        return []

    candidates: list[dict[str, Any]] = []
    for row in rows:
        own_period_days = max((row.last_bar - row.first_bar).days, 1)
        expected_in_own = own_period_days * _TRADING_DAY_RATIO
        relative_pct    = (row.data_points / max(expected_in_own, 1)) * 100
        days_since_ipo  = (end_date - row.first_bar).days

        candidates.append({
            "symbol":                    row.instrument_key,
            "avg_volume":                float(row.avg_volume or 0),
            "data_points":               int(row.data_points),
            "relative_completeness_pct": float(relative_pct),
            "days_since_first_bar":      days_since_ipo,
        })

    top_symbols = [s["symbol"] for s in candidates[:n]]

    logger.info(
        "Short-history track: %d candidates (bars=[%d, %d]  ipo_quarantine=%d days)  "
        "→ returning top %d",
        len(candidates), min_bars, max_bars, ipo_quarantine_days, len(top_symbols),
    )
    logger.info("Short-history top 10 by volume:")
    for i, s in enumerate(candidates[:10], 1):
        logger.info(
            "  %2d. %-40s  bars=%d  days_listed=%d  vol=%s  rel_completeness=%.1f%%",
            i, s["symbol"], s["data_points"], s["days_since_first_bar"],
            f"{s['avg_volume']:>12,.0f}",
            s["relative_completeness_pct"],
        )

    return top_symbols


async def analyze_symbol_data_quality(
    symbol: str,
    timeframe: str,
    lookback_days: int,
    db: AsyncSession,
    min_bars_override: int | None = None,
) -> dict[str, Any]:
    """
    Compute detailed data-quality metrics for a single symbol.

    Completeness is measured **relative to the symbol's own listing period**,
    not against the full training window.  This eliminates the survivorship bias
    introduced by penalising recently-listed instruments.

    A symbol is considered qualified when ALL of the following hold:

    1. At least ``min_bars_override`` (or ``MIN_BARS_FOR_TRAINING`` = 730 when
       not overridden) bars exist — the hard history gate.  Pass
       ``min_bars_override=MIN_BARS_SHORT_HISTORY`` (252) when auditing
       short-history track candidates so the gate reflects their lower floor.
    2. Relative completeness ≥ ``MIN_RELATIVE_COMPLETENESS_PCT`` (95 %) —
       i.e., ≥ 95 % of the expected trading days within its own listing period
       are present.  Applied identically to both tracks.
    3. No zero-price bars (corrupt data).
    4. No invalid OHLC bars (high < low, close outside [low, high]).
    5. Fewer than 5 % of bars have zero volume.

    Parameters
    ----------
    symbol:
        NSE instrument key.
    timeframe:
        OHLCV timeframe label (e.g. ``'1D'``).
    lookback_days:
        Calendar-day window for the data query.  Data before the symbol's
        first bar is simply absent; the function handles this correctly.
    db:
        Async database session.
    min_bars_override:
        When provided, replaces ``MIN_BARS_FOR_TRAINING`` in the
        ``insufficient_history`` gate and in the ``min_bars_required`` field
        of the returned report.  Use ``MIN_BARS_SHORT_HISTORY`` (252) for
        short-history track audits.

    Returns
    -------
    dict
        Quality report with ``completeness_pct`` (relative), ``is_qualified``,
        ``disqualification_reason`` (first failing gate or ``None``), and
        supporting statistics.
    """
    end_date   = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=lookback_days)

    stmt = (
        select(
            UpstoxOHLCV.timestamp,
            UpstoxOHLCV.open,
            UpstoxOHLCV.high,
            UpstoxOHLCV.low,
            UpstoxOHLCV.close,
            UpstoxOHLCV.volume,
        )
        .where(
            and_(
                UpstoxOHLCV.instrument_key == symbol,
                UpstoxOHLCV.timeframe      == timeframe,
                UpstoxOHLCV.timestamp      >= start_date,
                UpstoxOHLCV.timestamp      <= end_date,
            )
        )
        .order_by(UpstoxOHLCV.timestamp)
    )

    result = await db.execute(stmt)
    rows = result.fetchall()

    if not rows:
        return _no_data_report(symbol)

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    actual_bars  = len(df)
    first_ts     = df["timestamp"].iloc[0]
    last_ts      = df["timestamp"].iloc[-1]
    own_period_calendar_days = max((last_ts - first_ts).days, 1)

    # ── Relative completeness ────────────────────────────────────────────────
    # Expected trading days WITHIN the symbol's own period, not the full window.
    expected_in_own_period = own_period_calendar_days * _TRADING_DAY_RATIO
    relative_completeness  = (actual_bars / max(expected_in_own_period, 1)) * 100

    # ── Gap analysis ─────────────────────────────────────────────────────────
    date_diffs        = df["timestamp"].diff().dt.days.dropna()
    max_gap_days      = int(date_diffs.max()) if len(date_diffs) else 0
    gaps_over_5_days  = int((date_diffs > 5).sum())

    # ── Volume checks ────────────────────────────────────────────────────────
    avg_volume        = float(df["volume"].mean())
    zero_volume_days  = int((df["volume"] == 0).sum())
    zero_volume_pct   = zero_volume_days / actual_bars * 100

    # ── Price-integrity checks ───────────────────────────────────────────────
    has_zero_prices   = bool((df["close"] <= 0).any())
    has_invalid_ohlc  = bool(
        ((df["high"] < df["low"])
         | (df["close"] > df["high"])
         | (df["close"] < df["low"])).any()
    )

    # ── Qualification gates (in priority order) ──────────────────────────────
    min_bars_required = min_bars_override if min_bars_override is not None else MIN_BARS_FOR_TRAINING
    disqualification_reason: str | None = None

    if actual_bars < min_bars_required:
        disqualification_reason = (
            f"insufficient_history: {actual_bars} bars < {min_bars_required} minimum"
        )
    elif relative_completeness < MIN_RELATIVE_COMPLETENESS_PCT:
        disqualification_reason = (
            f"low_relative_completeness: {relative_completeness:.1f}% "
            f"< {MIN_RELATIVE_COMPLETENESS_PCT}% (own-period gate)"
        )
    elif has_zero_prices:
        disqualification_reason = "data_integrity: zero or negative close price detected"
    elif has_invalid_ohlc:
        disqualification_reason = "data_integrity: invalid OHLC bar(s) detected (high < low or close out of range)"
    elif zero_volume_pct >= 5.0:
        disqualification_reason = (
            f"low_liquidity: {zero_volume_pct:.1f}% zero-volume bars ≥ 5% threshold"
        )

    is_qualified = disqualification_reason is None

    return {
        "symbol":                symbol,
        "has_data":              True,
        "is_qualified":          is_qualified,
        "disqualification_reason": disqualification_reason,
        # Bar counts
        "data_points":           actual_bars,
        "min_bars_required":     min_bars_required,
        # Relative completeness (primary quality metric)
        "completeness_pct":      float(relative_completeness),   # relative — used by orchestrator
        "completeness_method":   "relative_to_own_listing_period",
        # Date coverage
        "date_range": {
            "start": first_ts.isoformat(),
            "end":   last_ts.isoformat(),
            "own_period_calendar_days": own_period_calendar_days,
            "expected_trading_days":    int(expected_in_own_period),
        },
        # Gap statistics
        "gaps": {
            "max_gap_days":     max_gap_days,
            "gaps_over_5_days": gaps_over_5_days,
        },
        # Volume
        "volume": {
            "avg":              float(avg_volume),
            "zero_volume_days": zero_volume_days,
            "zero_volume_pct":  float(zero_volume_pct),
        },
        # Price integrity
        "quality_issues": {
            "has_zero_prices":  has_zero_prices,
            "has_invalid_ohlc": has_invalid_ohlc,
        },
    }


async def batch_analyze_symbols(
    symbols: list[str],
    timeframe: str,
    lookback_days: int,
    db: AsyncSession,
    min_bars_override: int | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Run ``analyze_symbol_data_quality`` for each symbol in *symbols*.

    Returns a mapping of ``instrument_key → quality_report``.
    Errors on individual symbols are caught and reported in the report dict
    rather than aborting the entire batch.

    Parameters
    ----------
    min_bars_override:
        Forwarded verbatim to ``analyze_symbol_data_quality``.  Pass
        ``MIN_BARS_SHORT_HISTORY`` when auditing the short-history track.
    """
    logger.info(
        "Batch quality analysis: %d symbols  timeframe=%s  min_bars=%s",
        len(symbols), timeframe,
        min_bars_override if min_bars_override is not None else MIN_BARS_FOR_TRAINING,
    )

    results: dict[str, dict[str, Any]] = {}
    qualified_count = 0

    for i, symbol in enumerate(symbols, 1):
        try:
            report = await analyze_symbol_data_quality(
                symbol, timeframe, lookback_days, db,
                min_bars_override=min_bars_override,
            )
            results[symbol] = report
            if report.get("is_qualified"):
                qualified_count += 1
        except Exception as exc:
            logger.error("Error analyzing %s: %s", symbol, exc, exc_info=True)
            results[symbol] = {
                "symbol":   symbol,
                "has_data": False,
                "error":    str(exc),
            }

        if i % 200 == 0:
            logger.info("  Progress: %d/%d  (qualified so far: %d)", i, len(symbols), qualified_count)

    logger.info(
        "Batch analysis complete: %d/%d qualified",
        qualified_count, len(symbols),
    )
    return results


def filter_symbols_by_criteria(
    quality_results: dict[str, dict[str, Any]],
    min_completeness: float = MIN_RELATIVE_COMPLETENESS_PCT,
    min_bars: int = MIN_BARS_FOR_TRAINING,
    max_zero_volume_pct: float = 5.0,
) -> list[str]:
    """
    Filter the output of ``batch_analyze_symbols`` by explicit thresholds.

    Uses the same relative-completeness semantics as ``analyze_symbol_data_quality``.
    The ``is_qualified`` flag on each report already combines all gates; this
    function provides a way to override thresholds without re-running analysis.

    Parameters
    ----------
    quality_results:
        Mapping returned by ``batch_analyze_symbols``.
    min_completeness:
        Minimum relative completeness percentage.
    min_bars:
        Minimum bar count (hard history gate).
    max_zero_volume_pct:
        Maximum fraction of zero-volume bars.

    Returns
    -------
    list[str]
        Instrument keys passing all criteria.
    """
    qualified: list[str] = []

    for symbol, q in quality_results.items():
        if not q.get("has_data"):
            continue
        if q.get("data_points", 0) < min_bars:
            continue
        if q.get("completeness_pct", 0.0) < min_completeness:
            continue
        if q.get("volume", {}).get("zero_volume_pct", 100.0) >= max_zero_volume_pct:
            continue
        if q.get("quality_issues", {}).get("has_zero_prices"):
            continue
        if q.get("quality_issues", {}).get("has_invalid_ohlc"):
            continue
        qualified.append(symbol)

    logger.info(
        "filter_symbols_by_criteria: %d/%d passed  "
        "(min_completeness=%.0f%%  min_bars=%d  max_zero_vol=%.0f%%)",
        len(qualified), len(quality_results),
        min_completeness, min_bars, max_zero_volume_pct,
    )
    return qualified


# ── Internal helpers ───────────────────────────────────────────────────────────

def _no_data_report(symbol: str) -> dict[str, Any]:
    """Return a consistent empty report for symbols with no OHLCV data."""
    return {
        "symbol":                  symbol,
        "has_data":                False,
        "is_qualified":            False,
        "disqualification_reason": "no_data: no OHLCV rows found in query window",
        "data_points":             0,
        "completeness_pct":        0.0,
    }
