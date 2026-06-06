#!/usr/bin/env python3
"""
Backfill: RAG Document Embeddings
==================================
Embeds all ai_raw_events from the last N days into ai_document_embeddings
using NVIDIA NIM (nv-embedqa-e5-v5, 1024-dim).

Requires:
  - NVIDIA_NIM_API_KEY set in backend/.env
  - pgvector extension + ai_document_embeddings table (migration 0041)

Safety:
  - Idempotent: ON CONFLICT DO NOTHING, safe to re-run
  - Dry-run: --dry-run prints counts without making any NIM or DB calls
  - Rate-limit aware: --batch-size controls NIM requests per call (default 32)
  - Progress: prints running totals after every cycle

Usage:
  cd backend/
  source .venv/bin/activate
  python scripts/backfill_rag_embeddings.py
  python scripts/backfill_rag_embeddings.py --window-days 60
  python scripts/backfill_rag_embeddings.py --dry-run
  python scripts/backfill_rag_embeddings.py --batch-size 16   # slower, kinder to rate limit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

# Ensure backend/ is on sys.path when run from the scripts/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.intelligence.llm_client import CortexIntelligenceClient
from app.ai.rag.ingester import _fetch_unembedded_events, ingest_batch
from app.core.config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_rag")


async def _count_pending(session: AsyncSession, cutoff) -> int:
    from datetime import datetime, timezone
    from sqlalchemy import and_, func
    from app.ai.fusion.models import AIRawEvent, AIDocumentEmbedding
    from sqlalchemy import select

    stmt = (
        select(func.count())
        .select_from(AIRawEvent)
        .outerjoin(
            AIDocumentEmbedding,
            and_(
                AIDocumentEmbedding.source_table == "ai_raw_events",
                AIDocumentEmbedding.source_id == AIRawEvent.id,
            ),
        )
        .where(
            AIDocumentEmbedding.id.is_(None),
            AIRawEvent.event_timestamp >= cutoff,
        )
    )
    result = await session.execute(stmt)
    return result.scalar() or 0


async def run(window_days: int, batch_size: int, dry_run: bool) -> None:
    from datetime import datetime, timedelta, timezone

    settings = get_settings()

    if not settings.NVIDIA_NIM_API_KEY:
        logger.error("NVIDIA_NIM_API_KEY is not set — cannot embed. Aborting.")
        sys.exit(1)

    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=5,
        max_overflow=0,
        pool_timeout=30,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    logger.info("window_days=%d  cutoff=%s  batch_size=%d  dry_run=%s",
                window_days, cutoff.date().isoformat(), batch_size, dry_run)

    async with session_factory() as session:
        pending = await _count_pending(session, cutoff)

    logger.info("Events to embed: %d", pending)

    if dry_run:
        logger.info("DRY RUN — no NIM calls or DB writes.")
        await engine.dispose()
        return

    if pending == 0:
        logger.info("Nothing to do.")
        await engine.dispose()
        return

    # Initialize the LLM client (needed for embed calls)
    await CortexIntelligenceClient.initialize()

    total_inserted = 0
    total_skipped = 0
    cycle = 0
    start = time.monotonic()

    while True:
        cycle += 1
        async with session_factory() as session:
            events = await _fetch_unembedded_events(session, cutoff, limit=500)

        if not events:
            break

        logger.info("Cycle %d — embedding %d events...", cycle, len(events))
        cycle_start = time.monotonic()

        async with session_factory() as session:
            inserted = await ingest_batch(session, events, batch_size=batch_size)

        skipped = len(events) - inserted
        total_inserted += inserted
        total_skipped += skipped
        elapsed = time.monotonic() - cycle_start

        logger.info(
            "Cycle %d done — inserted=%d skipped=%d  (%.1fs)  "
            "running total: inserted=%d skipped=%d",
            cycle, inserted, skipped, elapsed, total_inserted, total_skipped,
        )

    total_elapsed = time.monotonic() - start
    logger.info(
        "Backfill complete — cycles=%d  inserted=%d  skipped=%d  total_time=%.1fs",
        cycle, total_inserted, total_skipped, total_elapsed,
    )

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill RAG embeddings for ai_raw_events.")
    parser.add_argument("--window-days", type=int, default=30,
                        help="How many days back to embed (default: 30)")
    parser.add_argument("--batch-size", type=int, default=32,
                        help="NIM embedding batch size per request (default: 32)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print counts only, no NIM calls or DB writes")
    args = parser.parse_args()

    asyncio.run(run(args.window_days, args.batch_size, args.dry_run))


if __name__ == "__main__":
    main()
