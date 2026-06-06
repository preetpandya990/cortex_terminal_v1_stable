#!/usr/bin/env python3
"""
Backfill: Event Symbol Classification + Trading Signal Event Contributions
==========================================================================
Corrects two related data-quality gaps introduced by the pre-fix event
pipeline, where spaCy NER returned empty entity lists for Indian financial
content and the LLM output was discarded on confidence < 0.5:

  Phase 1 — Fix affected_symbols on ai_event_classifications
    301 records after 2026-05-30 have empty affected_symbols.  For each,
    the original raw event content is re-processed using the deterministic
    content-level extraction logic (ALL-CAPS ticker scan + corporate-suffix
    company-name regex → normalize_and_validate_symbols DB lookup).
    No LLM calls.  Only records with empty/null affected_symbols are touched.

  Phase 2 — Recompute event contributions on ai_trading_signals
    3,103 signals after 2026-05-30 have contributing_events = [].  For each,
    the event signal is queried using gather_event_signals with reference_time
    set to the signal's original signal_timestamp (so decay is computed
    relative to when the signal was generated, not now).  If events are found,
    the fused confidence_score, action, and reasoning are recomputed using the
    stored ml_predictions and technical_indicators JSONB columns — no new
    inference.  Only signals that gain event coverage are written.

Safety
------
  • Idempotent: Phase 1 only updates rows WHERE affected_symbols = '{}'.
                Phase 2 only updates rows WHERE contributing_events = '[]'.
  • Dry-run mode (--dry-run): prints every proposed change, commits nothing.
  • Batched: DB writes happen in configurable batches (default 50) to avoid
    long-running transactions and lock contention.
  • Progress: running totals printed after every batch.
  • Atomic per batch: each batch is committed independently; a failure in one
    batch does not roll back earlier batches.

Usage
-----
  # Preview all changes (no writes):
  python scripts/backfill_event_symbols_and_signals.py --dry-run

  # Execute from 2026-05-30 (default):
  python scripts/backfill_event_symbols_and_signals.py

  # Execute from a custom date:
  python scripts/backfill_event_symbols_and_signals.py --since 2026-05-28

  # Tune batch size:
  python scripts/backfill_event_symbols_and_signals.py --batch-size 25

  # Run only one phase:
  python scripts/backfill_event_symbols_and_signals.py --phase 1
  python scripts/backfill_event_symbols_and_signals.py --phase 2
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import re
import sys
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

# ── Path setup (run from backend/ or project root) ────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import numpy as np
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.ai.fusion.models import (
    AIEventClassification,
    AINLPResult,
    AIProcessedEvent,
    AIRawEvent,
    AITradingSignal,
)
from app.ai.intelligence.event_classifier import (
    _ALLCAPS_RE,
    _CORP_NAME_RE,
    _NON_TICKER_WORDS,
    normalize_and_validate_symbols,
)
from app.ai.fusion.signal_assembler import SignalAssembler
from app.models.upstox_data import InstrumentMaster

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill")

# ── Constants ──────────────────────────────────────────────────────────────────

# Standard signal assembly weights (must match SignalAssembler defaults)
_EVENT_WEIGHT = 0.35
_ML_WEIGHT    = 0.40
_TECH_WEIGHT  = 0.25

# Lookback window used by gather_event_signals
_EVENT_LOOKBACK_HOURS = 48


# ── Stats tracking ─────────────────────────────────────────────────────────────

@dataclass
class Phase1Stats:
    examined: int = 0
    fixed:    int = 0
    skipped:  int = 0    # no symbols found in content
    errors:   int = 0
    symbols_added: list[str] = field(default_factory=list)


@dataclass
class Phase2Stats:
    examined:  int = 0
    updated:   int = 0
    no_events: int = 0
    errors:    int = 0
    action_changes: int = 0


# ── Content-level symbol extraction (mirrors event_classifier logic) ───────────

def _extract_symbol_candidates(content: str) -> list[str]:
    """
    Extract NSE ticker candidates from raw event content.
    Mirrors EventClassifier._extract_symbols_from_content() without DB I/O.
    Returns raw (unvalidated) candidate strings for normalize_and_validate_symbols.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(token: str) -> None:
        token = token.strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)

    # Pass 1: ALL-CAPS tokens (strip trailing punctuation that the regex absorbs)
    for m in _ALLCAPS_RE.finditer(content):
        token = m.group(1).rstrip("-&")
        if len(token) >= 3 and token not in _NON_TICKER_WORDS:
            _add(token)

    # Pass 2: Title-Case company names with Indian corporate suffixes
    for m in _CORP_NAME_RE.finditer(content):
        _add(m.group(1).strip())

    return out


