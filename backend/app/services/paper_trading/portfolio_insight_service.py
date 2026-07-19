"""
Paper Trading — Portfolio Insight Stats Service (B4)
=====================================================
Computes the portfolio-level risk panel for the read-only Portfolio Insight &
Advise layer: capital-at-risk, single-name & sector concentration, return
correlation, and a deterministic stress scan.  All figures are derived from the
paper book, company fundamentals, and stored OHLCV — no ML, no external calls.

Design
------
The heavy lifting lives in **pure functions** that take already-loaded data
(position views, a returns matrix, sector map); a thin async orchestrator does
the I/O (positions, fundamentals, OHLCV) and stitches the result.  This keeps
every statistic independently unit-testable and the query surface minimal.

Honest gaps, never guesses
--------------------------
  • Capital-at-risk excludes positions with no stop and reports their count.
  • Correlation excludes names with < ``_CORR_MIN_OBS`` overlapping daily
    returns and reports how many were dropped.
  • Sectors that cannot be resolved bucket as "Unclassified".
  • The vol stress excludes names without enough history to estimate σ.
Each gap surfaces in the typed fields and the top-level ``notes`` list.

Stress scenarios (deterministic, side-aware — shorts gain when the market falls)
--------------------------------------------------------------------------------
  index_down  : every position repriced −5% adverse-to-market
                Δ = −0.05 × Σ signed_exposure       (net directional exposure)
  sector_down : the most-concentrated *classified* sector repriced −10%
                Δ = −0.10 × Σ signed_exposure of that sector
  vol_double  : each position moves adversely by 2× its own daily σ (a 2-σ bad
                day), σ from the same daily log returns
                Δ = −Σ |2 × σ_daily × market_value|

Weights & concentration bases
-----------------------------
  • ``max_weight_pct`` / ``top_holdings`` — position market value ÷ **portfolio
    value** (cash included): "how much of my whole book is this name".
  • ``hhi`` / ``effective_positions`` — computed on **invested weights**
    (normalised across holdings, Σ = 1): the textbook, cash-independent
    concentration measure, so effective_positions = "behaves like N equal names".
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import numpy as np
import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.portfolio_insight import (
    CapitalAtRiskStat,
    CorrelationStat,
    HoldingWeight,
    PortfolioInsightStats,
    SectorConcentration,
    SectorWeight,
    SingleNameConcentration,
    StressScan,
    StressScenario,
)
from app.services import sector_map

logger = logging.getLogger(__name__)

# ── Tuning constants ────────────────────────────────────────────────────────────
_DAILY_TIMEFRAME = "1D"               # canonical daily-candle timeframe in upstox_ohlcv
_CORR_WINDOW_DAYS = 90                # trailing daily-return window used for correlation/σ
_CORR_LOOKBACK_CALENDAR_DAYS = 200    # calendar fetch window → ~90+ trading days
_CORR_MIN_OBS = 30                    # min overlapping returns to include a name
_TOP_HOLDINGS = 5
_UNCLASSIFIED = "Unclassified"

_INDEX_SHOCK = 0.05                   # −5% broad-market move
_SECTOR_SHOCK = 0.10                  # −10% single-sector move
_VOL_SHOCK_MULT = 2.0                 # 2× daily σ adverse move


@dataclass(slots=True)
class _PositionView:
    """Lightweight, side-aware view of one open position for the stat helpers."""
    symbol: str
    instrument_key: str
    side: str                # "LONG" | "SHORT"
    quantity: int
    avg_cost: float
    stop_loss: float | None
    last_price: float
    market_value: float      # qty × last_price (gross, ≥ 0)
    signed_exposure: float   # +market_value for LONG, −market_value for SHORT


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

async def compute_portfolio_insight_stats(
    session: AsyncSession,
    portfolio,
) -> PortfolioInsightStats:
    """
    Build the full portfolio insight panel for ``portfolio`` (an active Portfolio
    ORM row).  Returns a zeroed panel when there are no open positions.
    """
    now = datetime.now(timezone.utc)
    views = await _load_position_views(session, portfolio.id)
    if not views:
        return _empty_stats(portfolio.id, float(portfolio.current_cash), now)

    market_total = math.fsum(v.market_value for v in views)
    portfolio_value = float(portfolio.current_cash) + market_total

    sectors = await _resolve_sectors(session, views)
    returns = await _load_returns_matrix(session, [v.instrument_key for v in views])
    sigma_by_key = _daily_sigma(returns)

    car = _capital_at_risk(views, portfolio_value)
    single = _single_name_concentration(views, portfolio_value)
    sector_stat = _sector_concentration(views, sectors, portfolio_value)
    correlation = _correlation_stat(views, returns)
    stress = _stress_scan(views, sectors, sigma_by_key, portfolio_value)

    notes = _build_notes(car, correlation, sector_stat, len(views))

    return PortfolioInsightStats(
        portfolio_id=portfolio.id,
        portfolio_value=round(portfolio_value, 2),
        open_position_count=len(views),
        capital_at_risk=car,
        single_name=single,
        sector=sector_stat,
        correlation=correlation,
        stress=stress,
        notes=notes,
        computed_at=now,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pure statistic helpers
# ──────────────────────────────────────────────────────────────────────────────

def _capital_at_risk(views: list[_PositionView], portfolio_value: float) -> CapitalAtRiskStat:
    """Σ |avg_cost − stop| × qty over stopped positions; unstopped names excluded + counted."""
    car = 0.0
    with_stop = 0
    without_stop = 0
    for v in views:
        if v.stop_loss is not None:
            car += abs(v.avg_cost - v.stop_loss) * v.quantity
            with_stop += 1
        else:
            without_stop += 1
    pct = (car / portfolio_value * 100.0) if portfolio_value > 0 else 0.0
    return CapitalAtRiskStat(
        capital_at_risk=round(car, 2),
        capital_at_risk_pct=round(pct, 4),
        positions_with_stop=with_stop,
        positions_without_stop=without_stop,
    )


def _single_name_concentration(
    views: list[_PositionView], portfolio_value: float
) -> SingleNameConcentration:
    """Max name weight (% of PV) + HHI / effective positions (on invested weights)."""
    market_total = math.fsum(v.market_value for v in views)

    # PV weights (cash included) for the displayed name weights.
    pv_weights = [
        (v.symbol, (v.market_value / portfolio_value) if portfolio_value > 0 else 0.0)
        for v in views
    ]
    pv_weights.sort(key=lambda t: t[1], reverse=True)
    top = [HoldingWeight(symbol=s, weight_pct=round(w * 100.0, 4)) for s, w in pv_weights[:_TOP_HOLDINGS]]
    max_symbol, max_w = pv_weights[0] if pv_weights else (None, 0.0)

    # Invested weights (Σ = 1) for the cash-independent HHI / effective count.
    if market_total > 0:
        inv = [v.market_value / market_total for v in views]
        hhi = math.fsum(w * w for w in inv)
    else:
        hhi = 0.0
    effective = (1.0 / hhi) if hhi > 0 else 0.0

    return SingleNameConcentration(
        max_weight_pct=round(max_w * 100.0, 4),
        max_weight_symbol=max_symbol,
        hhi=round(hhi, 6),
        effective_positions=round(effective, 2),
        top_holdings=top,
    )


def _sector_concentration(
    views: list[_PositionView],
    sectors: dict[str, str],
    portfolio_value: float,
) -> SectorConcentration:
    """Group market-value weight by resolved sector; 'Unclassified' where unknown."""
    by_sector: dict[str, float] = {}
    for v in views:
        sec = sectors.get(v.instrument_key, _UNCLASSIFIED)
        by_sector[sec] = by_sector.get(sec, 0.0) + v.market_value

    def pct(mv: float) -> float:
        return (mv / portfolio_value * 100.0) if portfolio_value > 0 else 0.0

    breakdown = sorted(
        (SectorWeight(sector=s, weight_pct=round(pct(mv), 4)) for s, mv in by_sector.items()),
        key=lambda sw: sw.weight_pct,
        reverse=True,
    )
    # Most-concentrated *classified* sector (Unclassified is a gap, not a sector).
    classified = [sw for sw in breakdown if sw.sector != _UNCLASSIFIED]
    top = classified[0] if classified else None
    unclassified_pct = pct(by_sector.get(_UNCLASSIFIED, 0.0))

    return SectorConcentration(
        max_sector=top.sector if top else None,
        max_sector_weight_pct=top.weight_pct if top else 0.0,
        unclassified_weight_pct=round(unclassified_pct, 4),
        breakdown=breakdown,
    )


def _correlation_stat(
    views: list[_PositionView], returns: pd.DataFrame | None
) -> CorrelationStat:
    """Max & average pairwise correlation of daily log returns across positions."""
    total = len(views)
    key_to_symbol = {v.instrument_key: v.symbol for v in views}

    if returns is None or returns.shape[1] < 2:
        return CorrelationStat(
            covered_positions=0 if returns is None else int((returns.count() >= _CORR_MIN_OBS).sum()),
            excluded_positions=total if returns is None else total - int((returns.count() >= _CORR_MIN_OBS).sum()),
            window_days=_CORR_WINDOW_DAYS,
        )

    obs = returns.count()
    covered = [k for k in returns.columns if obs[k] >= _CORR_MIN_OBS]
    excluded = total - len(covered)

    if len(covered) < 2:
        return CorrelationStat(
            covered_positions=len(covered),
            excluded_positions=excluded,
            window_days=_CORR_WINDOW_DAYS,
        )

    corr = returns[covered].corr(min_periods=_CORR_MIN_OBS)
    keys = list(corr.columns)
    pair_values: list[float] = []
    best: tuple[float, str, str] | None = None
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            rho = corr.iat[i, j]
            if rho is None or (isinstance(rho, float) and math.isnan(rho)):
                continue
            rho = float(rho)
            pair_values.append(rho)
            if best is None or rho > best[0]:
                best = (rho, keys[i], keys[j])

    if not pair_values or best is None:
        return CorrelationStat(
            covered_positions=len(covered),
            excluded_positions=excluded,
            window_days=_CORR_WINDOW_DAYS,
        )

    return CorrelationStat(
        max_pair_correlation=round(best[0], 4),
        max_pair=[key_to_symbol.get(best[1], best[1]), key_to_symbol.get(best[2], best[2])],
        avg_pairwise_correlation=round(float(np.mean(pair_values)), 4),
        covered_positions=len(covered),
        excluded_positions=excluded,
        window_days=_CORR_WINDOW_DAYS,
    )


def _stress_scan(
    views: list[_PositionView],
    sectors: dict[str, str],
    sigma_by_key: dict[str, float],
    portfolio_value: float,
) -> StressScan:
    """Deterministic, side-aware repricing scenarios (see module docstring)."""
    scenarios: list[StressScenario] = []

    def as_pct(delta_value: float) -> float:
        return round((delta_value / portfolio_value * 100.0) if portfolio_value > 0 else 0.0, 4)

    # ── Broad market −5% ──────────────────────────────────────────────────────
    net_exposure = math.fsum(v.signed_exposure for v in views)
    scenarios.append(StressScenario(
        key="index_down",
        label="Broad market −5%",
        delta_pct=as_pct(-_INDEX_SHOCK * net_exposure),
        detail="Every position repriced 5% against the market (shorts gain)",
    ))

    # ── Most-concentrated classified sector −10% ─────────────────────────────
    sector_gross: dict[str, float] = {}
    for v in views:
        sec = sectors.get(v.instrument_key, _UNCLASSIFIED)
        if sec != _UNCLASSIFIED:
            sector_gross[sec] = sector_gross.get(sec, 0.0) + v.market_value
    if sector_gross:
        top_sector = max(sector_gross, key=sector_gross.get)
        sector_exposure = math.fsum(
            v.signed_exposure for v in views if sectors.get(v.instrument_key) == top_sector
        )
        scenarios.append(StressScenario(
            key="sector_down",
            label=f"{top_sector} sector −10%",
            delta_pct=as_pct(-_SECTOR_SHOCK * sector_exposure),
            detail=f"Most-concentrated sector ({top_sector}) repriced 10% against the market",
        ))

    # ── 2-σ adverse day (vol double) ──────────────────────────────────────────
    vol_delta = 0.0
    excluded = 0
    for v in views:
        sigma = sigma_by_key.get(v.instrument_key)
        if sigma is None or not math.isfinite(sigma) or sigma <= 0.0:
            excluded += 1
            continue
        vol_delta -= abs(_VOL_SHOCK_MULT * sigma * v.market_value)  # always adverse
    detail = "Each position moves 2× its daily volatility against you"
    if excluded:
        detail += f" ({excluded} excluded for insufficient history)"
    scenarios.append(StressScenario(
        key="vol_double",
        label="2σ adverse day",
        delta_pct=as_pct(vol_delta),
        detail=detail,
    ))

    return StressScan(scenarios=scenarios)


def _daily_sigma(returns: pd.DataFrame | None) -> dict[str, float]:
    """Per-instrument daily-log-return std, only where enough observations exist."""
    if returns is None or returns.empty:
        return {}
    obs = returns.count()
    out: dict[str, float] = {}
    for key in returns.columns:
        if obs[key] < _CORR_MIN_OBS:
            continue
        sigma = returns[key].std()
        if sigma is not None and math.isfinite(sigma):
            out[key] = float(sigma)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# I/O helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _load_position_views(session: AsyncSession, portfolio_id: UUID) -> list[_PositionView]:
    from app.models.paper_trading import PaperPosition

    rows = (await session.execute(
        select(PaperPosition).where(
            and_(
                PaperPosition.portfolio_id == portfolio_id,
                PaperPosition.status == "OPEN",
            )
        )
    )).scalars().all()

    views: list[_PositionView] = []
    for p in rows:
        avg_cost = float(p.avg_cost_price)
        last_price = float(p.last_price) if p.last_price is not None else avg_cost
        market_value = last_price * p.quantity
        signed = market_value if p.side == "LONG" else -market_value
        views.append(_PositionView(
            symbol=p.symbol,
            instrument_key=p.instrument_key,
            side=p.side,
            quantity=p.quantity,
            avg_cost=avg_cost,
            stop_loss=float(p.stop_loss) if p.stop_loss is not None else None,
            last_price=last_price,
            market_value=market_value,
            signed_exposure=signed,
        ))
    return views


async def _resolve_sectors(session: AsyncSession, views: list[_PositionView]) -> dict[str, str]:
    """instrument_key → sector, via fundamentals then symbol-map fallback, else 'Unclassified'."""
    from app.models.fundamentals import CompanyFundamentalsProfile

    keys = [v.instrument_key for v in views]
    rows = (await session.execute(
        select(CompanyFundamentalsProfile.instrument_key, CompanyFundamentalsProfile.sector)
        .where(CompanyFundamentalsProfile.instrument_key.in_(keys))
    )).all()
    cfp = {k: s for k, s in rows if s}

    resolved: dict[str, str] = {}
    for v in views:
        sec = cfp.get(v.instrument_key) or sector_map.get_sector(v.symbol, None)
        resolved[v.instrument_key] = sec or _UNCLASSIFIED
    return resolved


async def _load_returns_matrix(
    session: AsyncSession, instrument_keys: list[str]
) -> pd.DataFrame | None:
    """
    Wide DataFrame of daily log returns (index = date, columns = instrument_key)
    over the trailing window.  ``None`` when there is no usable data.
    """
    from app.models.upstox_data import UpstoxOHLCV

    if len(instrument_keys) < 2:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=_CORR_LOOKBACK_CALENDAR_DAYS)
    rows = (await session.execute(
        select(UpstoxOHLCV.instrument_key, UpstoxOHLCV.timestamp, UpstoxOHLCV.close)
        .where(
            and_(
                UpstoxOHLCV.instrument_key.in_(instrument_keys),
                UpstoxOHLCV.timeframe == _DAILY_TIMEFRAME,
                UpstoxOHLCV.timestamp >= cutoff,
            )
        )
        .order_by(UpstoxOHLCV.timestamp)
    )).all()
    if not rows:
        return None

    frame = pd.DataFrame(
        [(k, ts, float(c)) for k, ts, c in rows if c is not None],
        columns=["instrument_key", "timestamp", "close"],
    )
    if frame.empty:
        return None

    wide = frame.pivot_table(index="timestamp", columns="instrument_key", values="close")
    wide = wide.sort_index().tail(_CORR_WINDOW_DAYS + 1)
    # Log returns — same definition as ml.features.ohlcv_features.compute_price_features.
    returns = np.log(wide / wide.shift(1)).iloc[1:]
    return returns if not returns.empty else None


# ──────────────────────────────────────────────────────────────────────────────
# Notes + empty
# ──────────────────────────────────────────────────────────────────────────────

def _build_notes(
    car: CapitalAtRiskStat,
    correlation: CorrelationStat,
    sector: SectorConcentration,
    total_positions: int,
) -> list[str]:
    notes: list[str] = []
    if car.positions_without_stop > 0:
        notes.append(
            f"{car.positions_without_stop} open position(s) have no stop-loss and are "
            "excluded from capital-at-risk."
        )
    if correlation.excluded_positions > 0:
        notes.append(
            f"{correlation.excluded_positions} position(s) excluded from correlation "
            f"(fewer than {_CORR_MIN_OBS} days of price history)."
        )
    if total_positions < 2:
        notes.append("Correlation needs at least two positions.")
    if sector.unclassified_weight_pct > 0:
        notes.append(
            f"{sector.unclassified_weight_pct:.1f}% of the book has no resolvable sector "
            "(shown as Unclassified)."
        )
    return notes


def _empty_stats(portfolio_id: UUID, current_cash: float, now: datetime) -> PortfolioInsightStats:
    """Zeroed panel for a portfolio with no open positions."""
    return PortfolioInsightStats(
        portfolio_id=portfolio_id,
        portfolio_value=round(current_cash, 2),
        open_position_count=0,
        capital_at_risk=CapitalAtRiskStat(
            capital_at_risk=0.0, capital_at_risk_pct=0.0,
            positions_with_stop=0, positions_without_stop=0,
        ),
        single_name=SingleNameConcentration(
            max_weight_pct=0.0, max_weight_symbol=None, hhi=0.0,
            effective_positions=0.0, top_holdings=[],
        ),
        sector=SectorConcentration(
            max_sector=None, max_sector_weight_pct=0.0,
            unclassified_weight_pct=0.0, breakdown=[],
        ),
        correlation=CorrelationStat(
            covered_positions=0, excluded_positions=0, window_days=_CORR_WINDOW_DAYS,
        ),
        stress=StressScan(scenarios=[]),
        notes=["No open positions."],
        computed_at=now,
    )
