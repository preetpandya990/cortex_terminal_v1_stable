"""
Explanation Service — demand-side state machine (WS7)
=====================================================
Single authority for "does this suggestion have an explanation, and if not,
what happens next?" — delegated to by the SSE Stage-1 lookup, the REST
fallback endpoint, and the user bypass endpoint, so the three entry points
cannot drift.

States returned by ensure_explanation():
  ready       explanation exists — serve it.
  generating  a generation job is in flight (just published, or another
              caller/worker already holds the in-flight lock).
  weak_signal legacy mode only: the engine skipped auto-publish because
              consensus_score < EXPLANATION_CONSENSUS_THRESHOLD; the panel
              shows the user-driven "Request AI Explanation" button.
  failed      the demand job could not be enqueued (broker down) — the panel
              shows PanelFailed with a retry button instead of an eternal
              skeleton.

Mode behavior:
  EXPLANATION_ON_DEMAND=False (legacy): the engine auto-publishes jobs at
  suggestion creation; this service never publishes — it only classifies
  state (ready / generating / weak_signal).

  EXPLANATION_ON_DEMAND=True: demand IS the gate. The first viewer of an
  unexplained active suggestion publishes a demand job (deduplicated by a
  Redis SET NX lock shared with concurrent viewers) and everyone sees
  "generating" until the worker's ready/failed push lands.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import get_settings
from app.core.kafka import KafkaTopics, publish as kafka_publish
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)

# Demand-trigger dedup: concurrent first viewers of the same suggestion (two
# tabs, SSE + REST race) must produce exactly one job. TTL matches the
# worker's own in-flight window (LLM ceiling + DB write headroom); the worker
# clears it on terminal outcomes via its ready/failed push.
DEMAND_INFLIGHT_KEY = "cortex:explanation:demand:inflight:{suggestion_id}"
DEMAND_INFLIGHT_TTL_SECS = 150


async def publish_explanation_job(
    suggestion_id: str,
    suggestion_db_id: int | str,
    instrument_key: str,
    *,
    trigger: str,
) -> None:
    """
    Publish one explanation job to cortex.explanation.jobs.

    ``trigger`` ∈ {"auto", "demand", "bypass"} — carried in the payload so the
    worker applies the right retry policy (demand jobs fail fast: a user is
    watching a skeleton). Additive field; the consumer's .get() parsing
    ignores it on old workers.
    """
    await kafka_publish(
        KafkaTopics.EXPLANATION_JOBS,
        {
            "suggestion_id":  str(suggestion_id),
            "id":             str(suggestion_db_id),
            "instrument_key": instrument_key,
            "trigger":        trigger,
        },
        key=str(suggestion_id),
    )


async def ensure_explanation(redis: Any, suggestion: TradeSuggestion) -> dict[str, Any]:
    """
    Classify a suggestion's explanation state; in on-demand mode, guarantee a
    generation job is in flight for unexplained active suggestions.

    Returns ``{"status": ..., "suggestion_id": ...}`` — callers merge this
    into their payload shape. Never raises: enqueue failures degrade to
    ``status="failed"`` so the UI gets a terminal answer.
    """
    suggestion_id = str(suggestion.suggestion_id)

    if suggestion.llm_summary is not None:
        return {"status": "ready", "suggestion_id": suggestion_id}

    settings = get_settings()

    if not settings.EXPLANATION_ON_DEMAND:
        # Legacy mode: the engine already decided at creation time.
        if float(suggestion.consensus_score) < settings.EXPLANATION_CONSENSUS_THRESHOLD:
            return {"status": "weak_signal", "suggestion_id": suggestion_id}
        return {"status": "generating", "suggestion_id": suggestion_id}

    # ── On-demand mode: first view triggers generation ─────────────────────────
    inflight_key = DEMAND_INFLIGHT_KEY.format(suggestion_id=suggestion_id)
    try:
        acquired = await redis.set(
            inflight_key, "1", nx=True, ex=DEMAND_INFLIGHT_TTL_SECS
        )
    except Exception as exc:
        # Redis down: publish anyway — the worker's own in-flight key and DB
        # idempotency check make a duplicate job harmless, while NOT
        # publishing would leave the user on an eternal skeleton.
        logger.warning(
            "explanation_service: demand in-flight check failed for %s "
            "(publishing anyway): %s",
            suggestion_id, exc,
        )
        acquired = True

    if not acquired:
        return {"status": "generating", "suggestion_id": suggestion_id}

    try:
        await publish_explanation_job(
            suggestion_id, suggestion.id, suggestion.instrument_key, trigger="demand"
        )
        logger.info(
            "explanation_service: demand job published for suggestion %s (%s)",
            suggestion_id, suggestion.symbol,
        )
        return {"status": "generating", "suggestion_id": suggestion_id}
    except Exception as exc:
        # Free the lock so the next viewer can retry, and give the UI a
        # terminal failed state rather than a skeleton that never resolves.
        try:
            await redis.delete(inflight_key)
        except Exception:
            pass
        logger.error(
            "explanation_service: demand job publish failed for %s: %s",
            suggestion_id, exc,
        )
        return {"status": "failed", "suggestion_id": suggestion_id}
