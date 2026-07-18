"""
Cross-sectional rank normalization for fundamental features (WS2b).

v2.0.0 replaces per-symbol median imputation + rolling z-score (which mapped
the broadcast-constant fundamentals to exactly 0) with per-date
cross-sectional percentile ranks in [-1, 1] — the standard Gu–Kelly–Xiu
treatment for firm characteristics:

    rank = 2 * pct_rank(value among all symbols on that date) - 1
    missing (NaN) → 0.0 (neutral), n_obs <= 1 or all-equal → 0.0

Training computes ranks in-panel via ``rank_normalize_panel``. Inference
cannot see the panel, so training-time distributions are persisted as
101-point raw-value quantile grids (``ml_feature_cross_stats``, migration
0056) and reproduced with ``rank_transform_with_grid`` — ``np.interp`` of the
raw value over the grid, clipped to [-1, 1]. Grid round-trip parity with the
in-panel ranks is unit-tested.

The math core is pure (no DB); ``persist_cross_sectional_stats`` /
``load_cross_sectional_stats`` handle the grid table.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLFeatureCrossStats

logger = logging.getLogger(__name__)

#: Grid resolution: p0..p100 of the cross-section's raw values.
N_QUANTILES = 101

#: Interpolation targets: quantile p_i maps to rank -1 + 2*(i/100).
_RANK_GRID = np.linspace(-1.0, 1.0, N_QUANTILES)

_QUANTILE_LEVELS = np.linspace(0.0, 1.0, N_QUANTILES)

#: Upsert batch size for grid persistence.
_PERSIST_CHUNK = 500


@dataclass(frozen=True)
class CrossStat:
    """One date's cross-sectional distribution of one feature's raw values."""
    quantiles: tuple[float, ...]  # 101 raw-value floats, p0..p100, non-decreasing
    median: float
    n_obs: int

    @property
    def degenerate(self) -> bool:
        """True when the grid cannot express an ordering (0/1 obs or all-equal)."""
        return self.n_obs <= 1 or self.quantiles[0] == self.quantiles[-1]


# ── Pure core ──────────────────────────────────────────────────────────────────

def _date_keys(df: pd.DataFrame) -> pd.Series:
    """Normalized calendar dates from a frame's ``timestamp`` column (tz-safe)."""
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is not None:
        ts = ts.dt.tz_localize(None)
    return ts.dt.normalize().dt.date


def rank_normalize_panel(
    results: dict[str, pd.DataFrame],
    feature_names: list[str],
) -> dict[date, dict[str, CrossStat]]:
    """
    Rank-normalize ``feature_names`` across symbols, per date, **in place**.

    Each symbol frame in ``results`` must carry a ``timestamp`` column. For
    every (date, feature) cell the raw value is replaced by
    ``2 * pct_rank - 1`` among all symbols with a value on that date; NaN and
    degenerate cross-sections (n_obs <= 1 or all values equal) become 0.0.

    Returns the per-date ``CrossStat`` grids (raw-value quantiles) for
    persistence — the inference-side ``rank_transform_with_grid`` reproduces
    these in-panel ranks from raw values. Dates where a feature has no valid
    observations get no CrossStat (inference degrades to neutral 0).
    """
    if not results:
        return {}

    # One long panel; per-symbol frames stay contiguous so transformed values
    # can be written back by slice.
    slices: list[tuple[str, int, int]] = []  # (symbol, start, stop)
    parts: list[pd.DataFrame] = []
    offset = 0
    for symbol, df in results.items():
        part = pd.DataFrame({"_date": _date_keys(df)})
        for feat in feature_names:
            part[feat] = (
                pd.to_numeric(df[feat], errors="coerce").to_numpy(dtype=float)
                if feat in df.columns else np.nan
            )
        parts.append(part)
        slices.append((symbol, offset, offset + len(df)))
        offset += len(df)
    panel = pd.concat(parts, ignore_index=True)

    stats: dict[date, dict[str, CrossStat]] = {}
    grouped_dates = panel.groupby("_date", sort=False)

    for feat in feature_names:
        # Snapshot raw values BEFORE the in-place rank overwrite — grids must
        # be built from the raw distribution.
        raw_all = panel[feat].to_numpy(dtype=float).copy()
        grp = panel.groupby("_date", sort=False)[feat]

        # Degenerate cross-sections: <2 valid values, or all valid values equal.
        n_obs = grp.transform("count")
        nunique = grp.transform("nunique")
        pct = grp.rank(pct=True, method="average")

        ranked = 2.0 * pct - 1.0
        ranked = ranked.where((n_obs > 1) & (nunique > 1), 0.0)
        ranked = ranked.fillna(0.0)
        panel[feat] = ranked

        for dt, idx in grouped_dates.indices.items():
            raw = raw_all[idx]
            valid = raw[~np.isnan(raw)]
            if valid.size == 0:
                continue
            quantiles = np.quantile(valid, _QUANTILE_LEVELS)
            stats.setdefault(dt, {})[feat] = CrossStat(
                quantiles=tuple(float(q) for q in quantiles),
                median=float(np.median(valid)),
                n_obs=int(valid.size),
            )

    # Write ranks back into the caller's frames.
    for symbol, start, stop in slices:
        df = results[symbol]
        block = panel.iloc[start:stop]
        for feat in feature_names:
            df[feat] = block[feat].to_numpy()

    logger.info(
        "Cross-sectional rank normalization: %d features  %d symbols  %d dates",
        len(feature_names), len(results), len(stats),
    )
    return stats