# ── Fusion helpers (mirrors signal_assembler.fuse_signals) ─────────────────────

def _renormalize_weights(
    sources: dict[str, tuple[float, bool]],
) -> dict[str, float]:
    denom = sum(w for w, avail in sources.values() if avail)
    if denom == 0.0:
        return {k: w for k, (w, _) in sources.items()}
    return {k: (w / denom if avail else 0.0) for k, (w, avail) in sources.items()}


def _fuse(
    event_signals: dict[str, Any],
    ml_signals: dict[str, Any],
    technical_signals: dict[str, Any],
) -> dict[str, Any]:
    """
    Weighted-average fusion — exact replica of SignalAssembler.fuse_signals().
    Called with stored JSONB dicts so no inference is needed.
    """
    sources: dict[str, tuple[float, bool]] = {
        "event":     (_EVENT_WEIGHT, bool(event_signals.get("available", True))),
        "ml":        (_ML_WEIGHT,    bool(ml_signals.get("available", True))),
        "technical": (_TECH_WEIGHT,  bool(technical_signals.get("available", True))),
    }
    weights = _renormalize_weights(sources)

    fused_score = (
        weights["event"]     * event_signals.get("score", 0.0)
        + weights["ml"]      * ml_signals.get("score", 0.0)
        + weights["technical"] * technical_signals.get("score", 0.0)
    )
    fused_confidence = (
        weights["event"]     * event_signals.get("confidence", 0.0)
        + weights["ml"]      * ml_signals.get("confidence", 0.0)
        + weights["technical"] * technical_signals.get("confidence", 0.0)
    )

    n_available = sum(1 for _, avail in sources.values() if avail)
    if n_available == 1:
        fused_confidence = min(fused_confidence, 0.85)

    if fused_score > 50:
        action = "BUY"
    elif fused_score < -50:
        action = "SELL"
    else:
        action = "HOLD"

    return {
        "action":             action,
        "confidence_score":   fused_confidence,
        "fused_score":        fused_score,
        "contributing_events": event_signals.get("events", []),
    }


def _build_reasoning(
    action: str,
    confidence: float,
    time_horizon: str,
    event_signals: dict[str, Any],
    ml_signals: dict[str, Any],
    regime: str | None,
    target_price: float | None,
    stop_loss: float | None,
) -> str:
    """Mirrors SignalAssembler._build_reasoning()."""
    _CONFIDENCE_BAND = [(0.80, "high"), (0.60, "medium"), (0.0, "low")]
    band = next(label for threshold, label in _CONFIDENCE_BAND if confidence >= threshold)
    dominant = "ML ensemble" if ml_signals.get("score", 0.0) != 0.0 else "event analysis"
    ml_conf_pct = f"{ml_signals.get('confidence', 0.0) * 100:.0f}%"
    event_count = event_signals.get("event_count", 0)
    direction_str = action.capitalize()
    horizon_str = time_horizon.capitalize()

    parts = [
        f"{horizon_str} {direction_str} ({band} confidence · {confidence * 100:.1f}%):",
        f"{dominant} projects "
        f"{'bullish' if action == 'BUY' else 'bearish' if action == 'SELL' else 'neutral'} "
        f"momentum with {ml_conf_pct} confidence.",
    ]
    if event_count > 0:
        parts.append(
            f"{event_count} recent market event{'s' if event_count > 1 else ''} reinforce the setup."
        )
    if regime and regime != "unknown":
        parts.append(f"Market regime: {regime.replace('_', ' ')}.")
    if stop_loss:
        parts.append(f"Stop loss: ₹{stop_loss:,.2f}.")
    if target_price:
        parts.append(f"Target: ₹{target_price:,.2f}.")

    return " ".join(parts)


