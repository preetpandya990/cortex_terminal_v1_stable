"""
RAG corpus backfill runner (paced, monitorable).
================================================
Thin CLI over RagBackfillService — embeds the real news corpus
(ai_raw_events → ai_document_embeddings) via Gemini, paced under the embedding
rate limit, with live progress published to Redis.

The same service also backs the admin API (start/status/stop); this CLI lets an
operator run and monitor it standalone.

Prerequisites
-------------
  - GEMINI_API_KEY in backend/.env (billing-enabled key runs far faster).
  - Migration 0046 applied (vector(GEMINI_EMBED_DIM) column).
  - Postgres + Redis reachable.

Usage
-----
  cd backend
  # Start the backfill (paced at RAG_BACKFILL_RPM). Runs to completion; resumable.
  .venv/bin/python -m scripts.reembed_rag_corpus
  .venv/bin/python -m scripts.reembed_rag_corpus --rpm 1000   # paid-key pace

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


async def _run(rpm: int | None, sub_batch: int | None, window_days: int | None) -> int:
    s = get_settings()
    if not s.GEMINI_API_KEY:
        logger.error("GEMINI_API_KEY is not set — aborting. Set it in backend/.env.")
        return 2

    await init_redis()
    await CortexIntelligenceClient.initialize()
    try:
        service = RagBackfillService(
            get_redis(), rpm=rpm, sub_batch=sub_batch, window_days=window_days,
        )
        progress = await service.run()
        print(json.dumps(progress.__dict__, indent=2, default=str))
        return 0 if progress.state == "completed" else 1
    finally:
        await close_intelligence_client()
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(description="Paced, monitorable RAG corpus backfill.")
    parser.add_argument("--status", action="store_true", help="Print current backfill status and exit.")
    parser.add_argument("--stop", action="store_true", help="Request a graceful stop of the running backfill.")
    parser.add_argument("--rpm", type=int, default=None, help="Override embedding pace (texts/min).")
    parser.add_argument("--sub-batch", type=int, default=None, help="Override texts per embed call.")
    parser.add_argument("--window-days", type=int, default=None, help="Override corpus lookback window.")
    args = parser.parse_args()

    if args.status:
        sys.exit(asyncio.run(_status()))
    if args.stop:
        sys.exit(asyncio.run(_stop()))
    sys.exit(asyncio.run(_run(args.rpm, args.sub_batch, args.window_days)))


if __name__ == "__main__":
    main()
