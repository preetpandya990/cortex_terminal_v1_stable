"""
Cortex AI — Company Fundamentals Backfill Script
=================================================
Fetches and persists Upstox v2 fundamentals data for all NSE EQ instruments.

Usage
-----
    # Full run (or auto-resume if checkpoint exists):
    python backfill_fundamentals.py

    # Force fresh run, ignoring existing checkpoint:
    python backfill_fundamentals.py --fresh

Behaviour
---------
  - Queries all instrument_type='EQ' rows from instrument_master (~2,000–2,500)
  - asyncio.Semaphore(40) caps concurrent in-flight requests to stay safely
    under the Upstox 50 req/s rate limit (8 endpoints × 40 = 320 calls/burst,
    spread across gather() which naturally throttles to ~40 concurrent)
  - Checkpoint file at .fundamentals_backfill_checkpoint stores the last
    successfully processed instrument_key; restarting resumes from there
  - Per-instrument errors are logged and skipped — they never abort the run
  - Final summary reports success count, skip count, and error count

Expected runtime: ~2,500 instruments × 8 endpoints / 40 concurrency ≈ 8–12 min
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Path setup ─────────────────────────────────────────────────────────────────
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_ROOT))

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import get_settings
from app.models.upstox_data import InstrumentMaster
from app.services.fundamentals_service import fetch_and_store_all, extract_isin

# ── Config ─────────────────────────────────────────────────────────────────────
settings = get_settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backfill_fundamentals")

_CHECKPOINT_FILE = Path(__file__).parent / ".fundamentals_backfill_checkpoint"
# Instrument-level in-flight: 5 instruments process concurrently.
# The global rate limiter in fundamentals_service enforces 1 req/s total
# (1800 req/30min), the binding Upstox constraint. Higher concurrency only
# means more coroutines queued at the lock — no benefit beyond 5.
_CONCURRENCY = 5
_LOG_EVERY   = 50  # print progress every N instruments


# ── DB setup ───────────────────────────────────────────────────────────────────

def _make_engine():
    return create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )


# ── Checkpoint helpers ─────────────────────────────────────────────────────────

def _load_checkpoint() -> str | None:
    if _CHECKPOINT_FILE.exists():
        val = _CHECKPOINT_FILE.read_text().strip()
        return val or None
    return None


def _save_checkpoint(instrument_key: str) -> None:
    _CHECKPOINT_FILE.write_text(instrument_key)


def _clear_checkpoint() -> None:
    if _CHECKPOINT_FILE.exists():
        _CHECKPOINT_FILE.unlink()


# ── Worker ─────────────────────────────────────────────────────────────────────

async def _process_one(
    instrument_key: str,
    semaphore: asyncio.Semaphore,
    session_factory: async_sessionmaker,
) -> str:
    """
    Fetch and persist fundamentals for a single instrument.
    Returns 'ok' | 'skip' | 'error'.
    """
    async with semaphore:
        try:
            isin = extract_isin(instrument_key)
        except ValueError as exc:
            logger.warning("Cannot extract ISIN from %s: %s", instrument_key, exc)
            return "skip"

        try:
            async with session_factory() as db:
                await fetch_and_store_all(instrument_key, isin, db)
            return "ok"
        except Exception as exc:
            logger.error("Failed to fetch fundamentals for %s: %s", instrument_key, exc)
            return "error"


# ── Main ───────────────────────────────────────────────────────────────────────

async def main(fresh: bool) -> None:
    if fresh:
        _clear_checkpoint()
        logger.info("Fresh run — checkpoint cleared")

    engine         = _make_engine()
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Load all EQ instruments
    async with session_factory() as db:
        result = await db.execute(
            select(InstrumentMaster.instrument_key)
            .where(InstrumentMaster.instrument_type == "EQ")
            .order_by(InstrumentMaster.instrument_key)
        )
        all_keys: list[str] = [row[0] for row in result.all()]

    total = len(all_keys)
    logger.info("Found %d EQ instruments", total)

    # Resume from checkpoint
    checkpoint = _load_checkpoint()
    start_idx  = 0
    if checkpoint and not fresh:
        try:
            start_idx = all_keys.index(checkpoint) + 1
            logger.info("Resuming from checkpoint: %s (index %d / %d)", checkpoint, start_idx, total)
        except ValueError:
            logger.warning("Checkpoint key %s not found in current list — starting from beginning", checkpoint)

    instruments = all_keys[start_idx:]
    remaining   = len(instruments)
    logger.info("Processing %d instruments (skipping %d already done)", remaining, start_idx)

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    ok_count    = start_idx  # count previously completed as successes
    error_count = 0
    skip_count  = 0
    start_wall  = time.monotonic()

    for batch_start in range(0, remaining, _CONCURRENCY):
        batch = instruments[batch_start : batch_start + _CONCURRENCY]
        tasks = [
            asyncio.create_task(_process_one(key, semaphore, session_factory))
            for key in batch
        ]
        results = await asyncio.gather(*tasks)

        for key, outcome in zip(batch, results):
            if outcome == "ok":
                ok_count += 1
                _save_checkpoint(key)
            elif outcome == "skip":
                skip_count += 1
            else:
                error_count += 1

        processed  = ok_count + error_count + skip_count
        elapsed    = time.monotonic() - start_wall
        rate       = (processed - start_idx) / elapsed if elapsed > 0 else 0
        eta_secs   = (total - processed) / rate if rate > 0 else 0

        logger.info(
            "Progress: %d/%d | OK: %d | Errors: %d | Skips: %d | "
            "Elapsed: %.0fs | Rate: %.1f/s | ETA: %.0fs",
            processed, total, ok_count, error_count, skip_count,
            elapsed, rate, eta_secs,
        )

    elapsed = time.monotonic() - start_wall
    logger.info(
        "Backfill complete — Total: %d | OK: %d | Errors: %d | Skips: %d | Time: %.1fs",
        total, ok_count, error_count, skip_count, elapsed,
    )

    if error_count == 0:
        _clear_checkpoint()
        logger.info("No errors — checkpoint cleared")
    else:
        logger.warning(
            "%d errors encountered. Checkpoint retained at last successful key. "
            "Re-run without --fresh to retry only failed instruments.",
            error_count,
        )

    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill company fundamentals data")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing checkpoint and start from the beginning",
    )
    args = parser.parse_args()

    if not settings.UPSTOX_ACCESS_TOKEN:
        logger.error(
            "UPSTOX_ACCESS_TOKEN is not set. "
            "Export a valid token before running the backfill."
        )
        sys.exit(1)

    asyncio.run(main(fresh=args.fresh))
