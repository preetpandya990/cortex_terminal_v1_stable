"""
Standalone 1D OHLCV backfill — fast catch-up for the training timeframe.

Context
-------
The worker sidecar's ingestion loop backfills ALL enabled timeframes
(1D + 1hour) at a deliberately gentle 40 req/min. When only the daily bars
matter (ML training reads 1D exclusively) and the store has fallen behind —
e.g. the workers were stopped for several sessions — this script closes the
gap in a fraction of the time by:

  1. restricting gap detection to the 1D timeframe only, and
  2. pacing at 60 req/min — the compliant sweet spot under Upstox's official
     limits for historical-candle APIs (50/s burst, 500/min, and the binding
     2,000-per-30-min bucket ≈ 66/min sustained).

It is a thin wrapper: gap detection, chunking, rate limiting, circuit
breaking, dead-lettering, and persistence are all the SAME battle-tested
components the worker uses (`app.services.data_ingestion_worker`) — no
second ingestion code path exists.

Safety
------
  - Reuses the worker's token-bucket limiter + circuit breaker; a Cloudflare
    or quota trip degrades to backoff/dead-letter, never corruption.
  - Writes are idempotent upserts keyed on (instrument, timestamp, timeframe).
  - Concurrent-run safe vs the worker sidecar in the same way two worker
    maintenance cycles are (upserts converge) — but prefer running it while
    the sidecar's ingestion is idle to avoid burning the shared API quota
    (limits are per-user, not per-process).
  - Iteration-capped: young listings produce head-gap chunks that legitimately
    return no candles and would re-appear every scan; the script re-scans at
    most --max-iterations times and then reports residual gaps instead of
    spinning.

Usage
-----
  python scripts/backfill_ohlcv_1d.py --dry-run     # gap census only, no writes
  python scripts/backfill_ohlcv_1d.py               # backfill at 60 req/min
  python scripts/backfill_ohlcv_1d.py --rate 40     # gentler pacing

Exit codes
----------
  0  gaps fully closed (or dry-run census printed)
  1  completed with residual gaps or API errors (see summary — often benign:
     young listings with no data for the requested head range)
  2  fatal error (no token, DB unreachable, unexpected exception)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

logger = logging.getLogger("backfill_ohlcv_1d")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

_EXIT_OK = 0
_EXIT_RESIDUAL = 1
_EXIT_ERROR = 2

#: Compliant sustained ceiling: Upstox's 2,000-per-30-min bucket ≈ 66 req/min.
DEFAULT_RATE_PER_MINUTE = 60

#: Small parallelism so HTTP latency never becomes the bottleneck below the
#: rate limiter; the limiter (not concurrency) is the binding throughput knob.
DEFAULT_CONCURRENCY = 2

DEFAULT_MAX_ITERATIONS = 3


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Standalone 1D-only OHLCV backfill (reuses the worker's "
        "ingestion engine at a faster, still-compliant pace)."
    )
    parser.add_argument(
        "--rate", type=int, default=DEFAULT_RATE_PER_MINUTE, metavar="RPM",
        help=f"Requests per minute (default {DEFAULT_RATE_PER_MINUTE}; "
        f"Upstox's 30-min bucket caps sustained throughput at ~66).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=DEFAULT_CONCURRENCY, metavar="N",
        help=f"Parallel in-flight requests (default {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=DEFAULT_MAX_ITERATIONS, metavar="N",
        help=f"Gap-scan passes before reporting residuals (default {DEFAULT_MAX_ITERATIONS}).",
    )
    parser.add_argument(
        "--tail-only", action="store_true",
        help="Fetch only the recent gap (chunks ending at the newest wanted "
        "date) and skip historical head gaps — the fast path for a "
        "catch-up after downtime.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Detect and summarize gaps only; no API calls, no writes.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    # Imports happen here, AFTER the env overrides in main(), so the worker's
    # rate limiter / semaphore pick up the requested pacing via settings.
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.services.data_ingestion_worker as ingestion
    from app.core.config import get_settings
    from app.services.upstox_client import UpstoxClient

    settings = get_settings()

    # 1D only: ENABLED_TIMEFRAMES is the module-level filter used by
    # detect_gaps. Narrowing it here affects THIS process only.
    ingestion.ENABLED_TIMEFRAMES = {"1D"}

    engine = create_async_engine(
        str(settings.DATABASE_URL), echo=False, pool_pre_ping=True,
        pool_size=5, max_overflow=5,
    )
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    client = UpstoxClient()

    try:
        await client.start()
        if not args.dry_run and not client.has_token:
            logger.error(
                "No UPSTOX_ACCESS_TOKEN configured — set it in backend/.env. "
                "Refusing to run against mock data."
            )
            return _EXIT_ERROR

        worker = ingestion.DataIngestionWorker(session_factory, client)
        logger.info(
            "1D backfill | rate=%d req/min | concurrency=%d | dry_run=%s",
            args.rate, args.concurrency, args.dry_run,
        )

        total_candles = 0
        total_errors = 0
        residual = 0

        from datetime import date, timedelta
        # detect_gaps targets yesterday as the last closed session; a "tail"
        # chunk is precisely one that ends there. Anchoring on this date (not
        # on max-of-remaining) keeps --tail-only correct after the tail closes.
        want_to = date.today() - timedelta(days=1)

        for iteration in range(1, args.max_iterations + 1):
            tasks = await worker.detect_gaps(backfill=True)
            if tasks and args.tail_only:
                skipped = len(tasks)
                tasks = [t for t in tasks if t.to_date == want_to]
                logger.info(
                    "--tail-only: %d recent chunk(s) kept, %d historical head "
                    "chunk(s) skipped", len(tasks), skipped - len(tasks),
                )
            residual = len(tasks)
            if not tasks:
                break

            if args.dry_run:
                # Tail chunks end at the newest wanted date; anything ending
                # earlier is a historical head gap (young listings, etc.).
                max_to = max(t.to_date for t in tasks)
                by_kind: dict[str, int] = {}
                for t in tasks:
                    kind = "tail (recent)" if t.to_date == max_to else "head (history)"
                    by_kind[kind] = by_kind.get(kind, 0) + 1
                logger.info(
                    "[DRY-RUN] %d gap chunk(s) across %d instrument(s): %s. "
                    "No API calls made.",
                    len(tasks), len({t.instrument_key for t in tasks}), by_kind,
                )
                return _EXIT_OK

            logger.info("Pass %d/%d — %d chunk(s) to fetch", iteration, args.max_iterations, len(tasks))
            stats = await worker.run_phase(tasks, f"1D-backfill-{iteration}")
            total_candles += stats.candles_ingested
            total_errors += stats.api_errors

            # A pass that ingested nothing cannot make progress on the next
            # scan either (remaining chunks are empty ranges) — stop early.
            if stats.candles_ingested == 0:
                logger.info("Pass %d ingested 0 candles — remaining gaps are empty ranges.", iteration)
                break
            await asyncio.sleep(2)  # let DB writes settle before re-scanning
        else:
            tasks = await worker.detect_gaps(backfill=True)
            if args.tail_only:
                tasks = [t for t in tasks if t.to_date == want_to]
            residual = len(tasks)

        if args.dry_run:
            logger.info("[DRY-RUN] no gaps detected — 1D store is current.")
            return _EXIT_OK

        logger.info(
            "✓ 1D backfill finished | candles=%d | api_errors=%d | residual_gaps=%d",
            total_candles, total_errors, residual,
        )
        return _EXIT_OK if (residual == 0 and total_errors == 0) else _EXIT_RESIDUAL

    except Exception:
        logger.error("1D backfill failed", exc_info=True)
        return _EXIT_ERROR
    finally:
        await client.stop()
        await engine.dispose()


def main() -> None:
    args = _parse_args()
    if not 10 <= args.rate <= 66:
        # 66/min = the 2,000-per-30-min official bucket; above it we WILL 429.
        print(f"--rate must be 10..66 (Upstox 30-min bucket), got {args.rate}", file=sys.stderr)
        sys.exit(_EXIT_ERROR)

    # Env overrides must precede any app import: the worker module constructs
    # its rate limiter and semaphore from settings at instance creation.
    os.environ["DATA_INGESTION_REQUESTS_PER_MINUTE"] = str(args.rate)
    os.environ["DATA_INGESTION_CONCURRENCY"] = str(args.concurrency)

    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