# ── Phase 1 ────────────────────────────────────────────────────────────────────

async def phase1_fix_event_symbols(
    session_factory: async_sessionmaker,
    since: datetime,
    dry_run: bool,
    batch_size: int,
) -> Phase1Stats:
    """
    Re-process event classifications with empty affected_symbols.

    For each record, extracts symbol candidates from the raw event content
    using the deterministic content-level extraction (ALL-CAPS + corporate-
    suffix regex → instrument_master lookup).  If candidates resolve to valid
    NSE symbols, the affected_symbols array is updated in place.
    """
    stats = Phase1Stats()

    # Fetch all eligible record IDs upfront to avoid cursor-timeout issues on
    # large tables; process them in Python-controlled batches.
    async with session_factory() as db:
        id_rows = await db.execute(
            select(AIEventClassification.id)
            .where(
                AIEventClassification.created_at >= since,
                or_(
                    AIEventClassification.affected_symbols.is_(None),
                    AIEventClassification.affected_symbols == [],
                ),
            )
            .order_by(AIEventClassification.id)
        )
        all_ids = [r[0] for r in id_rows.all()]

    total = len(all_ids)
    logger.info("Phase 1: %d event classifications to process", total)

    for batch_start in range(0, total, batch_size):
        batch_ids = all_ids[batch_start : batch_start + batch_size]

        async with session_factory() as db:
            try:
                # Fetch full records + raw content for this batch
                rows = await db.execute(
                    select(
                        AIEventClassification.id,
                        AIRawEvent.raw_content,
                    )
                    .outerjoin(AINLPResult, AINLPResult.id == AIEventClassification.nlp_result_id)
                    .outerjoin(AIProcessedEvent, AIProcessedEvent.id == AINLPResult.processed_event_id)
                    .outerjoin(AIRawEvent, AIRawEvent.id == AIProcessedEvent.raw_event_id)
                    .where(AIEventClassification.id.in_(batch_ids))
                )
                records = rows.all()

                updates: list[tuple[int, list[str]]] = []

                for rec in records:
                    stats.examined += 1
                    content = rec.raw_content or ""
                    if not content.strip():
                        stats.skipped += 1
                        continue

                    try:
                        candidates = _extract_symbol_candidates(content)
                        if not candidates:
                            stats.skipped += 1
                            continue

                        validated = await normalize_and_validate_symbols(db, candidates)
                        if not validated:
                            stats.skipped += 1
                            continue

                        updates.append((rec.id, validated))
                        stats.symbols_added.extend(validated)

                    except Exception as exc:
                        logger.warning("  classification id=%d extraction error: %s", rec.id, exc)
                        stats.errors += 1

                if dry_run:
                    for cls_id, syms in updates:
                        logger.info(
                            "  [DRY-RUN] classification id=%-6d → symbols=%s",
                            cls_id, syms,
                        )
                    stats.fixed += len(updates)
                else:
                    for cls_id, syms in updates:
                        await db.execute(
                            update(AIEventClassification)
                            .where(AIEventClassification.id == cls_id)
                            .values(affected_symbols=syms)
                        )
                    await db.commit()
                    stats.fixed += len(updates)

            except Exception as exc:
                logger.error("Phase 1 batch %d–%d failed: %s", batch_start, batch_start + batch_size, exc)
                await db.rollback()
                stats.errors += batch_size

        pct = min(batch_start + batch_size, total)
        logger.info(
            "  Phase 1 progress: %d/%d  fixed=%d  skipped=%d  errors=%d",
            pct, total, stats.fixed, stats.skipped, stats.errors,
        )

    return stats


# ── Phase 2 ────────────────────────────────────────────────────────────────────

