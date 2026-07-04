"""
Forecast Batch Worker
=====================
Background task that drains the async forecast queue and fires one Gemini call
per batch of symbols instead of one call per signal-assembly hot path.

Architecture
------------
``SignalAssembler.gather_news_forecast()`` enqueues cache-miss (symbol, context)
payloads here and returns the NLP fallback immediately — the hot path never
blocks on Gemini.  This worker pops up to ``NEWS_FORECAST_BATCH_SIZE`` payloads,
fires one Gemini call with ``NewsForecastBatchOutput`` structured output, validates
each per-symbol result independently, and writes valid forecasts to the same
``cortex:news_forecast:{symbol}:{digest}`` cache keys that signal_assembler reads.
The NEXT signal assembly for the same (symbol, news-set) within the 5-minute TTL
returns the Gemini result with zero blocking.

Reliability guarantees
----------------------
- Per-item validation: ``symbol`` echo-back, ``direction`` enum, ``confidence``
  range [0, 1].  Invalid items fall back silently to NLP — no exceptions, no
  retries (retrying on validation failure risks doubling quota on degraded quota).
- ``GeminiQuotaExhausted`` / ``GeminiBudgetThrottled``: batch is dropped,
  dedup keys expire naturally so items can be re-enqueued on the next cycle.
- ``max_output_tokens=1000``: generous ceiling prevents the confirmed Gemini
  structured-output bug where fields repeat until token limit → silent None.
- Batch size ≤ 5: validated safe ceiling for complex multi-item structured
  output (15 indicators + 6 events per symbol, from community research).
- Never use the Gemini Async Batch API (confirmed ~70% hallucination rate).

Queue protocol
--------------
  Producer (signal_assembler): SET NX EX 600 dedup key, then publish the JSON
  payload (with ``enqueued_at``) to the ``cortex.forecast.batch`` Kafka topic.

  This worker holds ONE module-level manual-assignment consumer in the
  ``cortex-forecast-batch`` group, shared by the idle loop and the demand
  flush behind one asyncio.Lock (consumer churn would cause rebalance-style
  offset races).  Offsets are committed only AFTER ``_flush_batch`` returns,
  so a crash mid-flush redelivers the batch instead of losing it (the old
  LPOP-then-crash bug).  A deliberate batch-drop (quota/budget throttle in
  the auto loop) still commits — that keeps today's drop-with-dedup-TTL
  semantics: the 600s dedup expiry re-enqueues naturally, and retrying would
  burn Gemini quota on stale news.  Payloads older than one hour are skipped
  at drain time (replaces the old producer-side queue trim).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

from aiokafka.structs import TopicPartition
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.fusion.news_forecaster import (
    NewsForecastBatchOutput,
    _BATCH_FORECAST_SYSTEM_PROMPT,
    build_batch_forecast_prompt,
    score_from_direction,
)
from app.ai.fusion.models import AILLMAuditLog
from app.ai.intelligence.llm_client import get_intelligence_client
from app.ai.intelligence.request_manager import (
    GeminiBudgetThrottled,
    GeminiQuotaExhausted,
    GeminiRateLimitError,
    Priority,
)
from app.core.config import get_settings
from app.core.kafka import KafkaGroups, KafkaTopics, new_consumer, pending_count
from app.core.metrics import (
    llm_audit_log_writes_total,
    news_forecast_batch_calls_total,
    news_forecast_batch_size_histogram,
    news_forecast_queue_depth,
)

logger = logging.getLogger(__name__)

# Generous token ceiling — 150 tokens × 5 symbols + 33% headroom.
# Prevents the structured-output silent-None bug caused by token exhaustion.
_MAX_OUTPUT_TOKENS = 1_000

# Seconds between queue polls when the queue is empty.
_IDLE_POLL_INTERVAL = 2.0

# Payloads older than this are skipped (and committed past) at drain time —
# a forecast is a 5-minute-TTL cache warmer, so hour-old news context is
# worthless and calling Gemini on it wastes quota.  Replaces the old
# producer-side LLEN>cap→RPOP queue trim.
_STALE_AFTER_SECS = 3_600


class ForecastQueueConsumer:
    """
    Module-level owner of the single forecast-batch Kafka consumer.

    The idle loop and the demand flush share this one consumer behind
    ``lock`` — two consumers in the same group would trigger rebalance churn,
    and two tasks reading one consumer without the lock would interleave
    uncommitted reads (commit is position-based, so task A's commit would
    silently commit task B's in-flight items).
    """

    _TP = TopicPartition(KafkaTopics.FORECAST_BATCH, 0)

    def __init__(self) -> None:
        self._consumer: Any = None
        self.lock = asyncio.Lock()

    async def ensure_started(self) -> Any:
        """Create and start the consumer on first use (manual assignment —
        the topic has exactly one partition, so group-join adds no value)."""
        if self._consumer is None:
            consumer = new_consumer(group_id=KafkaGroups.FORECAST_BATCH)
            consumer.assign([self._TP])
            await consumer.start()
            self._consumer = consumer
        return self._consumer

    async def drain(self, max_items: int, timeout_ms: int) -> list[dict]:
        """
        Read up to ``max_items`` payloads.  Malformed and stale payloads are
        dropped here (the next commit passes over them).  Does NOT commit —
        the caller commits after the batch reaches its terminal outcome.
        """
        consumer = await self.ensure_started()
        batches = await consumer.getmany(timeout_ms=timeout_ms, max_records=max_items)
        items: list[dict] = []
        for records in batches.values():
            for msg in records:
                payload = msg.value
                if not isinstance(payload, dict) or not payload.get("symbol"):
                    continue
                if _is_stale(payload):
                    logger.debug(
                        "forecast_batch_worker: skipping stale payload for %s "
                        "(enqueued_at=%s)",
                        payload.get("symbol"), payload.get("enqueued_at"),
                    )
                    continue
                items.append(payload)
        return items

    async def commit(self) -> None:
        """Commit the current consume position (call only after a terminal outcome)."""
        if self._consumer is not None:
            await self._consumer.commit()

    async def rewind_to_committed(self) -> None:
        """
        Seek back to the last committed offset, discarding uncommitted
        in-memory reads.  Used when a demand flush aborts early: without the
        seek, the live consumer's advanced position would silently skip the
        uncommitted remainder until the next process restart.
        """
        if self._consumer is None:
            return
        committed = await self._consumer.committed(self._TP)
        if committed is not None and committed >= 0:
            self._consumer.seek(self._TP, committed)
        else:
            await self._consumer.seek_to_beginning(self._TP)

    async def stop(self) -> None:
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                await self._consumer.stop()
            self._consumer = None


_queue_consumer = ForecastQueueConsumer()


def _is_stale(payload: dict) -> bool:
    """True when the payload's enqueued_at is more than _STALE_AFTER_SECS old."""
    raw = payload.get("enqueued_at")
    if not raw:
        return False
    try:
        enqueued_at = datetime.fromisoformat(str(raw))
    except (ValueError, TypeError):
        return False
    if enqueued_at.tzinfo is None:
        enqueued_at = enqueued_at.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - enqueued_at).total_seconds() > _STALE_AFTER_SECS


