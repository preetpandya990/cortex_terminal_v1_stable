"""
Cortex AI — ML Feedback & Regime Backfill
==========================================
One-time migration script to populate historical data that was missing due to:

  1. Regime detector stub (returns None since inception — table was empty)
  2. Wrong column names in _get_regime_at_entry (always returned None)
  3. No compute_ml_feedback calls prior to this implementation

Run once immediately after deploying the ML feedback system.

Phases
------
Phase 0 — Historical regime backfill for outcomes that predate the detection system
    For outcomes with market_regime_at_entry IS NULL that have no matching
    ai_regime_detections row for their entry date, compute the regime from
    historical OHLCV data (candles up to the entry date) and write a synthetic
    AIRegimeDetection record.  Uses detection_timestamp = 16:00 IST on the entry
    date to represent the post-market regime for that trading day.
    After this phase, run Phase 2 to copy the values into the outcomes.

Phase 1 — Regime detection backfill
    Runs _run_regime_detection() for all 100 SIGNAL_SCHEDULED_UNIVERSE symbols
    using today's OHLCV data (most recent available).  This populates
    ai_regime_detections so that new outcomes will have market_regime_at_entry.

Phase 2 — market_regime_at_entry backfill
    Finds all PaperTradeOutcome rows with market_regime_at_entry IS NULL and
    retries _get_regime_at_entry().  After Phase 0/1 complete, this will find
    matching detections for all resolvable outcomes.

Phase 3 — ML feedback backfill
    Finds all PaperTradeOutcome rows with ml_direction_correct IS NULL (those
    that were never processed) and runs compute_ml_feedback() on them.
    Uses batched processing (BATCH_SIZE=50) to avoid overwhelming the DB.

Usage
-----
    # Run all phases including historical regime backfill:
    python -m app.services.ml_feedback_backfill

    # Run individual phases:
    python -m app.services.ml_feedback_backfill --phase 0
    python -m app.services.ml_feedback_backfill --phase 1
    python -m app.services.ml_feedback_backfill --phase 2
    python -m app.services.ml_feedback_backfill --phase 3

    # Dry-run (report what would be done, make no changes):
    python -m app.services.ml_feedback_backfill --dry-run

Output
------
Structured logs to stdout.  Final summary includes counts for every phase.
Exit code 0 on success, 1 on partial failure, 2 on total failure.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("backfill")

BATCH_SIZE = 50  # outcomes per batch for Phase 2 & 3
IST = ZoneInfo("Asia/Kolkata")


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0 — Historical regime backfill for pre-deployment outcomes
# ──────────────────────────────────────────────────────────────────────────────

async def backfill_historical_regimes(
    session_factory,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    For outcomes with NULL market_regime_at_entry that predate the detection
    system, compute the regime from historical OHLCV data and write synthetic
    AIRegimeDetection records.

    Returns (written_count, skipped_count).
    """
    from sqlalchemy import text

    from app.ai.fusion.models import AIRegimeDetection
    from app.models.paper_trading import PaperTradeOutcome
    from app.services.regime_service import RegimeService

    logger.info("Phase 0: historical regime backfill (dry_run=%s)", dry_run)

    # ── Collect outcomes with NULL market_regime_at_entry ─────────────────────
    async with session_factory() as session:
        result = await session.execute(
            select(
                PaperTradeOutcome.id,
                PaperTradeOutcome.symbol,
                PaperTradeOutcome.created_at,
                PaperTradeOutcome.hold_duration_seconds,
            ).where(PaperTradeOutcome.market_regime_at_entry.is_(None))
        )
        rows = result.fetchall()

    if not rows:
        logger.info("Phase 0: no outcomes with NULL market_regime_at_entry — nothing to do")
        return 0, 0

    logger.info("Phase 0: %d outcomes have NULL market_regime_at_entry", len(rows))

    # ── Compute entry date (IST) per outcome ──────────────────────────────────
    # Group by (symbol, entry_ist_date) to avoid redundant OHLCV queries
    from collections import defaultdict
    groups: dict[tuple[str, date], list] = defaultdict(list)
    for row in rows:
        outcome_id, symbol, created_at, hold_sec = row
        entry_utc = created_at - timedelta(seconds=hold_sec)
        entry_ist = entry_utc.astimezone(IST)
        entry_date = entry_ist.date()
        groups[(symbol, entry_date)].append(outcome_id)

    logger.info("Phase 0: %d unique (symbol, date) pairs to resolve", len(groups))

    if dry_run:
        for (symbol, entry_date), oids in groups.items():
            logger.info(
                "DRY RUN — would compute historical regime for %s on %s (%d outcomes)",
                symbol, entry_date, len(oids),
            )
        return 0, len(rows)

    # ── Resolve instrument keys for all symbols in one query ──────────────────
    symbols = list({sym for sym, _ in groups})
    async with session_factory() as session:
        ikey_result = await session.execute(
            text("""
                SELECT trading_symbol, instrument_key
                FROM instrument_master
                WHERE trading_symbol = ANY(:syms)
                  AND exchange = 'NSE'
                  AND (instrument_type = 'EQ' OR instrument_type IS NULL)
            """),
            {"syms": symbols},
        )
        instrument_map = {row[0]: row[1] for row in ikey_result.fetchall()}

    written = 0
    skipped = 0

    for (symbol, entry_date), outcome_ids in groups.items():
        ikey = instrument_map.get(symbol)
        if ikey is None:
            logger.debug("Phase 0: %s not in instrument_master — skipping", symbol)
            skipped += len(outcome_ids)
            continue

        async with session_factory() as session:
            # Check if a detection already exists for this symbol+date in IST
            existing = await session.execute(
                text("""
                    SELECT id FROM ai_regime_detections
                    WHERE symbol = :symbol
                      AND DATE(detection_timestamp AT TIME ZONE 'Asia/Kolkata') = :entry_date
                    LIMIT 1
                """),
                {"symbol": symbol, "entry_date": entry_date},
            )
            if existing.fetchone() is not None:
                logger.debug(
                    "Phase 0: detection already exists for %s on %s — skipping write",
                    symbol, entry_date,
                )
                written += len(outcome_ids)  # outcomes will be resolved by Phase 2
                continue

            # Load OHLCV candles up to and including entry_date
            # Use end-of-day cutoff (23:59:59 IST = 18:29:59 UTC on same date)
            eod_utc = datetime(
                entry_date.year, entry_date.month, entry_date.day,
                18, 30, 0, tzinfo=timezone.utc  # 23:59 IST in UTC
            )
            ohlcv_result = await session.execute(
                text("""
                    SELECT timestamp, open::float, high::float,
                           low::float, close::float, volume
                    FROM upstox_ohlcv
                    WHERE instrument_key = :ikey
                      AND timeframe = '1D'
                      AND timestamp <= :eod
                    ORDER BY timestamp DESC
                    LIMIT 65
                """),
                {"ikey": ikey, "eod": eod_utc},
            )
            ohlcv_rows = ohlcv_result.fetchall()

            if not ohlcv_rows:
                logger.debug(
                    "Phase 0: no OHLCV data for %s up to %s — skipping",
                    symbol, entry_date,
                )
                skipped += len(outcome_ids)
                continue

            # Convert to candle dicts (oldest first)
            candles = [
                {"ts": r[0], "open": r[1], "high": r[2],
                 "low": r[3], "close": r[4], "volume": r[5]}
                for r in reversed(ohlcv_rows)
            ]

            if len(candles) < 20:
                logger.debug(
                    "Phase 0: only %d candles for %s — need ≥20 — skipping",
                    len(candles), symbol,
                )
                skipped += len(outcome_ids)
                continue

            try:
                result_data = RegimeService._compute_regime_from_candles(candles)
            except Exception as exc:
                logger.warning(
                    "Phase 0: regime computation failed for %s on %s: %s",
                    symbol, entry_date, exc,
                )
                skipped += len(outcome_ids)
                continue

            # Write synthetic detection at 16:00 IST on the entry date
            # (represents post-market regime classification for that trading day)
            detection_ts = datetime(
                entry_date.year, entry_date.month, entry_date.day,
                10, 30, 0, tzinfo=timezone.utc  # 16:00 IST in UTC
            )

            detection = AIRegimeDetection(
                symbol=symbol,
                detection_timestamp=detection_ts,
                regime_type=result_data["regime"],
                confidence_score=Decimal(str(result_data["confidence"])),
                contributing_factors={
                    "indicators": result_data["indicators"],
                    "change_pct": result_data["change_pct"],
                    "change_pct_20d": result_data["change_pct_20d"],
                    "data_points": len(candles),
                    "backfilled": True,
                    "backfill_entry_date": entry_date.isoformat(),
                },
                volatility_index=Decimal(str(result_data["indicators"]["atr_pct"])),
                volume_index=None,
                trend_strength=Decimal(str(result_data["indicators"]["adx"])),
            )
            session.add(detection)
            await session.commit()

            logger.info(
                "Phase 0: wrote historical detection for %s on %s: regime=%s confidence=%.2f",
                symbol, entry_date, result_data["regime"], result_data["confidence"],
            )
            written += len(outcome_ids)

    logger.info(
        "Phase 0 complete: %d outcomes covered by new historical detections, "
        "%d skipped (no OHLCV data or not in instrument_master)",
        written, skipped,
    )
    return written, skipped


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — Regime detection backfill
# ──────────────────────────────────────────────────────────────────────────────

