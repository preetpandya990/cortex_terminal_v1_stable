"""
RAG corpus backfill runner (paced, monitorable).
================================================
Thin CLI over RagBackfillService — embeds the real news corpus
(ai_raw_events → ai_document_embeddings) via Gemini, with live progress
published to Redis.

The same service also backs the admin API (start/status/stop); this CLI lets an
operator run and monitor it standalone.

Prerequisites
-------------
  - GEMINI_API_KEY in backend/.env (billing-enabled key required for --batch).
  - Migration 0048 applied (halfvec(768) column).
  - Postgres + Redis reachable.

Usage
-----
  cd backend

  # Synchronous mode (default): paced at RAG_BACKFILL_RPM, near-real-time.
  .venv/bin/python -m scripts.reembed_rag_corpus
  .venv/bin/python -m scripts.reembed_rag_corpus --rpm 1000   # paid-key pace

  # Batch API mode: 50% cheaper, results deferred 30–90 min (paid tier only).
  .venv/bin/python -m scripts.reembed_rag_corpus --batch

  # Monitor a run in progress (read-only, from Redis):
  .venv/bin/python -m scripts.reembed_rag_corpus --status

  # Request a graceful stop of the running backfill:
  .venv/bin/python -m scripts.reembed_rag_corpus --stop

Idempotent + resumable: re-running continues where it left off (ON CONFLICT).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from app.ai.intelligence.llm_client import CortexIntelligenceClient, close_intelligence_client
from app.ai.rag.backfill_service import RagBackfillService
from app.core.config import get_settings
from app.core.redis import close_redis, get_redis, init_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("reembed_rag_corpus")


async def _status() -> int:
    await init_redis()
    try:
        st = await RagBackfillService.read_status(get_redis())
        print(json.dumps(st, indent=2) if st else "No backfill status found (never run).")
    finally:
        await close_redis()
    return 0


async def _stop() -> int:
    await init_redis()
    try:
        await RagBackfillService.request_cancel(get_redis())
        print("Cancellation requested — the running backfill will stop after the current sub-batch.")
    finally:
        await close_redis()
    return 0


async def _run(
    rpm: int | None,
    sub_batch: int | None,
    window_days: int | None,
    *,
    use_batch_api: bool = False,
) -> int:
    s = get_settings()
    if not s.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set — aborting. Set it in backend/.env.")
        return 2

    if use_batch_api:
        logger.info(
            "Batch API mode enabled — jobs complete in 30–90 min (paid tier only). "
            "Per-job size: %d texts, poll interval: %ds, timeout: %ds.",
            s.RAG_BATCH_JOB_SIZE,
            s.RAG_BATCH_POLL_INTERVAL_SECS,
            s.RAG_BATCH_JOB_TIMEOUT_SECS,
        )

    await init_redis()
    await CortexIntelligenceClient.initialize()
    try:
        service = RagBackfillService(
            get_redis(), rpm=rpm, sub_batch=sub_batch, window_days=window_days,
        )
        progress = (
            await service.run_batch() if use_batch_api else await service.run()
        )
        print(json.dumps(progress.__dict__, indent=2, default=str))
        return 0 if progress.state == "completed" else 1
    finally:
        await close_intelligence_client()
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paced, monitorable RAG corpus backfill.")
    parser.add_argument(
        "--status", action="store_true",
        help="Print current backfill status and exit.",
    )
    parser.add_argument(
        "--stop", action="store_true",
        help="Request a graceful stop of the running backfill.",
    )
    parser.add_argument(
        "--batch", action="store_true",
        help=(
            "Use the Gemini Batch API (paid tier, 50%% cheaper, 30–90 min latency). "
            "Mutually exclusive with --rpm and --sub-batch."
        ),
    )
    parser.add_argument("--rpm", type=int, default=None, help="Override embedding pace (texts/min). Sync mode only.")
    parser.add_argument("--sub-batch", type=int, default=None, help="Override texts per embed call. Sync mode only.")
    parser.add_argument("--window-days", type=int, default=None, help="Override corpus lookback window.")
    args = parser.parse_args()

    if args.batch and (args.rpm or args.sub_batch):
        parser.error("--batch is incompatible with --rpm and --sub-batch (batch mode has no per-minute pacing).")

    if args.status:
        sys.exit(asyncio.run(_status()))
    if args.stop:
        sys.exit(asyncio.run(_stop()))
    sys.exit(asyncio.run(_run(
        args.rpm, args.sub_batch, args.window_days, use_batch_api=args.batch
    )))


if __name__ == "__main__":
    main()