async def forecast_batch_loop(
    redis: Any,
    session_factory: async_sessionmaker,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """Supervised entry-point — runs until ``shutdown`` is set.

    ``redis`` is used only for result-cache ``setex`` writes inside
    ``_flush_batch`` — queueing itself lives on Kafka.
    """
    logger.info("forecast_batch_worker: starting")

    try:
        while not shutdown.is_set():
            settings   = get_settings()
            batch_size = settings.NEWS_FORECAST_BATCH_SIZE
            window     = settings.NEWS_FORECAST_BATCH_WINDOW_SECS

            if on_cycle is not None:
                on_cycle()

            # ── Update queue-depth gauge from consumer lag ────────────────────
            # Single set-site now (the old producer-side write in
            # signal_assembler's trim block was deleted with the trim).
            try:
                depth = await pending_count(
                    KafkaTopics.FORECAST_BATCH, KafkaGroups.FORECAST_BATCH
                )
                news_forecast_queue_depth.set(depth)
            except Exception:
                pass

            # When FORECAST_AUTO_DISPATCH is False, never consume — items stay
            # queued on the topic (durable, unlike the sentiment queue) until
            # an explicit dispatch via flush_pending_forecasts() drains them.
            if not settings.FORECAST_AUTO_DISPATCH:
                await _interruptible_sleep(shutdown, _IDLE_POLL_INTERVAL)
                continue

            # ── Accumulate-and-flush under the shared consumer lock ───────────
            # The whole drain→flush→commit sequence is one critical section:
            # commit is position-based, so uncommitted reads must never be
            # held while another task (demand flush) commits.
            try:
                async with _queue_consumer.lock:
                    batch: list[dict] = []
                    seen_symbols: set[str] = set()

                    def _absorb(items: list[dict]) -> None:
                        for payload in items:
                            symbol = payload.get("symbol")
                            if symbol and symbol not in seen_symbols:
                                seen_symbols.add(symbol)
                                batch.append(payload)

                    _absorb(await _queue_consumer.drain(
                        batch_size, timeout_ms=int(_IDLE_POLL_INTERVAL * 1000)
                    ))

                    if batch:
                        # First item arrived — wait up to the batch window for
                        # the rest, then fire one Gemini call for the group.
                        window_deadline = monotonic() + window
                        while (
                            len(batch) < batch_size
                            and monotonic() < window_deadline
                            and not shutdown.is_set()
                        ):
                            _absorb(await _queue_consumer.drain(
                                batch_size - len(batch), timeout_ms=1_000
                            ))

                        await _flush_batch(batch, redis, session_factory)

                    # Commit AFTER the flush (crash mid-flush → redelivery, the
                    # LPOP-loss fix).  Also commits past stale/malformed skips
                    # when the batch itself came up empty.
                    await _queue_consumer.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "forecast_batch_worker: consumer error — %s (reconnecting)", exc
                )
                await _queue_consumer.stop()
                await _interruptible_sleep(shutdown, _IDLE_POLL_INTERVAL)
                continue

            if not batch:
                await _interruptible_sleep(shutdown, _IDLE_POLL_INTERVAL)

    except asyncio.CancelledError:
        logger.info("forecast_batch_worker: cancelled")
        raise
    except Exception as exc:
        logger.error(
            "forecast_batch_worker: unhandled error — %s", exc, exc_info=True
        )
        raise
    finally:
        await _queue_consumer.stop()

    logger.info("forecast_batch_worker: stopped")