def rank_transform_with_grid(raw: float, stat: CrossStat | None) -> float:
    """
    Map one raw value to its [-1, 1] rank via a persisted quantile grid.

    NaN raw, missing stat, or a degenerate grid → neutral 0.0. Values outside
    the grid clip to ±1 (``np.interp`` clamps to the endpoint ranks).
    """
    if stat is None or stat.degenerate:
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if np.isnan(value):
        return 0.0
    return float(np.interp(value, np.asarray(stat.quantiles), _RANK_GRID))


# ── Persistence (ml_feature_cross_stats, migration 0056) ──────────────────────

async def persist_cross_sectional_stats(
    session: AsyncSession,
    stats: dict[date, dict[str, CrossStat]],
    feature_version: str,
) -> int:
    """
    Upsert grids into ``ml_feature_cross_stats``; returns rows written.

    Conflict target is the (as_of_date, feature_name, feature_version) unique
    key, so re-running a day's pass refreshes its grids idempotently. The
    caller owns the transaction.
    """
    rows = [
        {
            "as_of_date": dt,
            "feature_name": feat,
            "feature_version": feature_version,
            "quantiles": list(stat.quantiles),
            "median": stat.median,
            "n_obs": stat.n_obs,
        }
        for dt, per_feature in stats.items()
        for feat, stat in per_feature.items()
    ]
    for start in range(0, len(rows), _PERSIST_CHUNK):
        chunk = rows[start:start + _PERSIST_CHUNK]
        stmt = pg_insert(MLFeatureCrossStats).values(chunk)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_ml_feature_cross_stats_date_feature_version",
            set_={
                "quantiles": stmt.excluded.quantiles,
                "median":    stmt.excluded.median,
                "n_obs":     stmt.excluded.n_obs,
            },
        )
        await session.execute(stmt)
    return len(rows)


async def load_cross_sectional_stats(
    session: AsyncSession,
    feature_names: list[str],
    start_date: date,
    end_date: date,
    feature_version: str,
) -> dict[date, dict[str, CrossStat]]:
    """Read grids for the date range back into the in-memory shape."""
    result = await session.execute(
        select(
            MLFeatureCrossStats.as_of_date,
            MLFeatureCrossStats.feature_name,
            MLFeatureCrossStats.quantiles,
            MLFeatureCrossStats.median,
            MLFeatureCrossStats.n_obs,
        ).where(
            MLFeatureCrossStats.feature_version == feature_version,
            MLFeatureCrossStats.feature_name.in_(feature_names),
            MLFeatureCrossStats.as_of_date >= start_date,
            MLFeatureCrossStats.as_of_date <= end_date,
        )
    )
    stats: dict[date, dict[str, CrossStat]] = {}
    for as_of, feat, quantiles, median, n_obs in result.all():
        stats.setdefault(as_of, {})[feat] = CrossStat(
            quantiles=tuple(float(q) for q in quantiles),
            median=float(median),
            n_obs=int(n_obs),
        )
    return stats