async def phase2_fix_signal_contributions(
    session_factory: async_sessionmaker,
    since: datetime,
    dry_run: bool,
    batch_size: int,
) -> Phase2Stats:
    """
    Recompute event contributions for trading signals with empty contributing_events.

    For each signal, gather_event_signals is called with reference_time set to
    the signal's original signal_timestamp so temporal decay is computed at the
    moment the signal was generated.  Stored ml_predictions and
    technical_indicators JSONB are reused verbatim — no new ML inference.

    Only signals that gain at least one event are written; signals with no
    associated events are left unchanged.
    """
    stats = Phase2Stats()
    assembler = SignalAssembler()  # default weights: event=0.35 ml=0.40 technical=0.25

    # Fetch eligible signal IDs upfront
    async with session_factory() as db:
        id_rows = await db.execute(
            select(AITradingSignal.id)
            .where(
                AITradingSignal.created_at >= since,
                or_(
                    AITradingSignal.contributing_events.is_(None),
                    AITradingSignal.contributing_events == [],
                ),
            )
            .order_by(AITradingSignal.id)
        )
        all_ids = [r[0] for r in id_rows.all()]

    total = len(all_ids)
    logger.info("Phase 2: %d signals to process", total)

    for batch_start in range(0, total, batch_size):
        batch_ids = all_ids[batch_start : batch_start + batch_size]

        async with session_factory() as db:
            try:
                rows = await db.execute(
                    select(
                        AITradingSignal.id,
                        AITradingSignal.symbol,
                        AITradingSignal.action,
                        AITradingSignal.signal_timestamp,
                        AITradingSignal.time_horizon,
                        AITradingSignal.regime_type,
                        AITradingSignal.target_price,
                        AITradingSignal.stop_loss,
                        AITradingSignal.ml_predictions,
                        AITradingSignal.technical_indicators,
                    )
                    .where(AITradingSignal.id.in_(batch_ids))
                )
                signals = rows.all()

                updates: list[dict[str, Any]] = []

                for sig in signals:
                    stats.examined += 1
                    try:
                        # Query events relative to signal generation time
                        event_signals = await assembler.gather_event_signals(
                            db=db,
                            symbol=sig.symbol,
                            lookback_hours=_EVENT_LOOKBACK_HOURS,
                            reference_time=sig.signal_timestamp,
                        )

                        if not event_signals.get("available"):
                            stats.no_events += 1
                            continue

                        # Reuse stored JSONB — no new inference needed
                        ml_signals: dict[str, Any] = dict(sig.ml_predictions or {})
                        tech_signals: dict[str, Any] = dict(sig.technical_indicators or {})

                        fused = _fuse(event_signals, ml_signals, tech_signals)

                        target = float(sig.target_price) if sig.target_price else None
                        sl = float(sig.stop_loss) if sig.stop_loss else None
                        reasoning = _build_reasoning(
                            action=fused["action"],
                            confidence=fused["confidence_score"],
                            time_horizon=sig.time_horizon or "swing",
                            event_signals=event_signals,
                            ml_signals=ml_signals,
                            regime=sig.regime_type,
                            target_price=target,
                            stop_loss=sl,
                        )

                        action_changed = fused["action"] != sig.action
                        if action_changed:
                            stats.action_changes += 1

                        updates.append({
                            "id":                  sig.id,
                            "contributing_events": fused["contributing_events"],
                            "confidence_score":    round(fused["confidence_score"], 2),
                            "action":              fused["action"],
                            "reasoning":           reasoning,
                            "old_action":          sig.action,
                            "old_confidence":      None,  # not stored on signal row
                            "event_count":         event_signals.get("event_count", 0),
                        })

                    except Exception as exc:
                        logger.warning("  signal id=%d error: %s", sig.id, exc)
                        stats.errors += 1

                if dry_run:
                    for u in updates:
                        action_note = (
                            f"  ACTION: {u['old_action']}→{u['action']}"
                            if u["old_action"] != u["action"] else ""
                        )
                        logger.info(
                            "  [DRY-RUN] signal id=%-7d  conf→%.3f  events=%d%s",
                            u["id"], u["confidence_score"], u["event_count"], action_note,
                        )
                    stats.updated += len(updates)
                else:
                    for u in updates:
                        await db.execute(
                            update(AITradingSignal)
                            .where(AITradingSignal.id == u["id"])
                            .values(
                                contributing_events=u["contributing_events"],
                                confidence_score=Decimal(str(u["confidence_score"])),
                                action=u["action"],
                                reasoning=u["reasoning"],
                            )
                        )
                    await db.commit()
                    stats.updated += len(updates)

            except Exception as exc:
                logger.error("Phase 2 batch %d–%d failed: %s", batch_start, batch_start + batch_size, exc)
                await db.rollback()
                stats.errors += batch_size

        pct = min(batch_start + batch_size, total)
        logger.info(
            "  Phase 2 progress: %d/%d  updated=%d  no_events=%d  action_changes=%d  errors=%d",
            pct, total, stats.updated, stats.no_events, stats.action_changes, stats.errors,
        )

    return stats


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(
    since: datetime,
    dry_run: bool,
    batch_size: int,
    phase: int | None,
) -> None:
    settings = get_settings()

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=2,
        pool_pre_ping=True,
        connect_args={"server_settings": {"timezone": "UTC"}},
    )
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    mode = "[DRY-RUN]" if dry_run else "[LIVE]"
    logger.info("=" * 65)
    logger.info("Backfill: Event Symbols + Signal Contributions  %s", mode)
    logger.info("Since:      %s", since.date())
    logger.info("Batch size: %d", batch_size)
    logger.info("=" * 65)

    p1_stats: Phase1Stats | None = None
    p2_stats: Phase2Stats | None = None

    try:
        if phase is None or phase == 1:
            logger.info("")
            logger.info("── Phase 1: Fix event classification affected_symbols ──────")
            p1_stats = await phase1_fix_event_symbols(session_factory, since, dry_run, batch_size)
            logger.info("")
            logger.info("Phase 1 complete:")
            logger.info("  Examined : %d", p1_stats.examined)
            logger.info("  Fixed    : %d", p1_stats.fixed)
            logger.info("  Skipped  : %d  (no symbols found in content)", p1_stats.skipped)
            logger.info("  Errors   : %d", p1_stats.errors)
            unique_syms = sorted(set(p1_stats.symbols_added))
            if unique_syms:
                logger.info("  Symbols added: %s", ", ".join(unique_syms[:30]))
                if len(unique_syms) > 30:
                    logger.info("  ... and %d more", len(unique_syms) - 30)

        if phase is None or phase == 2:
            logger.info("")
            logger.info("── Phase 2: Recompute signal event contributions ───────────")
            p2_stats = await phase2_fix_signal_contributions(session_factory, since, dry_run, batch_size)
            logger.info("")
            logger.info("Phase 2 complete:")
            logger.info("  Examined       : %d", p2_stats.examined)
            logger.info("  Updated        : %d", p2_stats.updated)
            logger.info("  No events      : %d  (no change needed)", p2_stats.no_events)
            logger.info("  Action changes : %d", p2_stats.action_changes)
            logger.info("  Errors         : %d", p2_stats.errors)

        logger.info("")
        logger.info("=" * 65)
        if dry_run:
            logger.info("DRY-RUN complete — no data was written.")
            logger.info("Re-run without --dry-run to apply changes.")
        else:
            total_fixed = (p1_stats.fixed if p1_stats else 0)
            total_updated = (p2_stats.updated if p2_stats else 0)
            logger.info("Backfill complete: %d classifications fixed, %d signals updated.",
                        total_fixed, total_updated)
        logger.info("=" * 65)

    finally:
        await engine.dispose()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill event symbols and trading signal contributions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to the database.",
    )
    parser.add_argument(
        "--since",
        default="2026-05-30",
        metavar="YYYY-MM-DD",
        help="Process records created on or after this date (default: 2026-05-30).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        metavar="N",
        help="Records per DB transaction batch (default: 50).",
    )
    parser.add_argument(
        "--phase",
        type=int,
        choices=[1, 2],
        default=None,
        help="Run only Phase 1 or Phase 2 (default: run both).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    asyncio.run(
        main(
            since=since_dt,
            dry_run=args.dry_run,
            batch_size=args.batch_size,
            phase=args.phase,
        )
    )
