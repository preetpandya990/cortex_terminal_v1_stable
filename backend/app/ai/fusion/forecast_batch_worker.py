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
  LPUSH  cortex:forecast:batch:queue  <json payload>
  SET    cortex:forecast:batch:dedup:{cache_key}  1  NX  EX 600
  LPOP   cortex:forecast:batch:queue  COUNT N       (by this worker, non-blocking)
"""
from __future__ import annotations

import asyncio
import json
import logging
from time import monotonic
from typing import Any
from uuid import uuid4

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
from app.core.metrics import (
    llm_audit_log_writes_total,
    news_forecast_batch_calls_total,
    news_forecast_batch_size_histogram,
    news_forecast_queue_depth,
)

logger = logging.getLogger(__name__)

_QUEUE_KEY   = "cortex:forecast:batch:queue"
_DEDUP_PREFIX = "cortex:forecast:batch:dedup:"

# Generous token ceiling — 150 tokens × 5 symbols + 33% headroom.
# Prevents the structured-output silent-None bug caused by token exhaustion.
_MAX_OUTPUT_TOKENS = 1_000

# Seconds between queue polls when the queue is empty.
_IDLE_POLL_INTERVAL = 2.0


async def forecast_batch_loop(
    redis: Any,
    session_factory: async_sessionmaker,
    shutdown: asyncio.Event,
) -> None:
    """Supervised entry-point — runs until ``shutdown`` is set."""
    logger.info("forecast_batch_worker: starting")

    batch:          list[dict] = []
    seen_symbols:   set[str]   = set()
    window_deadline: float | None = None

    try:
        while not shutdown.is_set():
            settings   = get_settings()
            batch_size = settings.NEWS_FORECAST_BATCH_SIZE
            window     = settings.NEWS_FORECAST_BATCH_WINDOW_SECS

            # ── Non-blocking drain ────────────────────────────────────────────
            # When FORECAST_AUTO_DISPATCH is False, never LPOP — items stay
            # queued in Redis (durable, unlike the sentiment queue) until an
            # explicit dispatch via flush_pending_forecasts() drains them.
            # Still poll LLEN below so the pending-count gauge stays live.
            if settings.FORECAST_AUTO_DISPATCH:
                needed = batch_size - len(batch)
                raw_items: list[bytes] | None = None
                try:
                    raw_items = await redis.lpop(_QUEUE_KEY, count=needed)
                except Exception as exc:
                    logger.warning("forecast_batch_worker: Redis lpop failed — %s", exc)
                    await _interruptible_sleep(shutdown, _IDLE_POLL_INTERVAL)
                    continue

                if raw_items:
                    if window_deadline is None:
                        window_deadline = monotonic() + window
                    for raw in raw_items:
                        try:
                            payload = json.loads(raw)
                            symbol  = payload.get("symbol")
                            if symbol and symbol not in seen_symbols:
                                seen_symbols.add(symbol)
                                batch.append(payload)
                        except Exception:
                            pass

            # ── Update queue-depth gauge ──────────────────────────────────────
            try:
                depth = await redis.llen(_QUEUE_KEY)
                news_forecast_queue_depth.set(depth)
            except Exception:
                pass

            if not settings.FORECAST_AUTO_DISPATCH:
                await _interruptible_sleep(shutdown, _IDLE_POLL_INTERVAL)
                continue

            # ── Flush decision ────────────────────────────────────────────────
            batch_full      = len(batch) >= batch_size
            window_expired  = window_deadline is not None and monotonic() >= window_deadline

            if (batch_full or window_expired) and batch:
                await _flush_batch(batch, redis, session_factory)
                batch          = []
                seen_symbols   = set()
                window_deadline = None
            else:
                # Sleep briefly; wake early if shutdown fires.
                if window_deadline is not None:
                    remaining = window_deadline - monotonic()
                    sleep_for = min(_IDLE_POLL_INTERVAL, max(0.1, remaining))
                else:
                    sleep_for = _IDLE_POLL_INTERVAL
                await _interruptible_sleep(shutdown, sleep_for)

    except asyncio.CancelledError:
        logger.info("forecast_batch_worker: cancelled")
        raise
    except Exception as exc:
        logger.error(
            "forecast_batch_worker: unhandled error — %s", exc, exc_info=True
        )
        raise

    logger.info("forecast_batch_worker: stopped")


# ── Demand-driven dispatch (admin/safety-net entry points) ─────────────────────

async def pending_forecast_count(redis: Any) -> int:
    """Current forecast batch queue depth (for the Worker Control Panel)."""
    return await redis.llen(_QUEUE_KEY)


async def flush_pending_forecasts(
    redis: Any,
    session_factory: async_sessionmaker,
) -> dict[str, int]:
    """
    Drain the entire forecast batch queue right now, regardless of
    FORECAST_AUTO_DISPATCH.  Called by an explicit admin dispatch or the
    daily safety net.

    Pops NEWS_FORECAST_BATCH_SIZE items at a time and fires one Gemini call
    per popped group.  Stops looping — and raises — the moment a batch comes
    back budget_throttled or error, so a quota outage during the drain fails
    loudly instead of silently swallowing the remaining queue.

    Returns:
        {"dispatched": N, "calls_made": M} on full success.

    Raises:
        RuntimeError: if any batch's outcome is "budget_throttled" or "error".
            The caller (worker sidecar router) maps this to HTTP 502.
    """
    batch_size = get_settings().NEWS_FORECAST_BATCH_SIZE
    dispatched = 0
    calls_made = 0

    while True:
        raw_items = await redis.lpop(_QUEUE_KEY, count=batch_size)
        if not raw_items:
            break

        batch: list[dict] = []
        seen_symbols: set[str] = set()
        for raw in raw_items:
            try:
                payload = json.loads(raw)
                symbol  = payload.get("symbol")
                if symbol and symbol not in seen_symbols:
                    seen_symbols.add(symbol)
                    batch.append(payload)
            except Exception:
                pass

        if not batch:
            continue

        dispatched += len(batch)
        result = await _flush_batch(batch, redis, session_factory)
        calls_made += 1

        if result["outcome"] in ("budget_throttled", "error"):
            raise RuntimeError(
                f"forecast dispatch stopped early: batch outcome={result['outcome']} "
                f"(valid_count={result['valid_count']}/{result['total']}); "
                f"{dispatched} items drained, {calls_made} Gemini calls made so far"
            )

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
