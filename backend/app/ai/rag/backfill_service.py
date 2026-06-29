"""
RAG corpus backfill — rate-limited, resumable, monitorable background job.
==========================================================================
Embeds the real news corpus (ai_raw_events) into ai_document_embeddings via
Gemini, pacing under the embedding rate limit and publishing live progress so
the run is *actively monitorable* (Redis status + Prometheus + CLI/API surface).

Why a paced background job
--------------------------
gemini-embedding-001 on the free tier allows ~100 embedding requests/minute and
counts per text, so the ~11k-event corpus is a long (~2h) run.  This job paces
itself with a token bucket, streams rows in continuously, and is fully resumable
(the ingester upserts with ON CONFLICT DO NOTHING, so re-running never duplicates
and always continues where it left off).

Monitoring surface
-------------------
  - Redis ``cortex:rag:backfill:status`` (JSON): state, totals, pct, rate, ETA,
    timestamps, last_error — read by the CLI ``--status`` and the admin API.
  - Prometheus: rag_documents_embedded_total, rag_backfill_remaining,
    rag_backfill_running.

Safety / reliability
--------------------
  - Single-runner: a Redis lock (SET NX) with a refreshed TTL prevents concurrent
    backfills; a dead runner's lock expires so another can take over.
  - Cooperative cancel via ``cortex:rag:backfill:cancel``.
  - Transient embedding errors are retried with backoff; a hard stop records the
    error in status and exits cleanly (already-embedded rows are durable).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Any

from sqlalchemy import func, select

from app.ai.fusion.models import AIDocumentEmbedding, AIRawEvent
from app.ai.intelligence.llm_client import LLMFallbackExhausted
from app.ai.rag.ingester import (
    _SOURCE_TABLE,
    _build_embed_rows,
    _dedup_events,
    _fetch_unembedded_events,
    _insert_embed_rows,
    ingest_batch,
)
from app.core.config import get_settings
from app.core.database import AsyncSessionLocal

logger = logging.getLogger(__name__)

_STATUS_KEY = "cortex:rag:backfill:status"
_LOCK_KEY = "cortex:rag:backfill:lock"
_CANCEL_KEY = "cortex:rag:backfill:cancel"
_LOCK_TTL_SECS = 120  # refreshed every cycle; expires if the runner dies


# ── Token-bucket rate limiter (paces outbound embedding request-units) ──────────

class TokenBucket:
    """Async token bucket: smooth pacing at ``rate_per_min`` units/minute.

    ``acquire(n)`` blocks until n tokens are available, then consumes them.  We
    pace by *text count* (one token per text) so the limit holds whether the API
    accounts embeddings per-text or per-request.

    The bucket starts EMPTY and ``capacity`` (max burst) is kept small, so the
    outbound rate never exceeds the limit at startup or after a backoff pause —
    the worst case in any 60s window is ``capacity + rate_per_min``.  (Starting
    full with capacity == rate would burst a full minute's quota instantly.)
    """

    def __init__(
        self,
        rate_per_min: float,
        *,
        capacity: float | None = None,
        start_full: bool = False,
    ) -> None:
        self._capacity = float(capacity if capacity is not None else rate_per_min)
        self._tokens = self._capacity if start_full else 0.0
        self._refill_per_sec = float(rate_per_min) / 60.0
        self._updated = monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, n: int) -> None:
        while True:
            async with self._lock:
                now = monotonic()
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._refill_per_sec
                )
                self._updated = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self._refill_per_sec
            await asyncio.sleep(wait)


# ── Progress model ──────────────────────────────────────────────────────────────

@dataclass
class BackfillProgress:
    state: str = "idle"            # idle | running | completed | cancelled | failed
    total: int = 0                 # unembedded events at start
    embedded: int = 0              # rows inserted this run
    skipped: int = 0               # already-embedded / conflict
    processed: int = 0             # embedded + skipped
    pct: float = 0.0
    rate_per_min: float = 0.0
    eta_seconds: int | None = None
    rpm_limit: int = 0
    window_days: int = 0
    started_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None
    last_error: str | None = None
    errors: int = 0

    def as_json(self) -> str:
        return json.dumps(asdict(self), default=str)


# ── Service ─────────────────────────────────────────────────────────────────────

class RagBackfillService:
    """Drives the paced, monitorable corpus embedding run."""

    def __init__(
        self,
        redis: Any,
        *,
        rpm: int | None = None,
        sub_batch: int | None = None,
        window_days: int | None = None,
        fetch_limit: int = 500,
    ) -> None:
        s = get_settings()
        self._redis = redis
        self._rpm = rpm or s.RAG_BACKFILL_RPM
        self._sub_batch = sub_batch or s.RAG_BACKFILL_SUB_BATCH
        self._window_days = window_days or s.RAG_BACKFILL_WINDOW_DAYS
        self._fetch_limit = fetch_limit
        self._cutoff = datetime.now(timezone.utc) - timedelta(days=self._window_days)

    # ── status helpers ──────────────────────────────────────────────────────
    @classmethod
    async def read_status(cls, redis: Any) -> dict | None:
        raw = await redis.get(_STATUS_KEY)
        return json.loads(raw) if raw else None

    @classmethod
    async def request_cancel(cls, redis: Any) -> None:
        await redis.set(_CANCEL_KEY, "1", ex=3600)

    async def _publish(self, p: BackfillProgress) -> None:
        p.updated_at = datetime.now(timezone.utc).isoformat()
        try:
            await self._redis.set(_STATUS_KEY, p.as_json())
        except Exception as exc:
            logger.debug("rag.backfill: status publish failed (non-fatal): %s", exc)

    async def _refresh_lock(self) -> None:
        try:
            await self._redis.expire(_LOCK_KEY, _LOCK_TTL_SECS)
        except Exception:
            pass

    async def _cancelled(self) -> bool:
        try:
            return bool(await self._redis.get(_CANCEL_KEY))
        except Exception:
            return False

    async def _count_remaining(self) -> int:
        """Count unembedded events in the window (anti-join, index-only)."""
        async with AsyncSessionLocal() as db:
            stmt = (
                select(func.count())
                .select_from(AIRawEvent)
                .outerjoin(
                    AIDocumentEmbedding,
                    (AIDocumentEmbedding.source_table == _SOURCE_TABLE)
                    & (AIDocumentEmbedding.source_id == AIRawEvent.id),
                )
                .where(
                    AIDocumentEmbedding.id.is_(None),
                    AIRawEvent.event_timestamp >= self._cutoff,
                )
            )
            return int((await db.execute(stmt)).scalar() or 0)

    # ── main run ──────────────────────────────────────────────────────────────
    async def run(self) -> BackfillProgress:
        from app.core.metrics import (
            rag_backfill_remaining,
            rag_backfill_running,
            rag_documents_embedded_total,
        )

        # Single-runner lock.
        got = await self._redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECS)
        if not got:
            existing = await self.read_status(self._redis) or {}
            raise RuntimeError(
                f"A RAG backfill is already running (state={existing.get('state')}). "
                "Use the status command to monitor it."
            )
        await self._redis.delete(_CANCEL_KEY)

        p = BackfillProgress(
            state="running",
            rpm_limit=self._rpm,
            window_days=self._window_days,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        # Start empty, burst capped at one sub-batch → never exceeds the limit.
        bucket = TokenBucket(self._rpm, capacity=self._sub_batch)
        t0 = monotonic()
        rag_backfill_running.set(1)

        try:
            p.total = await self._count_remaining()
            await self._publish(p)
            logger.info(
                "rag.backfill start: total=%d rpm=%d sub_batch=%d window_days=%d",
                p.total, self._rpm, self._sub_batch, self._window_days,
            )

            while True:
                if await self._cancelled():
                    p.state = "cancelled"
                    break

                async with AsyncSessionLocal() as db:
                    events = await _fetch_unembedded_events(
                        db, self._cutoff, limit=self._fetch_limit
                    )
                    if not events:
                        p.state = "completed"
                        break

                    for i in range(0, len(events), self._sub_batch):
                        if await self._cancelled():
                            p.state = "cancelled"
                            break
                        sub = events[i : i + self._sub_batch]
                        await bucket.acquire(len(sub))   # pace outbound calls
                        inserted = await self._ingest_with_retry(db, sub, p)

                        p.embedded += inserted
                        p.skipped += len(sub) - inserted
                        p.processed = p.embedded + p.skipped
                        elapsed = max(monotonic() - t0, 1e-6)
                        p.rate_per_min = round(p.processed / elapsed * 60.0, 1)
                        remaining = max(p.total - p.processed, 0)
                        p.pct = round(p.processed / p.total * 100.0, 1) if p.total else 100.0
                        p.eta_seconds = (
                            int(remaining / (p.rate_per_min / 60.0))
                            if p.rate_per_min > 0 else None
                        )
                        rag_documents_embedded_total.inc(inserted)
                        rag_backfill_remaining.set(remaining)
                        await self._publish(p)
                        await self._refresh_lock()

                    if p.state == "cancelled":
                        break

        except asyncio.CancelledError:
            p.state = "cancelled"
            raise
        except Exception as exc:
            p.state = "failed"
            p.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("rag.backfill failed: %s", exc, exc_info=True)
        finally:
            p.finished_at = datetime.now(timezone.utc).isoformat()
            await self._publish(p)
            rag_backfill_running.set(0)
            try:
                await self._redis.delete(_LOCK_KEY)
            except Exception:
                pass

        logger.info(
            "rag.backfill end: state=%s embedded=%d skipped=%d errors=%d",
            p.state, p.embedded, p.skipped, p.errors,
        )
        return p

    async def _ingest_with_retry(
        self, db: Any, sub: list[Any], p: BackfillProgress, max_attempts: int = 4
    ) -> int:
        """Embed+insert one sub-batch, retrying transient embedding failures.

        The paced bucket should keep us under the rate limit; this covers the
        occasional blip.  A persistent failure raises to abort the run cleanly
        (already-committed rows are durable; the job is resumable)."""
        delay = 2.0
        for attempt in range(1, max_attempts + 1):
            try:
                return await ingest_batch(db, sub, batch_size=len(sub))
            except LLMFallbackExhausted as exc:
                p.errors += 1
                p.last_error = f"{type(exc).__name__}: {exc}"
                if attempt == max_attempts:
                    raise
                logger.warning(
                    "rag.backfill: embed failed (attempt %d/%d) — backing off %.0fs: %s",
                    attempt, max_attempts, delay, str(exc)[:120],
                )
                await self._publish(p)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        return 0

    # ── Batch-API run ─────────────────────────────────────────────────────────
    async def run_batch(self) -> BackfillProgress:
        """
        Corpus embedding via the Gemini Batch API (paid tier, 50% cost saving).

        Fetches unembedded events in chunks of ``RAG_BATCH_JOB_SIZE``, submits
        each chunk as an async Gemini batch embedding job, polls for completion
        (30–90 min typical per job), then inserts the results.

        Compared with ``run()``:
        - **Cost**: 50% cheaper ($0.075 vs $0.15 per 1M tokens).
        - **Latency**: Results arrive in 30–90 min (not seconds).
        - **Rate limits**: No per-minute cap; the Batch API processes the entire
          job server-side at Google's pace.

        Use for overnight corpus rebuilds or large-scale re-embeds.  Use ``run()``
        for near-real-time corpus freshness.

        Requires a billing-enabled Gemini API key — free tier does not support
        the Batch API (``google-genai >= 2.8``, experimental surface).
        """
        from app.ai.rag.embedder import embed_texts_batch
        from app.core.metrics import (
            rag_backfill_remaining,
            rag_backfill_running,
            rag_documents_embedded_total,
        )

        # Single-runner lock (shared with synchronous backfill — cannot run both
        # at the same time; the lock key is the same so they mutually exclude).
        got = await self._redis.set(_LOCK_KEY, "1", nx=True, ex=_LOCK_TTL_SECS)
        if not got:
            existing = await self.read_status(self._redis) or {}
            raise RuntimeError(
                f"A RAG backfill is already running (state={existing.get('state')}). "
                "Use the status command to monitor it."
            )
        await self._redis.delete(_CANCEL_KEY)

        s = get_settings()
        batch_job_size = s.RAG_BATCH_JOB_SIZE
        poll_interval = float(s.RAG_BATCH_POLL_INTERVAL_SECS)
        timeout = float(s.RAG_BATCH_JOB_TIMEOUT_SECS)

        p = BackfillProgress(
            state="running",
            rpm_limit=0,  # not applicable for Batch API mode
            window_days=self._window_days,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        t0 = monotonic()
        rag_backfill_running.set(1)

        try:
            p.total = await self._count_remaining()
            await self._publish(p)
            logger.info(
                "rag.batch_backfill start: total=%d job_size=%d window_days=%d "
                "poll_interval=%.0fs timeout=%.0fs",
                p.total, batch_job_size, self._window_days, poll_interval, timeout,
            )

            while True:
                if await self._cancelled():
                    p.state = "cancelled"
                    break

                async with AsyncSessionLocal() as db:
                    events = await _fetch_unembedded_events(
                        db, self._cutoff, limit=batch_job_size
                    )
                if not events:
                    p.state = "completed"
                    break

                deduped = _dedup_events(events)
                texts = [row.raw_content for row in deduped]

                await self._refresh_lock()
                await self._publish(p)
                logger.info(
                    "rag.batch_backfill: submitting batch job texts=%d (raw=%d deduped=%d)",
                    len(texts), len(events), len(events) - len(deduped),
                )

                try:
                    vectors = await embed_texts_batch(
                        texts,
                        input_type="passage",
                        poll_interval_secs=poll_interval,
                        timeout_secs=timeout,
                    )
                except (LLMFallbackExhausted, RuntimeError, asyncio.TimeoutError) as exc:
                    p.errors += 1
                    p.last_error = f"{type(exc).__name__}: {exc}"
                    p.state = "failed"
                    logger.error(
                        "rag.batch_backfill: batch job failed — %s",
                        exc,
                        exc_info=True,
                    )
                    break

                async with AsyncSessionLocal() as db:
                    rows = _build_embed_rows(deduped, vectors)
                    inserted = await _insert_embed_rows(db, rows)

                skipped = len(rows) - inserted
                p.embedded += inserted
                p.skipped += skipped
                p.processed = p.embedded + p.skipped
                elapsed = max(monotonic() - t0, 1e-6)
                p.rate_per_min = round(p.processed / elapsed * 60.0, 1)
                remaining = max(p.total - p.processed, 0)
                p.pct = round(p.processed / p.total * 100.0, 1) if p.total else 100.0
                p.eta_seconds = (
                    int(remaining / (p.rate_per_min / 60.0))
                    if p.rate_per_min > 0 else None
                )
                rag_documents_embedded_total.inc(inserted)
                rag_backfill_remaining.set(remaining)
                await self._publish(p)
                await self._refresh_lock()

        except asyncio.CancelledError:
            p.state = "cancelled"
            raise
        except Exception as exc:
            p.state = "failed"
            p.last_error = f"{type(exc).__name__}: {exc}"
            logger.error("rag.batch_backfill failed: %s", exc, exc_info=True)
        finally:
            p.finished_at = datetime.now(timezone.utc).isoformat()
            await self._publish(p)
            rag_backfill_running.set(0)
            try:
                await self._redis.delete(_LOCK_KEY)
            except Exception:
                pass

        logger.info(
            "rag.batch_backfill end: state=%s embedded=%d skipped=%d errors=%d",
            p.state, p.embedded, p.skipped, p.errors,
        )
        return p


# ── Lifespan background worker ───────────────────────────────────────────────────

async def run_rag_refresh_worker(redis: Any) -> None:
    """
    Periodic RAG corpus refresh — runs as a FastAPI lifespan background task.

    Embeds new ai_raw_events into ai_document_embeddings immediately on startup,
    then every RAG_REFRESH_INTERVAL_HOURS hours.  Uses RagBackfillService so each
    cycle is paced (TokenBucket), resumable (ON CONFLICT DO NOTHING), single-runner
    (Redis SET NX lock), and fully observable (Redis status + Prometheus).

    The Redis lock ensures at most one backfill runs at any time.  If the CLI tool
    (scripts/reembed_rag_corpus.py) holds the lock, this worker logs a skip and
    waits for the next interval rather than racing.

    Shutdown: asyncio.CancelledError propagates cleanly.  If raised during an
    active backfill, RagBackfillService.run() updates the Redis status to
    ``"cancelled"`` and releases the lock before re-raising, leaving the corpus
    in a consistent state.  The next startup resumes without duplicates.
    """
    settings = get_settings()
    interval_secs = settings.RAG_REFRESH_INTERVAL_HOURS * 3600

    use_batch = settings.RAG_BATCH_EMBED_ENABLED
    logger.info(
        "rag.refresh_worker: started (interval=%dh window=%dd rpm=%d sub_batch=%d "
        "batch_api=%s)",
        settings.RAG_REFRESH_INTERVAL_HOURS,
        settings.RAG_BACKFILL_WINDOW_DAYS,
        settings.RAG_BACKFILL_RPM,
        settings.RAG_BACKFILL_SUB_BATCH,
        use_batch,
    )

    while True:
        try:
            svc = RagBackfillService(redis)
            progress = await (svc.run_batch() if use_batch else svc.run())
            logger.info(
                "rag.refresh_worker: cycle complete — mode=%s state=%s "
                "embedded=%d skipped=%d errors=%d",
                "batch" if use_batch else "sync",
                progress.state,
                progress.embedded,
                progress.skipped,
                progress.errors,
            )
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            # Lock held by another runner (CLI tool or a sibling process) — skip.
            logger.info("rag.refresh_worker: cycle skipped — %s", exc)
        except Exception as exc:
            # Unexpected error (Redis unavailable, unhandled DB error, etc.) —
            # log and continue; the next cycle will retry cleanly.
            logger.error(
                "rag.refresh_worker: unexpected error — %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )

        # CancelledError raised here propagates naturally out of the while loop,
        # signalling a clean shutdown to the lifespan task manager.
        await asyncio.sleep(interval_secs)