# ── Demand-driven dispatch (admin/safety-net entry points) ─────────────────────

async def pending_forecast_count() -> int:
    """Current forecast queue depth from consumer lag (Worker Control Panel)."""
    return await pending_count(KafkaTopics.FORECAST_BATCH, KafkaGroups.FORECAST_BATCH)


async def flush_pending_forecasts(
    redis: Any,
    session_factory: async_sessionmaker,
) -> dict[str, int]:
    """
    Drain the forecast topic to a snapshot of its end offset right now,
    regardless of FORECAST_AUTO_DISPATCH.  Called by an explicit admin
    dispatch or the daily safety net.  ``redis`` is used only for the result
    cache writes inside ``_flush_batch``.

    Reads NEWS_FORECAST_BATCH_SIZE items at a time and fires one Gemini call
    per group, committing per successful group.  Stops looping — and raises —
    the moment a batch comes back budget_throttled or error, so a quota
    outage during the drain fails loudly instead of silently swallowing the
    remaining queue.  The failed group's offsets are NOT committed and the
    consumer is rewound to the committed offset, so the remainder stays
    pending for the next dispatch (strictly better than the old LPOP path,
    which lost the popped items).

    Returns:
        {"dispatched": N, "calls_made": M} on full success.

    Raises:
        RuntimeError: if any batch's outcome is "budget_throttled" or "error".
            The caller (worker sidecar router) maps this to HTTP 502.
    """
    batch_size = get_settings().NEWS_FORECAST_BATCH_SIZE
    dispatched = 0
    calls_made = 0

    async with _queue_consumer.lock:
        consumer = await _queue_consumer.ensure_started()
        tp = ForecastQueueConsumer._TP
        end_snapshot = (await consumer.end_offsets([tp]))[tp]

        while (await consumer.position(tp)) < end_snapshot:
            items = await _queue_consumer.drain(batch_size, timeout_ms=2_000)

            batch: list[dict] = []
            seen_symbols: set[str] = set()
            for payload in items:
                symbol = payload.get("symbol")
                if symbol and symbol not in seen_symbols:
                    seen_symbols.add(symbol)
                    batch.append(payload)

            if not batch:
                # Drained only stale/malformed payloads (or nothing) — commit
                # past them and re-check the position against the snapshot.
                await _queue_consumer.commit()
                if not items and (await consumer.position(tp)) < end_snapshot:
                    # Broker returned nothing despite lag — avoid a hot loop.
                    break
                continue

            dispatched += len(batch)
            result = await _flush_batch(batch, redis, session_factory)
            calls_made += 1

            if result["outcome"] in ("budget_throttled", "error"):
                # Do NOT commit this group; rewind so the shared consumer's
                # in-memory position matches the committed offset and the
                # remainder is redelivered on the next dispatch.
                await _queue_consumer.rewind_to_committed()
                raise RuntimeError(
                    f"forecast dispatch stopped early: batch outcome={result['outcome']} "
                    f"(valid_count={result['valid_count']}/{result['total']}); "
                    f"{dispatched} items drained, {calls_made} Gemini calls made so far"
                )

            await _queue_consumer.commit()

    return {"dispatched": dispatched, "calls_made": calls_made}


