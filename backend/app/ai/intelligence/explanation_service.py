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
  failed      a republish attempt could not be enqueued (broker down) — the
              panel shows PanelFailed with a retry button instead of an
              eternal skeleton.

Mode behavior:
  EXPLANATION_ON_DEMAND=False (legacy): the engine auto-publishes jobs at
  suggestion creation. That publish is deliberately non-fatal — a broker
  hiccup must never roll back a committed suggestion — so it can silently
  fail and orphan the suggestion. This service does NOT blindly trust the
  engine's attempt succeeded: once a suggestion is older than
  EXPLANATION_RECONCILE_STALENESS_SECS with no explanation, it self-heals by
  republishing (same claim-and-publish path as demand mode, trigger=
  "reconciliation"). The periodic ExplanationReconciliationSweep task
  (backend/app/workers/explanation_reconciliation_sweep.py) is the backstop
  for suggestions nobody views before the next sweep tick.

  EXPLANATION_ON_DEMAND=True: demand IS the gate. The first viewer of an
  unexplained active suggestion publishes a demand job (deduplicated by a
  Redis SET NX lock shared with concurrent viewers) and everyone sees
  "generating" until the worker's ready/failed push lands.

Both paths funnel through the same EXPLANATION_INFLIGHT_KEY (SET NX) so a
worker attempt, a demand-mode first view, and a legacy self-heal/sweep
republish can never race each other into a duplicate publish.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.core.kafka import KafkaTopics, publish as kafka_publish
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)

# In-flight lock shared by every path that can publish an explanation job for
# a given suggestion: demand-mode first view, legacy-mode self-heal-on-read,
# the periodic reconciliation sweep, and the worker itself (which refreshes
# it on every attempt — see explanation_worker.py). TTL comfortably covers
# the worker's retry pacing (_RETRY_DELAY_SECS + LLM_CALL_TIMEOUT_SECS) plus
# margin to the next attempt; the worker re-sets it every attempt so it never
# lapses mid-retry, and deletes it on every terminal outcome.
EXPLANATION_INFLIGHT_KEY = "cortex:explanation:inflight:{suggestion_id}"
EXPLANATION_INFLIGHT_TTL_SECS = 200


async def publish_explanation_job(
    suggestion_id: str,
    suggestion_db_id: int | str,
    instrument_key: str,
    *,
    trigger: str,
) -> None:
    """
    Publish one explanation job to cortex.explanation.jobs.

    ``trigger`` ∈ {"auto", "demand", "bypass", "reconciliation"} — carried in
    the payload so the worker applies the right retry policy (demand jobs
    fail fast: a user is watching a skeleton). Additive field; the consumer's
    .get() parsing ignores it on old workers.
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


async def claim_and_publish(
    redis: Any,
    suggestion: TradeSuggestion,
    *,
    trigger: str,
    log_context: str,
) -> dict[str, Any]:
    """
    Shared claim-and-publish sequence for every path that may need to enqueue
    an explanation job for a suggestion that doesn't have one yet: SET NX the
    in-flight lock, publish on success, release the lock and return "failed"
    if the publish itself errors. Callers differ only in ``trigger`` (which
    also drives the worker's retry policy) and log wording.

    The returned dict always carries ``"published"`` (bool) alongside
    ``"status"`` — two of the three outcomes return ``status="generating"``
    (lock already held vs. this call just published), and callers that need
    to distinguish "actually did something" from "someone else already has
    it" (e.g. the reconciliation sweep's republish count) must check
    ``"published"``, not ``"status"``.
    """
    suggestion_id = str(suggestion.suggestion_id)
    inflight_key = EXPLANATION_INFLIGHT_KEY.format(suggestion_id=suggestion_id)

    try:
        acquired = await redis.set(
            inflight_key, "1", nx=True, ex=EXPLANATION_INFLIGHT_TTL_SECS
        )
    except Exception as exc:
        # Redis down: publish anyway — the worker's own in-flight key and DB
        # idempotency check make a duplicate job harmless, while NOT
        # publishing would leave the user on an eternal skeleton.
        logger.warning(
            "explanation_service: %s in-flight check failed for %s "
            "(publishing anyway): %s",
            log_context, suggestion_id, exc,
        )
        acquired = True

    if not acquired:
        return {"status": "generating", "published": False, "suggestion_id": suggestion_id}

    try:
        await publish_explanation_job(
            suggestion_id, suggestion.id, suggestion.instrument_key, trigger=trigger
        )
        logger.info(
            "explanation_service: %s job published for suggestion %s (%s)",
            log_context, suggestion_id, suggestion.symbol,
        )
        return {"status": "generating", "published": True, "suggestion_id": suggestion_id}
    except Exception as exc:
        # Free the lock so the next viewer/sweep can retry, and give the UI a
        # terminal failed state rather than a skeleton that never resolves.
        try:
            await redis.delete(inflight_key)
        except Exception:
            pass
        logger.error(
            "explanation_service: %s job publish failed for %s: %s",
            log_context, suggestion_id, exc,
        )
        return {"status": "failed", "published": False, "suggestion_id": suggestion_id}


async def ensure_explanation(redis: Any, suggestion: TradeSuggestion) -> dict[str, Any]:
    """
    Classify a suggestion's explanation state; guarantee a generation job is
    in flight for unexplained active suggestions (on-demand: on first view;
    legacy: once the engine's original auto-publish attempt looks stale).

    Returns ``{"status": ..., "suggestion_id": ...}`` — callers merge this
    into their payload shape. Never raises: enqueue failures degrade to
    ``status="failed"`` so the UI gets a terminal answer.
    """
    suggestion_id = str(suggestion.suggestion_id)

    if suggestion.llm_summary is not None:
        return {"status": "ready", "suggestion_id": suggestion_id}

    settings = get_settings()

    if not settings.EXPLANATION_ON_DEMAND:
        # Legacy mode: the engine attempted to auto-publish at creation time.
        if float(suggestion.consensus_score) < settings.EXPLANATION_CONSENSUS_THRESHOLD:
            return {"status": "weak_signal", "suggestion_id": suggestion_id}

        age_secs = (
            datetime.now(timezone.utc) - suggestion.created_at
        ).total_seconds()
        if age_secs < settings.EXPLANATION_RECONCILE_STALENESS_SECS:
            return {"status": "generating", "suggestion_id": suggestion_id}

        # Past the staleness window with no explanation — the original
        # auto-publish may have failed (or is still legitimately retrying;
        # claim_and_publish's SET NX against EXPLANATION_INFLIGHT_KEY, which
        # the worker refreshes every attempt, is what actually prevents a
        # duplicate publish in that case, not this age check alone).
        logger.warning(
            "explanation_service: legacy suggestion %s stale (age=%.0fs >= "
            "%ds) with no explanation — attempting self-heal republish",
            suggestion_id, age_secs, settings.EXPLANATION_RECONCILE_STALENESS_SECS,
        )
        result = await claim_and_publish(
            redis, suggestion, trigger="reconciliation", log_context="self-heal",
        )
        if result.get("published"):
            from app.core.metrics import explanation_reconciliation_republish_total
            explanation_reconciliation_republish_total.labels(
                trigger_source="self_heal"
            ).inc()
        return result

    # ── On-demand mode: first view triggers generation ─────────────────────
    return await claim_and_publish(
        redis, suggestion, trigger="demand", log_context="demand",
    )
