"""
Sentiment Drift Monitor — Daily Statistical Baseline Check
==========================================================
Compares the 7-day rolling distribution of stored sentiment results
(``ai_nlp_results``) against the 30-day baseline: per-label fractions, mean
score, and the total variation distance between the two label distributions.

Why statistical, not model-based
--------------------------------
FinBERT was permanently removed from production (WS4 binding decision) — there
is no second model to dual-write against. What CAN drift silently is Gemini's
calibration: a model-version bump or prompt regression shifts the label mix or
score distribution across ALL articles at once, which is exactly what a
7d-vs-30d comparison surfaces. Genuine market-mood swings also move these
numbers — the monitor flags, humans interpret; nothing here auto-acts.

Scheduling
----------
Same pattern as rag_cleanup: hourly tick, once per 24h cycle, last-run wall
time persisted in Redis (``cai:sentiment:drift:last_run``) so a worker restart
mid-cycle does not re-run immediately.

Outputs
-------
Prometheus gauges (label fractions + mean score per window, distribution
distance) for Grafana panels/alerts, a run-outcome counter, and a WARNING log
when either drift threshold is crossed.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.redis import CacheService

logger = logging.getLogger(__name__)

# Redis key that records when the last successful drift check ran.
_EPOCH_KEY = "cai:sentiment:drift:last_run"
_EPOCH_TTL = 8 * 86_400  # outlives the daily cycle with margin

# How often the outer loop wakes to check whether a cycle is due.
_TICK_SECS = 3_600  # 1 hour

_LABELS = ("positive", "negative", "neutral")

# Below this many scored rows in a window, fractions are statistically noise —
# publish nothing and count the run as insufficient_data.
_MIN_ROWS_PER_WINDOW = 50

# Warn thresholds. Total variation distance is half the L1 distance between
# the two label distributions (0 = identical, 1 = disjoint); 0.25 means ≥25%
# of probability mass moved. Mean score is on the -1..1 scale.
_TVD_WARN_THRESHOLD = 0.25
_MEAN_SHIFT_WARN_THRESHOLD = 0.30

# Only rows produced by a real model run count — neutral fallbacks written on
# LLM failure would dilute both windows identically and mask real drift.
_WINDOW_STATS_SQL = text(
    """
    SELECT sentiment_label AS label,
           COUNT(*)         AS n,
           AVG(sentiment_score) AS mean_score
    FROM ai_nlp_results
    WHERE created_at >= :cutoff
      AND sentiment_label IN ('positive', 'negative', 'neutral')
      AND sentiment_score IS NOT NULL
      AND model_used != 'unavailable'
    GROUP BY sentiment_label
    """
)


@dataclass(frozen=True)
class WindowStats:
    """Aggregated sentiment statistics for one rolling window."""

    total: int
    fractions: dict[str, float]  # label -> fraction of total (all three keys)
    mean_score: float


def build_window_stats(rows: list) -> WindowStats:
    """
    Shape (label, n, mean_score) group rows into WindowStats.

    Pure function — unit-testable without a DB. Labels absent from the rows
    get fraction 0.0; the overall mean is the count-weighted combination of
    the per-label means.
    """
    counts = {label: 0 for label in _LABELS}
    weighted_sum = 0.0
    for row in rows:
        if row.label not in counts:
            continue
        n = int(row.n)
        counts[row.label] = n
        weighted_sum += float(row.mean_score) * n

    total = sum(counts.values())
    if total == 0:
        return WindowStats(total=0, fractions={l: 0.0 for l in _LABELS}, mean_score=0.0)
    return WindowStats(
        total=total,
        fractions={label: counts[label] / total for label in _LABELS},
        mean_score=weighted_sum / total,
    )


def total_variation_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """TVD between two label distributions: 0.5 * Σ|a_l − b_l|, in [0, 1]."""
    return 0.5 * sum(abs(a.get(l, 0.0) - b.get(l, 0.0)) for l in _LABELS)


async def _run_drift_check(session_factory: async_sessionmaker) -> str:
    """
    Execute one drift comparison and publish metrics.

    Returns the run status for the outcome counter:
    "success" | "insufficient_data".
    """
    from app.core.metrics import (
        sentiment_drift_distribution_distance,
        sentiment_drift_label_fraction,
        sentiment_drift_mean_score,
    )

    now = datetime.now(timezone.utc)
    stats: dict[str, WindowStats] = {}
    async with session_factory() as db:
        for window, days in (("7d", 7), ("30d", 30)):
            result = await db.execute(
                _WINDOW_STATS_SQL, {"cutoff": now - timedelta(days=days)}
            )
            stats[window] = build_window_stats(result.all())

    if any(s.total < _MIN_ROWS_PER_WINDOW for s in stats.values()):
        logger.info(
            "sentiment_drift: insufficient data (7d=%d rows, 30d=%d rows, "
            "minimum %d per window) — skipping metric publication",
            stats["7d"].total, stats["30d"].total, _MIN_ROWS_PER_WINDOW,
        )
        return "insufficient_data"

    for window, window_stats in stats.items():
        for label, fraction in window_stats.fractions.items():
            sentiment_drift_label_fraction.labels(label=label, window=window).set(fraction)
        sentiment_drift_mean_score.labels(window=window).set(window_stats.mean_score)

    tvd = total_variation_distance(stats["7d"].fractions, stats["30d"].fractions)
    sentiment_drift_distribution_distance.set(tvd)

    mean_shift = abs(stats["7d"].mean_score - stats["30d"].mean_score)
    log = logger.warning if (
        tvd > _TVD_WARN_THRESHOLD or mean_shift > _MEAN_SHIFT_WARN_THRESHOLD
    ) else logger.info
    log(
        "sentiment_drift: tvd=%.3f mean_7d=%.3f mean_30d=%.3f shift=%.3f "
        "rows_7d=%d rows_30d=%d (warn at tvd>%.2f or shift>%.2f)",
        tvd, stats["7d"].mean_score, stats["30d"].mean_score, mean_shift,
        stats["7d"].total, stats["30d"].total,
        _TVD_WARN_THRESHOLD, _MEAN_SHIFT_WARN_THRESHOLD,
    )
    return "success"


async def sentiment_drift_loop(
    session_factory: async_sessionmaker,
    redis: CacheService,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Daily sentiment drift monitoring loop (worker sidecar task).

    Hourly tick; runs one comparison per 24h, epoch-gated via Redis so worker
    restarts resume the schedule instead of re-running.
    """
    from app.core.metrics import sentiment_drift_runs_total

    logger.info(
        "Sentiment drift monitor started (tick_secs=%d, min_rows=%d)",
        _TICK_SECS, _MIN_ROWS_PER_WINDOW,
    )

    while not shutdown.is_set():
        now = datetime.now(timezone.utc)

        should_run = True
        try:
            last_run_str: str | None = await redis.get(_EPOCH_KEY)
            if last_run_str is not None:
                age = now - datetime.fromisoformat(last_run_str)
                if age < timedelta(hours=24):
                    should_run = False
        except Exception as exc:
            # Redis unavailable — run defensively rather than miss a cycle.
            logger.warning(
                "sentiment_drift: epoch key read failed (%s); running as fallback", exc
            )

        if should_run:
            try:
                status = await _run_drift_check(session_factory)
                sentiment_drift_runs_total.labels(status=status).inc()
                try:
                    await redis.set(_EPOCH_KEY, now.isoformat(), ttl=_EPOCH_TTL)
                except Exception as exc:
                    logger.warning(
                        "sentiment_drift: epoch key write failed (non-fatal): %s", exc
                    )
            except Exception as exc:
                sentiment_drift_runs_total.labels(status="error").inc()
                logger.exception(
                    "sentiment_drift: cycle failed — will retry next tick: %s", exc
                )

        if on_cycle is not None:
            on_cycle()

        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_TICK_SECS)
        except asyncio.TimeoutError:
            pass  # normal wakeup

    logger.info("Sentiment drift monitor exiting cleanly (shutdown signalled)")