# ── Internal helpers ───────────────────────────────────────────────────────────

async def _interruptible_sleep(shutdown: asyncio.Event, secs: float) -> None:
    """Sleep for ``secs`` but wake immediately if ``shutdown`` is set."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=secs)
    except asyncio.TimeoutError:
        pass


async def _flush_batch(
    batch: list[dict],
    redis: Any,
    session_factory: async_sessionmaker,
) -> dict[str, Any]:
    """Build a multi-symbol prompt, call Gemini once, validate each result,
    write valid forecasts to the cache, and emit one audit log row.

    Returns:
        {"outcome": str, "valid_count": int, "total": int} — outcome is one
        of "success" | "validation_partial" | "budget_throttled" | "error".
        Callers draining multiple batches (flush_pending_forecasts) use
        "budget_throttled"/"error" to stop early rather than keep burning
        quota into a confirmed-exhausted or failing call.
    """
    settings = get_settings()
    client   = get_intelligence_client()

    prompt_text, valid_symbols = build_batch_forecast_prompt(batch)
    if not valid_symbols:
        return {"outcome": "error", "valid_count": 0, "total": 0}

    n = len(valid_symbols)
    news_forecast_batch_size_histogram.observe(n)
    invocation_id = uuid4()
    t0            = monotonic()

    # Scale timeout with batch size; give each symbol at least GEMINI_FORECAST_TIMEOUT.
    call_timeout = settings.GEMINI_FORECAST_TIMEOUT * n

    try:
        out, usage = await asyncio.wait_for(
            client.generate_structured_with_usage(
                prompt=prompt_text,
                response_model=NewsForecastBatchOutput,
                system=_BATCH_FORECAST_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=_MAX_OUTPUT_TOKENS,
                priority=Priority.LOW,
            ),
            timeout=call_timeout,
        )
    except (GeminiQuotaExhausted, GeminiBudgetThrottled) as exc:
        logger.warning(
            "forecast_batch_worker: quota/budget throttled — dropping batch of %d: %s",
            n, exc,
        )
        news_forecast_batch_calls_total.labels(outcome="budget_throttled").inc()
        return {"outcome": "budget_throttled", "valid_count": 0, "total": n}
    except (GeminiRateLimitError, asyncio.TimeoutError) as exc:
        logger.warning(
            "forecast_batch_worker: transient error for batch of %d — %s",
            n, type(exc).__name__,
        )
        news_forecast_batch_calls_total.labels(outcome="error").inc()
        return {"outcome": "error", "valid_count": 0, "total": n}
    except Exception as exc:
        logger.error(
            "forecast_batch_worker: unexpected error for batch of %d — %s",
            n, exc, exc_info=True,
        )
        news_forecast_batch_calls_total.labels(outcome="error").inc()
        return {"outcome": "error", "valid_count": 0, "total": n}

    latency_ms = int((monotonic() - t0) * 1000)

    # ── Per-item validation and cache write ───────────────────────────────────
    symbol_to_payload = {p["symbol"]: p for p in batch if "symbol" in p}
    valid_count       = 0

    for item in out.forecasts or []:
        if not _validate_item(item, symbol_to_payload):
            continue

        payload = symbol_to_payload[item.symbol]
        result  = _build_result(item, payload, usage, client)

        try:
            await redis.setex(
                payload["cache_key"],
                settings.NEWS_FORECAST_CACHE_TTL,
                json.dumps(result, default=str),
            )
            valid_count += 1
        except Exception as exc:
            logger.debug(
                "forecast_batch_worker: cache write failed for %s — %s",
                item.symbol, exc,
            )

    # ── Outcome metrics ───────────────────────────────────────────────────────
    if valid_count == n:
        outcome = "success"
    elif valid_count > 0:
        outcome = "validation_partial"
    else:
        outcome = "error"

    news_forecast_batch_calls_total.labels(outcome=outcome).inc()
    logger.info(
        "forecast_batch_worker: batch complete — %d/%d valid, latency=%dms",
        valid_count, n, latency_ms,
    )

    await _write_audit(
        session_factory=session_factory,
        invocation_id=invocation_id,
        model_id=(usage or {}).get("model_id", client.model_id),
        latency_ms=latency_ms,
        usage=usage,
        valid_count=valid_count,
        total=n,
    )

    return {"outcome": outcome, "valid_count": valid_count, "total": n}


def _validate_item(item: Any, symbol_to_payload: dict[str, dict]) -> bool:
    """Return True only if the item passes all safety checks."""
    try:
        if not isinstance(item.symbol, str) or item.symbol not in symbol_to_payload:
            return False
        if item.direction not in ("BUY", "SELL", "HOLD"):
            return False
        if not (0.0 <= float(item.confidence) <= 1.0):
            return False
        if "cache_key" not in symbol_to_payload[item.symbol]:
            return False
        return True
    except Exception:
        return False


def _build_result(
    item: Any,
    payload: dict,
    usage: dict | None,
    client: Any,
) -> dict[str, Any]:
    """Build the result dict in the same shape signal_assembler's cache reads."""
    events = payload.get("events") or []
    return {
        "score":           score_from_direction(item.direction, item.confidence),
        "confidence":      float(item.confidence),
        "available":       True,
        "events":          events,
        "event_count":     len(events),
        "direction":       item.direction,
        "rationale":       item.rationale,
        "forecast_source": "gemini_batch",
        "model":           (usage or {}).get("model_id", client.model_id),
    }


