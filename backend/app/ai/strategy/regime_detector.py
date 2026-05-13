"""
Cortex AI — Daily Post-Market Regime Detector
==============================================
Detects market regimes for all 100 SIGNAL_SCHEDULED_UNIVERSE symbols once
daily after NSE market close (configurable via REGIME_DETECTION_HOUR_IST,
default 16:00 IST — 30 min after 15:30 close).

Architecture
------------
- Runs as a long-lived asyncio loop (one of 10 worker tasks).
- On each 5-minute poll tick, checks whether it is time to run today's
  detection. Runs at most once per trading day.
- Symbol universe: settings.SIGNAL_SCHEDULED_UNIVERSE (100 symbols).
- Regime computation: delegates to RegimeService._compute_regime_from_candles()
  (ADX/RSI/ATR/Bollinger-width rule-based classifier — identical to the live
  market overview endpoint, no additional training required).
- OHLCV data: loaded in a single batch SQL query (no N+1).
- Instrument resolution: single batch query against instrument_master.
- Change detection: skips writing if regime is unchanged AND last detection
  is < 26 hours old (allows daily refresh while avoiding redundant rows).
- Writes: bulk-inserts all new AIRegimeDetection records in one transaction.
- Redis: publishes per-symbol regime notifications (cai:regime:{symbol}).
- Prometheus: regime_detection_runs_total, regime_detection_duration_seconds,
  regime_detection_symbols_total.
- On error: publishes to cai:ml:regime_detection_errors + logs at ERROR.

Design decisions
----------------
- "Once daily after market close" is correct for daily OHLCV data: running
  hourly would recompute the same indicators from the same candles (no new
  daily candle arrives until the next trading day).
- Rule-based classifier preferred over GMM: already production-tested in
  the live market overview API, NSE-tuned thresholds, deterministic, zero
  training data requirement, interpretable.
- Batch OHLCV query (window function, single round-trip) prevents the
  N×DB-roundtrip penalty that would arise from per-symbol queries.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.fusion.models import AIRegimeDetection
from app.core.config import get_settings
from app.services.market_calendar import nse_calendar
from app.services.regime_service import RegimeService

logger = logging.getLogger(__name__)
settings = get_settings()

IST = ZoneInfo("Asia/Kolkata")

# Poll interval: check every 5 minutes whether it is time to run
_POLL_INTERVAL_SECONDS: int = 300

# Hours since last detection below which we skip writing an unchanged regime
# (26h > 24h allows for clock skew; ensures daily refresh always lands)
_SKIP_UNCHANGED_THRESHOLD_HOURS: float = 26.0


# ──────────────────────────────────────────────────────────────────────────────
# Public loop entry point (called by worker.py)
# ──────────────────────────────────────────────────────────────────────────────

async def regime_detection_loop(session_factory: async_sessionmaker) -> None:
    """
    Perpetual post-market regime detection loop.

    Triggers one full detection run per trading day after REGIME_DETECTION_HOUR_IST
    (default 16:00 IST).  Polls every 5 minutes so the detection starts within
    5 minutes of the configured hour — precise enough for a daily batch job.

    Args:
        session_factory: SQLAlchemy async session factory (worker pool).
    """
    logger.info(
        "Regime detection loop started — daily trigger at %02d:00 IST on trading days",
        settings.REGIME_DETECTION_HOUR_IST,
    )

    last_run_date: date | None = None

    try:
        while True:
            now_ist = datetime.now(IST)
            today = now_ist.date()

            # Refresh NSE holiday calendar (best-effort; uses cached copy on failure)
            try:
                await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=5.0)
            except Exception:
                pass

            trigger_hour_reached = now_ist.hour >= settings.REGIME_DETECTION_HOUR_IST
            not_yet_run_today = last_run_date != today
            is_trading_day = nse_calendar.is_trading_day(today)

            if trigger_hour_reached and not_yet_run_today and is_trading_day:
                logger.info(
                    "Triggering post-market regime detection for %s "
                    "(IST=%s, symbols=%d)",
                    today,
                    now_ist.strftime("%H:%M"),
                    len(settings.SIGNAL_SCHEDULED_UNIVERSE),
                )

                success = await _run_regime_detection(session_factory, run_date=today)
                if success:
                    last_run_date = today
                # On failure, last_run_date is NOT updated so the next poll
                # will retry — capped to one retry per 5-minute window.

            elif not_yet_run_today and is_trading_day and not trigger_hour_reached:
                logger.debug(
                    "Waiting for post-market trigger: today=%s, "
                    "trigger_hour=%02d:00 IST, current=%s IST",
                    today,
                    settings.REGIME_DETECTION_HOUR_IST,
                    now_ist.strftime("%H:%M"),
                )
            else:
                logger.debug(
                    "Regime detection skipped: today=%s, "
                    "trading_day=%s, already_ran=%s",
                    today, is_trading_day, last_run_date == today,
                )

            try:
                await asyncio.wait_for(
                    asyncio.Event().wait(),
                    timeout=float(_POLL_INTERVAL_SECONDS),
                )
            except asyncio.TimeoutError:
                pass  # expected — normal poll tick

    except asyncio.CancelledError:
        logger.info("Regime detection loop cancelled")
        raise
    finally:
        logger.info("Regime detection loop stopped")


# ──────────────────────────────────────────────────────────────────────────────
# Core detection batch run
# ──────────────────────────────────────────────────────────────────────────────

async def _run_regime_detection(
    session_factory: async_sessionmaker,
    run_date: date,
) -> bool:
    """
    Execute one full regime detection run for all 100 universe symbols.

    Returns True on success, False on unrecoverable error.
    Metrics and Redis alerts are published regardless of outcome.
    """
    from app.core.metrics import (
        regime_detection_duration_seconds,
        regime_detection_runs_total,
        regime_detection_symbols_total,
    )

    t0 = time.monotonic()
    symbols = list(dict.fromkeys(settings.SIGNAL_SCHEDULED_UNIVERSE))  # dedup, preserve order
    symbols_processed = 0

    try:
        async with session_factory() as session:
            # ── Step 1: resolve symbol → instrument_key in one query ──────────
            instrument_map = await _resolve_instruments_batch(session, symbols)

            # ── Step 2: load last known regime per symbol (change detection) ──
            last_regimes = await _get_last_detections(session, symbols)

            # ── Step 3: batch OHLCV load (single round-trip, window function) ─
            ikeys = [
                info["instrument_key"]
                for sym in symbols
                if (info := instrument_map.get(sym)) is not None
            ]
            ohlcv_batch = await RegimeService._load_ohlcv_batch(session, ikeys, limit=65)

            # ── Step 4: compute regimes and collect new detections ─────────────
            now_utc = datetime.now(timezone.utc)
            new_detections: list[AIRegimeDetection] = []
            skipped_unchanged = 0
            skipped_no_data = 0

            for symbol in symbols:
                info = instrument_map.get(symbol)
                if info is None:
                    logger.debug(
                        "Symbol %s absent from instrument_master — skipping", symbol
                    )
                    regime_detection_symbols_total.labels(status="skipped").inc()
                    skipped_no_data += 1
                    continue

                candles = ohlcv_batch.get(info["instrument_key"], [])
                if len(candles) < 20:
                    logger.debug(
                        "Symbol %s has only %d daily candles (need ≥20) — skipping",
                        symbol, len(candles),
                    )
                    regime_detection_symbols_total.labels(status="skipped").inc()
                    skipped_no_data += 1
                    continue

                try:
                    result = RegimeService._compute_regime_from_candles(candles)
                except Exception as compute_exc:
                    logger.warning(
                        "Regime computation failed for %s: %s", symbol, compute_exc
                    )
                    regime_detection_symbols_total.labels(status="failed").inc()
                    continue

                regime_type: str = result["regime"]

                # Skip if regime is unchanged and the last detection is fresh
                last = last_regimes.get(symbol)
                if last is not None and last["regime_type"] == regime_type:
                    hours_since = (
                        (now_utc - last["detection_timestamp"]).total_seconds() / 3600
                    )
                    if hours_since < _SKIP_UNCHANGED_THRESHOLD_HOURS:
                        logger.debug(
                            "Symbol %s regime unchanged (%s, %.1fh ago) — skipping write",
                            symbol, regime_type, hours_since,
                        )
                        regime_detection_symbols_total.labels(status="unchanged").inc()
                        skipped_unchanged += 1
                        symbols_processed += 1
                        continue

                detection = AIRegimeDetection(
                    symbol=symbol,
                    detection_timestamp=now_utc,
                    regime_type=regime_type,
                    confidence_score=Decimal(str(result["confidence"])),
                    contributing_factors={
                        "indicators": result["indicators"],
                        "change_pct": result["change_pct"],
                        "change_pct_20d": result["change_pct_20d"],
                        "data_points": len(candles),
                    },
                    volatility_index=Decimal(str(result["indicators"]["atr_pct"])),
                    volume_index=None,  # not computed by rule-based classifier
                    trend_strength=Decimal(str(result["indicators"]["adx"])),
                )
                session.add(detection)
                new_detections.append(detection)
                regime_detection_symbols_total.labels(status="detected").inc()
                symbols_processed += 1

            # ── Step 5: commit all new detections in one transaction ───────────
            if new_detections:
                await session.commit()
                # Refresh to get server-assigned IDs before publishing
                for det in new_detections:
                    await session.refresh(det)
                logger.info(
                    "Regime detection %s: %d new detections, %d unchanged, "
                    "%d skipped (no data)",
                    run_date, len(new_detections), skipped_unchanged, skipped_no_data,
                )
            else:
                logger.info(
                    "Regime detection %s: all %d symbols unchanged or skipped",
                    run_date, len(symbols),
                )

        # ── Step 6: publish per-symbol Redis notifications (outside session) ──
        if new_detections:
            await _publish_regime_updates(new_detections)

        duration = time.monotonic() - t0
        regime_detection_duration_seconds.observe(duration)
        regime_detection_runs_total.labels(status="success").inc()
        logger.info(
            "Regime detection run complete: date=%s duration=%.2fs "
            "symbols=%d detections=%d",
            run_date, duration, symbols_processed, len(new_detections),
        )
        return True

    except Exception as exc:
        duration = time.monotonic() - t0
        regime_detection_runs_total.labels(status="failure").inc()
        logger.error(
            "Regime detection run FAILED: date=%s duration=%.2fs error=%s",
            run_date, duration, exc,
            exc_info=True,
        )
        await _publish_regime_error_alert(
            error=str(exc),
            symbols_attempted=len(symbols),
            symbols_processed=symbols_processed,
            run_date=run_date,
        )
        return False


# ──────────────────────────────────────────────────────────────────────────────
# DB helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _resolve_instruments_batch(
    session: AsyncSession,
    symbols: list[str],
) -> dict[str, dict]:
    """
    Map trading_symbol → {instrument_key, name} for all NSE EQ instruments.
    Single query — no N+1.
    """
    if not symbols:
        return {}
    result = await session.execute(
        text(
            """
            SELECT trading_symbol, instrument_key, name
            FROM instrument_master
            WHERE trading_symbol = ANY(:syms)
              AND exchange = 'NSE'
              AND (instrument_type = 'EQ' OR instrument_type IS NULL)
            """
        ),
        {"syms": symbols},
    )
    return {
        row[0]: {"instrument_key": row[1], "name": row[2] or row[0]}
        for row in result.fetchall()
    }


async def _get_last_detections(
    session: AsyncSession,
    symbols: list[str],
) -> dict[str, dict]:
    """
    Fetch the most recent AIRegimeDetection per symbol (one query).
    Returns {symbol: {regime_type, detection_timestamp}}.
    """
    if not symbols:
        return {}
    result = await session.execute(
        text(
            """
            SELECT DISTINCT ON (symbol)
                symbol, regime_type, detection_timestamp
            FROM ai_regime_detections
            WHERE symbol = ANY(:syms)
            ORDER BY symbol, detection_timestamp DESC
            """
        ),
        {"syms": symbols},
    )
    return {
        row[0]: {
            "regime_type": row[1],
            "detection_timestamp": row[2],
        }
        for row in result.fetchall()
    }


# ──────────────────────────────────────────────────────────────────────────────
# Redis helpers
# ──────────────────────────────────────────────────────────────────────────────

async def _publish_regime_updates(detections: list[AIRegimeDetection]) -> None:
    """
    Publish one Redis message per new detection to cai:regime:{symbol}.
    Fire-and-forget — failure is logged and swallowed.
    """
    from app.core.redis import RedisChannels, get_pubsub_client

    try:
        pubsub = get_pubsub_client()
        for det in detections:
            try:
                await pubsub.publish_json(
                    RedisChannels.REGIME_SYMBOL.format(symbol=det.symbol),
                    {
                        "symbol": det.symbol,
                        "regime_type": det.regime_type,
                        "confidence": float(det.confidence_score),
                        "detection_timestamp": det.detection_timestamp.isoformat(),
                        "contributing_factors": det.contributing_factors,
                    },
                )
            except Exception as pub_exc:
                logger.debug(
                    "Failed to publish regime update for %s: %s", det.symbol, pub_exc
                )
    except Exception as exc:
        logger.warning("Failed to publish regime updates to Redis: %s", exc)


async def _publish_regime_error_alert(
    *,
    error: str,
    symbols_attempted: int,
    symbols_processed: int,
    run_date: date,
) -> None:
    """Publish a regime detection failure alert.  Never raises."""
    from app.core.redis import RedisChannels, get_pubsub_client

    try:
        pubsub = get_pubsub_client()
        await pubsub.publish_json(
            RedisChannels.ML_REGIME_ERRORS,
            {
                "error": error,
                "symbols_attempted": symbols_attempted,
                "symbols_processed": symbols_processed,
                "run_date": run_date.isoformat(),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception as exc:
        logger.warning("Failed to publish regime error alert: %s", exc)