async def backfill_regime_detections(
    session_factory,
    dry_run: bool = False,
) -> int:
    """
    Run a full regime detection pass for today's date.

    Returns number of new AIRegimeDetection records written (0 if dry_run).
    """
    logger.info("Phase 1: Regime detection backfill (dry_run=%s)", dry_run)

    if dry_run:
        from app.core.config import get_settings
        settings = get_settings()
        logger.info(
            "DRY RUN — would process %d symbols",
            len(settings.SIGNAL_SCHEDULED_UNIVERSE),
        )
        return 0

    from app.ai.strategy.regime_detector import _run_regime_detection

    today = date.today()
    success = await _run_regime_detection(session_factory, run_date=today)

    if success:
        logger.info("Phase 1 complete — regime detections written for %s", today)
    else:
        logger.error("Phase 1 FAILED — regime detection encountered an error")

    # Query how many we actually wrote today
    from sqlalchemy import func, text
    async with session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT COUNT(*)
                FROM ai_regime_detections
                WHERE DATE(detection_timestamp) = :today
                """
            ),
            {"today": today},
        )
        count = result.scalar_one() or 0

    logger.info("Phase 1 result: %d regime detections in ai_regime_detections for %s", count, today)
    return count


# ──────────────────────────────────────────────────────────────────────────────
# Phase 2 — market_regime_at_entry backfill
# ──────────────────────────────────────────────────────────────────────────────

async def backfill_regime_at_entry(
    session_factory,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Populate market_regime_at_entry for outcomes where it is NULL.

    Returns (updated_count, skipped_count).
    """
    from app.models.paper_trading import PaperTradeOutcome
    from app.services.paper_trading.outcome_service import _get_regime_at_entry

    logger.info("Phase 2: market_regime_at_entry backfill (dry_run=%s)", dry_run)

    # Fetch all NULL outcomes (no limit — one-time migration)
    async with session_factory() as session:
        result = await session.execute(
            select(PaperTradeOutcome).where(
                PaperTradeOutcome.market_regime_at_entry.is_(None)
            )
        )
        outcomes: list[PaperTradeOutcome] = list(result.scalars().all())

    logger.info(
        "Phase 2: found %d outcomes with NULL market_regime_at_entry", len(outcomes)
    )

    if not outcomes:
        return 0, 0

    if dry_run:
        logger.info("DRY RUN — would attempt to fill %d outcomes", len(outcomes))
        return 0, len(outcomes)

    updated = 0
    skipped = 0

    for i in range(0, len(outcomes), BATCH_SIZE):
        batch = outcomes[i : i + BATCH_SIZE]

        async with session_factory() as session:
            for outcome in batch:
                # Re-attach to this session
                merged = await session.merge(outcome)
                regime = await _get_regime_at_entry(session, merged)
                if regime:
                    merged.market_regime_at_entry = regime
                    updated += 1
                else:
                    skipped += 1

            await session.commit()

        logger.info(
            "Phase 2: batch %d/%d processed (updated=%d, skipped=%d so far)",
            min(i + BATCH_SIZE, len(outcomes)), len(outcomes), updated, skipped,
        )

    logger.info(
        "Phase 2 complete: %d outcomes updated, %d had no matching regime detection",
        updated, skipped,
    )
    return updated, skipped


