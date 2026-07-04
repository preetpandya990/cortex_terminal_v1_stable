"""
RAG Corpus TTL Eviction — Daily Cleanup Worker
===============================================
Deletes ``ai_document_embeddings`` rows whose ``as_of_timestamp`` is older than
``RAG_EMBEDDING_TTL_DAYS`` (default 7 days).

Why 7 days
----------
The retriever's freshness window is bounded by ``RAG_WINDOW_HOURS`` (max 168 h =
7 days).  Any embedding older than 7 days can never appear in a retrieval result
and is pure dead-weight — wasting pgvector HNSW index memory and slowing HNSW
builds.  Deleting them keeps the candidate set lean and the index compact.

Scheduling
----------
Runs once per 24-hour cycle.  Last-run wall time is persisted in Redis under
``cai:rag:cleanup:last_run`` (TTL 8 days) so a worker restart mid-cycle does NOT
re-run the cleanup immediately — it resumes its scheduled position.  The check
loop ticks every hour, so the maximum drift from the intended 24-hour cadence is
±1 hour.

Safety
------
- ``shutdown`` event is respected at every sleep boundary.
- Exceptions are caught and logged; a failed cycle does not crash the task loop.
- Prometheus counters (``rag_cleanup_rows_deleted_total``,
  ``rag_cleanup_runs_total``) provide observability for Grafana alerting.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import get_settings
from app.core.redis import CacheService

logger = logging.getLogger(__name__)

# Redis key that records when the last successful cleanup ran.
_EPOCH_KEY  = "cai:rag:cleanup:last_run"
_EPOCH_TTL  = 8 * 86_400   # 8 days; outlives the cleanup cycle with margin

# How often the outer loop wakes to check whether a cleanup cycle is due.
_TICK_SECS  = 3_600         # 1 hour


async def rag_cleanup_loop(
    session_factory: async_sessionmaker,
    redis: CacheService,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Daily RAG corpus TTL eviction loop.

    Runs inside the worker sidecar supervised task group.  Deletes embedding
    rows older than ``RAG_EMBEDDING_TTL_DAYS`` once every 24 hours.
    """
    from app.core.metrics import rag_cleanup_rows_deleted_total, rag_cleanup_runs_total

    settings = get_settings()
    ttl_days = settings.RAG_EMBEDDING_TTL_DAYS

    logger.info(
        "RAG cleanup loop started (ttl_days=%d, tick_secs=%d)",
        ttl_days,
        _TICK_SECS,
    )

    while not shutdown.is_set():
        now = datetime.now(timezone.utc)

        # ── Check whether a cleanup cycle is due ─────────────────────────────
        should_run = True
        try:
            last_run_str: str | None = await redis.get(_EPOCH_KEY)
            if last_run_str is not None:
                last_run = datetime.fromisoformat(last_run_str)
                age = now - last_run
                if age < timedelta(hours=24):
                    should_run = False
                    logger.debug(
                        "rag_cleanup: skipping — last run was %.1fh ago (threshold 24h)",
                        age.total_seconds() / 3600,
                    )
        except Exception as exc:
            # Redis unavailable — run defensively to avoid missing a cycle.
            logger.warning(
                "rag_cleanup: Redis epoch key read failed (%s); running cleanup as fallback",
                exc,
            )

        # ── Run the cleanup cycle ─────────────────────────────────────────────
        if should_run:
            cutoff = now - timedelta(days=ttl_days)
            logger.info(
                "rag_cleanup: starting — deleting embeddings older than %s (ttl_days=%d)",
                cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                ttl_days,
            )

            try:
                async with session_factory() as db:
                    result = await db.execute(
                        text(
                            "DELETE FROM ai_document_embeddings "
                            "WHERE as_of_timestamp < :cutoff"
                        ),
                        {"cutoff": cutoff},
                    )
                    deleted = result.rowcount if result.rowcount >= 0 else 0
                    await db.commit()

                rag_cleanup_rows_deleted_total.inc(deleted)
                rag_cleanup_runs_total.labels(status="success").inc()

                logger.info(
                    "rag_cleanup: complete — deleted=%d rows cutoff=%s",
                    deleted,
                    cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
                )

                # Persist run timestamp so restarts don't re-run immediately.
                try:
                    await redis.set(_EPOCH_KEY, now.isoformat(), ttl=_EPOCH_TTL)
                except Exception as exc:
                    logger.warning(
                        "rag_cleanup: failed to write epoch key (non-fatal): %s", exc
                    )

            except Exception as exc:
                rag_cleanup_runs_total.labels(status="error").inc()
                logger.exception(
                    "rag_cleanup: cleanup cycle failed — will retry next tick: %s", exc
                )

        if on_cycle is not None:
            on_cycle()

        # ── Sleep until the next tick (interruptible by shutdown) ─────────────
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_TICK_SECS)
        except asyncio.TimeoutError:
            pass  # normal wakeup — continue to next iteration

    logger.info("RAG cleanup loop exiting cleanly (shutdown signalled)")