async def _write_audit(
    *,
    session_factory: async_sessionmaker,
    invocation_id: Any,
    model_id: str,
    latency_ms: int,
    usage: dict | None,
    valid_count: int,
    total: int,
) -> None:
    """Append one ai_llm_audit_log row for governance.  Never raises."""
    provider, _, mid = (model_id or "gemini/").partition("/")
    preview = f"batch_forecast valid={valid_count}/{total}"
    try:
        async with session_factory() as db:
            db.add(AILLMAuditLog(
                invocation_id=invocation_id,
                invocation_type="news_forecast_batch",
                reference_table="ai_trading_signals",
                reference_id=None,
                model_provider=provider or "gemini",
                model_id=mid or model_id,
                prompt_hash=f"batch_{total}",
                retrieved_source_ids=None,
                input_tokens=(usage or {}).get("input_tokens"),
                output_tokens=(usage or {}).get("output_tokens"),
                latency_ms=latency_ms,
                guardrail_events=[],
                output_preview=preview,
                error_message=None,
            ))
            await db.commit()
        llm_audit_log_writes_total.labels(status="success").inc()
    except Exception as exc:
        llm_audit_log_writes_total.labels(status="failure").inc()
        logger.error("forecast_batch_worker: audit write failed — %s", exc, exc_info=True)