# ──────────────────────────────────────────────────────────────────────────────
# Phase 3 — ML feedback fields backfill
# ──────────────────────────────────────────────────────────────────────────────

async def backfill_ml_feedback(
    session_factory,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    Compute ML feedback for outcomes where ml_direction_correct IS NULL.

    Returns (updated_count, failed_count).
    """
    from app.models.paper_trading import PaperTradeOutcome
    from app.services.paper_trading.outcome_service import compute_ml_feedback

    logger.info("Phase 3: ML feedback backfill (dry_run=%s)", dry_run)

    async with session_factory() as session:
        result = await session.execute(
            select(PaperTradeOutcome.id, PaperTradeOutcome.symbol).where(
                PaperTradeOutcome.ml_direction_correct.is_(None)
            )
        )
        pending: list[tuple[UUID, str]] = list(result.fetchall())

    logger.info("Phase 3: found %d outcomes pending ML feedback", len(pending))

    if not pending:
        return 0, 0

    if dry_run:
        logger.info("DRY RUN — would compute ML feedback for %d outcomes", len(pending))
        return 0, len(pending)

    updated = 0
    failed = 0

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i : i + BATCH_SIZE]

        async with session_factory() as session:
            for outcome_id, symbol in batch:
                try:
                    await compute_ml_feedback(session, outcome_id)
                    updated += 1
                except Exception as exc:
                    logger.warning(
                        "Phase 3: compute_ml_feedback failed for outcome=%s symbol=%s: %s",
                        outcome_id, symbol, exc,
                    )
                    failed += 1
            await session.commit()

        logger.info(
            "Phase 3: batch %d/%d processed (updated=%d, failed=%d so far)",
            min(i + BATCH_SIZE, len(pending)), len(pending), updated, failed,
        )

    logger.info(
        "Phase 3 complete: %d outcomes updated, %d failed",
        updated, failed,
    )
    return updated, failed


# ──────────────────────────────────────────────────────────────────────────────
# Phase 4 — Signal confidence backfill for manually-opened outcomes
# ──────────────────────────────────────────────────────────────────────────────

async def backfill_signal_confidence(
    session_factory,
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    For outcomes where suggestion_id IS NULL and suggestion_confidence_level IS NULL,
    look up a matching AITradingSignal and populate confidence level + price targets.

    Returns (updated_count, skipped_count).
    """
    from datetime import timedelta

    from app.models.paper_trading import PaperTradeOutcome
    from app.services.paper_trading.outcome_service import (
        _derive_confidence_level,
        _find_matching_ai_signal,
    )

    logger.info("Phase 4: signal confidence backfill (dry_run=%s)", dry_run)

    async with session_factory() as session:
        result = await session.execute(
            select(PaperTradeOutcome).where(
                PaperTradeOutcome.suggestion_id.is_(None),
                PaperTradeOutcome.suggestion_confidence_level.is_(None),
            )
        )
        outcomes: list[PaperTradeOutcome] = list(result.scalars().all())

    logger.info(
        "Phase 4: found %d outcomes with no linked suggestion and no confidence level",
        len(outcomes),
    )

    if not outcomes:
        return 0, 0

    if dry_run:
        logger.info("DRY RUN — would attempt to match %d outcomes to AITradingSignals", len(outcomes))
        return 0, len(outcomes)

    updated = 0
    skipped = 0

    for i in range(0, len(outcomes), BATCH_SIZE):
        batch = outcomes[i : i + BATCH_SIZE]

        async with session_factory() as session:
            for outcome in batch:
                merged = await session.merge(outcome)

                opened_at = merged.created_at - timedelta(seconds=merged.hold_duration_seconds)
                closed_at = merged.created_at

                signal = await _find_matching_ai_signal(
                    session,
                    symbol=merged.symbol,
                    direction=merged.signal_direction,
                    opened_at=opened_at,
                    closed_at=closed_at,
                )

                if signal is None:
                    logger.debug(
                        "Phase 4: no matching signal for %s (%s) — skipping",
                        merged.symbol, merged.signal_direction,
                    )
                    skipped += 1
                    continue

                merged.suggestion_confidence_level = _derive_confidence_level(signal.confidence_score)
                merged.suggestion_consensus_score = (
                    signal.confidence_score * Decimal("100")
                ).quantize(Decimal("0.01"))

                # Fill in price targets from signal if still missing
                if merged.suggested_stop_loss is None and signal.stop_loss is not None:
                    merged.suggested_stop_loss = signal.stop_loss
                if merged.suggested_tp1 is None and signal.target_price is not None:
                    merged.suggested_tp1 = signal.target_price

                logger.info(
                    "Phase 4: %s matched to signal id=%s confidence=%s score=%.2f",
                    merged.symbol, signal.id,
                    merged.suggestion_confidence_level, float(signal.confidence_score),
                )
                updated += 1

            await session.commit()

        logger.info(
            "Phase 4: batch %d/%d processed (updated=%d, skipped=%d so far)",
            min(i + BATCH_SIZE, len(outcomes)), len(outcomes), updated, skipped,
        )

    logger.info(
        "Phase 4 complete: %d outcomes matched to AITradingSignals, %d had no match",
        updated, skipped,
    )
    return updated, skipped


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

async def run_backfill(phases: list[int], dry_run: bool) -> int:
    """
    Run the specified phases and return exit code:
      0 — all phases completed successfully
      1 — one or more phases had partial failures (some records skipped)
      2 — one or more phases failed completely
    """
    import sys
    sys.path.insert(0, ".")  # ensure app is importable when run as __main__

    from app.core.database import WorkerSessionLocal

    exit_code = 0
    start = datetime.now(timezone.utc)

    logger.info(
        "=== ML Feedback & Regime Backfill START === phases=%s dry_run=%s ===",
        phases, dry_run,
    )

    if 0 in phases:
        p0_written, p0_skipped = await backfill_historical_regimes(WorkerSessionLocal, dry_run)
        if p0_skipped > 0 and p0_written == 0 and not dry_run:
            logger.warning(
                "Phase 0: all %d outcomes skipped — no OHLCV data available for their entry dates",
                p0_skipped,
            )
            exit_code = max(exit_code, 1)

    if 1 in phases:
        regime_count = await backfill_regime_detections(WorkerSessionLocal, dry_run)
        if regime_count == 0 and not dry_run:
            logger.warning("Phase 1: No regime detections written — check OHLCV data coverage")
            exit_code = max(exit_code, 1)

    if 2 in phases:
        p2_updated, p2_skipped = await backfill_regime_at_entry(WorkerSessionLocal, dry_run)
        if p2_skipped > 0:
            logger.info(
                "Phase 2: %d outcomes still have no regime match — "
                "they may predate the first detection run",
                p2_skipped,
            )

    if 3 in phases:
        p3_updated, p3_failed = await backfill_ml_feedback(WorkerSessionLocal, dry_run)
        if p3_failed > 0:
            logger.warning("Phase 3: %d outcomes could not be processed", p3_failed)
            exit_code = max(exit_code, 1)

    if 4 in phases:
        p4_updated, p4_skipped = await backfill_signal_confidence(WorkerSessionLocal, dry_run)
        if p4_skipped > 0:
            logger.info(
                "Phase 4: %d outcomes had no matching AITradingSignal in the ±2-day window",
                p4_skipped,
            )

    duration = (datetime.now(timezone.utc) - start).total_seconds()
    logger.info(
        "=== ML Feedback & Regime Backfill COMPLETE === duration=%.1fs exit_code=%d ===",
        duration, exit_code,
    )
    return exit_code


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ML Feedback & Regime Detection Backfill",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[0, 1, 2, 3, 4],
        default=None,
        help="Run only a specific phase (default: run all phases 0–4)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Report what would be done without writing any changes",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    phases = [args.phase] if args.phase is not None else [0, 1, 2, 3, 4]

    exit_code = asyncio.run(run_backfill(phases=phases, dry_run=args.dry_run))
    sys.exit(exit_code)
