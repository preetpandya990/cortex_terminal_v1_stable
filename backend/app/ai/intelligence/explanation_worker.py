"""
LLM Explanation Worker
======================
Async background task that generates plain-English trade explanations for every
new suggestion committed to the database.

Pipeline per suggestion
-----------------------
  1. Receive {suggestion_id, id} from Redis channel cortex:llm:explanation:pending
  2. Load TradeSuggestion from DB
  3. RAG retrieve — top-k news chunks for the symbol in the last 24 hours
  4. Build a structured prompt from signal data + retrieved context
  5. LLM structured generation → ExplanationOutput (Instructor + Pydantic)
  6. Apply output guardrails (disclaimer injection, price-prediction filter,
     citation check)
  7. Write llm_summary + llm_explanation to trade_suggestions
  8. Append one row to ai_llm_audit_log (includes success and failure cases)
  9. Publish cortex:llm:explanation:ready:{suggestion_id} for the SSE stream

Design invariants
-----------------
  - A failed explanation never blocks or retries indefinitely.  Each suggestion
    gets at most MAX_ATTEMPTS attempts; after that the audit log records the
    failure and the frontend shows no explanation rather than a stale skeleton.
  - The worker runs as a single asyncio task; it is not concurrent.  Suggestions
    are processed one at a time.  At Cortex's current signal volume (< 50/day)
    this is sufficient; introduce a semaphore-bounded pool when volume scales.
  - Every LLM inference — success or failure — writes one ai_llm_audit_log row.
    This is a non-negotiable governance requirement (SR 11-7).
  - The worker reconnects automatically on Redis errors (same pattern as
    cai_redis_listener and suggestions_redis_listener).

Guardrails (CORTEX_LLM_UPGRADE_PLAN.md §8.1)
---------------------------------------------
  Disclaimer injection  Always appended to full_explanation.
  No price predictions  Regex filter on output; violating sentences are removed
                        and the event is recorded in guardrail_events.
  Citation check        If RAG returned context, the explanation must contain at
                        least one "[Source:" reference.  Violations are logged as
                        guardrail events but do NOT suppress the explanation —
                        they flag a quality issue for review.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AILLMAuditLog
from app.ai.intelligence.llm_client import (
    LLMFallbackExhausted,
    get_intelligence_client,
)
from app.ai.rag.pipeline import build_retrieval_source_refs, format_context, retrieve
from app.core.metrics import (
    llm_audit_log_writes_total,
    llm_explanation_duration_seconds,
    llm_explanations_total,
    llm_guardrail_events_total,
)
from app.core.redis import RedisChannels
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

MAX_ATTEMPTS = 2
_RECONNECT_DELAY_SECS = 5

# ── Output schema ──────────────────────────────────────────────────────────────

class ExplanationOutput(BaseModel):
    """Structured explanation produced by the LLM for a trade suggestion."""
    summary: str = Field(
        min_length=20,
        max_length=600,
        description=(
            "2–3 sentence plain-English summary suitable for inline display. "
            "Must be grounded in the retrieved news context."
        ),
    )
    full_explanation: str = Field(
        min_length=50,
        description=(
            "Full narrative explanation covering: (1) what the signal detected, "
            "(2) supporting news evidence with inline citations, "
            "(3) key risk factors. "
            "Cite sources inline as: According to [Source Name, YYYY-MM-DD]..."
        ),
    )
    sources_used: list[str] = Field(
        default_factory=list,
        description="List of source names explicitly cited in full_explanation.",
    )


# ── System prompt (per CORTEX_LLM_UPGRADE_PLAN.md §8.2) ──────────────────────

_EXPLANATION_SYSTEM_PROMPT = """\
You are a financial signal analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: generate a concise, factual explanation for a machine-generated trade signal,
grounded exclusively in the retrieved news articles provided.

Mandatory rules:
1. BASE ALL CLAIMS on the retrieved news context provided in the prompt.
   Do not invent facts, prices, or events not present in the context.
2. CITE EVERY FACTUAL CLAIM inline: According to [Source Name, YYYY-MM-DD]...
   If no context is provided, state that clearly rather than inventing sources.
3. PROHIBITED language (these will be filtered):
   - Price predictions: "will reach ₹X", "target price", "price target"
   - Guarantees: "guaranteed", "certain to", "will definitely"
   - Advisory language: "you should buy/sell", "recommend buying", "buy now"
4. ALLOWED: describe what the signal detected, what the news says, what the risk is.
5. DISCLAIMER: The system will automatically append the required regulatory disclaimer.
   Do NOT add your own disclaimer — it will duplicate the injected one.
6. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
   No markdown fences, no extra keys.\
"""

# ── Guardrail patterns ────────────────────────────────────────────────────────

_PRICE_PREDICTION_RE = re.compile(
    r"will\s+reach\s+[₹$₨\d]"
    r"|price\s+target"
    r"|target\s+price"
    r"|guaranteed\s+(return|profit|gain)"
    r"|certain\s+to\s+(rise|fall|go)"
    r"|will\s+definitely\s+(rise|fall|go|increase|decrease)",
    re.IGNORECASE,
)

_REGULATORY_DISCLAIMER = (
    "\n\n⚠ This is AI-generated analysis for informational purposes only "
    "and does not constitute financial advice. Past signal performance does "
    "not guarantee future results. Always conduct your own due diligence."
)


# ── Guardrail application ─────────────────────────────────────────────────────

def _apply_guardrails(
    output: ExplanationOutput,
    has_context: bool,
) -> tuple[ExplanationOutput, list[str]]:
    """
    Apply all output guardrails.  Returns the (possibly modified) output and
    a list of guardrail event names that fired.

    Guardrails:
      - disclaimer_injection  Always appended (not a violation).
      - price_prediction_filter  Removes violating sentences; recorded as event.
      - citation_check  Logs a warning if context was provided but no citation
                        found; does NOT suppress the explanation.
    """
    events: list[str] = []

    # 1. Price-prediction filter — remove violating sentences
    sentences = re.split(r"(?<=[.!?])\s+", output.full_explanation)
    clean_sentences: list[str] = []
    for sentence in sentences:
        if _PRICE_PREDICTION_RE.search(sentence):
            events.append("price_prediction_filter")
            logger.warning(
                "explanation_worker: price-prediction guardrail removed sentence: %.80s...",
                sentence,
            )
        else:
            clean_sentences.append(sentence)
    full_explanation = " ".join(clean_sentences)

    # Apply the same filter to the summary
    summary_sentences = re.split(r"(?<=[.!?])\s+", output.summary)
    clean_summary = " ".join(
        s for s in summary_sentences if not _PRICE_PREDICTION_RE.search(s)
    )
    if not clean_summary.strip():
        clean_summary = output.summary  # keep original if everything was stripped

    # 2. Citation check — log if context was provided but output has no citation
    if has_context and "[" not in full_explanation:
        events.append("citation_missing")
        logger.warning(
            "explanation_worker: citation guardrail — context was provided but "
            "no [Source] citation found in full_explanation"
        )

    # 3. Disclaimer injection — always appended
    full_explanation_with_disclaimer = full_explanation.rstrip() + _REGULATORY_DISCLAIMER

    return (
        ExplanationOutput(
            summary=clean_summary or output.summary,
            full_explanation=full_explanation_with_disclaimer,
            sources_used=output.sources_used,
        ),
        events,
    )


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_explanation_prompt(
    suggestion: TradeSuggestion,
    context: str,
) -> str:
    """
    Render the explanation prompt from suggestion signal data + RAG context.
    All values are taken from the committed DB row — no inference, no guessing.
    """
    ml = suggestion.ml_signal or {}
    ai = suggestion.ai_signal or {}

    lines = [
        "## Trade Signal Summary",
        f"Symbol:           {suggestion.symbol}",
        f"Direction:        {suggestion.signal_direction}",
        f"Consensus Score:  {float(suggestion.consensus_score):.1f}/100",
        f"Confidence:       {suggestion.confidence_level}",
    ]
    if suggestion.entry_price:
        lines.append(f"Entry Price:      ₹{float(suggestion.entry_price):.2f}")
    if suggestion.stop_loss:
        lines.append(f"Stop Loss:        ₹{float(suggestion.stop_loss):.2f}")
    if suggestion.risk_reward_ratio:
        lines.append(f"Risk/Reward:      {float(suggestion.risk_reward_ratio):.1f}x")
    if suggestion.time_horizon:
        lines.append(f"Time Horizon:     {suggestion.time_horizon}")
    if suggestion.regime_type:
        lines.append(f"Market Regime:    {suggestion.regime_type}")

    # ML signal details
    if ml.get("available"):
        lines.append("")
        lines.append("## ML Model Output")
        lines.append(f"ML Direction:     {ml.get('action', 'N/A')}")
        lines.append(f"ML Confidence:    {ml.get('confidence', 0.0):.2%}")

    # AI/Event signal details
    sentiment_label = ai.get("sentiment_label") or ai.get("sentiment")
    if sentiment_label:
        lines.append("")
        lines.append("## News Sentiment Signal")
        lines.append(f"Sentiment:        {sentiment_label}")
        event_count = ai.get("event_count") or len(ai.get("contributing_events", []))
        if event_count:
            lines.append(f"Contributing Events: {event_count}")

    if context:
        lines.append("")
        lines.append("## Retrieved News Context")
        lines.append(
            "(Use these articles as the ONLY factual basis for your explanation. "
            "Cite inline as: According to [Source Name, YYYY-MM-DD]...)"
        )
        lines.append(context)
    else:
        lines.append("")
        lines.append(
            "## Retrieved News Context\n"
            "No recent news articles were found for this symbol. "
            "Base your explanation on the quantitative signal data above only, "
            "and state clearly that no news context was available."
        )

    return "\n".join(lines)


# ── Audit log helper ──────────────────────────────────────────────────────────

async def _write_audit_entry(
    db: AsyncSession,
    *,
    invocation_id: UUID,
    invocation_type: str,
    reference_id: int | None,
    model_provider: str,
    model_id: str,
    prompt_hash: str,
    source_refs: list[dict] | None,
    input_tokens: int | None,
    output_tokens: int | None,
    latency_ms: int,
    guardrail_events: list[str],
    output_preview: str | None,
    error_message: str | None,
) -> None:
    """
    Append one row to ai_llm_audit_log.  Never raises — audit failures are
    logged at ERROR level but must not abort the calling pipeline.
    """
    try:
        entry = AILLMAuditLog(
            invocation_id=invocation_id,
            invocation_type=invocation_type,
            reference_table="trade_suggestions",
            reference_id=reference_id,
            model_provider=model_provider,
            model_id=model_id,
            prompt_hash=prompt_hash,
            retrieved_source_ids=source_refs,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            guardrail_events=guardrail_events,
            output_preview=output_preview[:500] if output_preview else None,
            error_message=error_message,
        )
        db.add(entry)
        await db.commit()
        llm_audit_log_writes_total.labels(status="success").inc()
    except Exception as exc:
        llm_audit_log_writes_total.labels(status="failure").inc()
        logger.error(
            "explanation_worker: failed to write audit log entry "
            "(governance violation — investigate immediately): %s",
            exc,
            exc_info=True,
        )


# ── Core explanation logic ────────────────────────────────────────────────────

async def _generate_explanation(
    suggestion_id: str,
    suggestion_db_id: int,
    db: AsyncSession,
) -> None:
    """
    Execute the full explanation pipeline for one suggestion.
    Raises on unrecoverable errors so the caller can track retry count.
    """
    from app.core.database import AsyncSessionLocal

    client = get_intelligence_client()
    invocation_id = uuid4()

    # ── Load suggestion ───────────────────────────────────────────────────────
    stmt = select(TradeSuggestion).where(
        TradeSuggestion.suggestion_id == UUID(suggestion_id)
    )
    result = await db.execute(stmt)
    suggestion = result.scalar_one_or_none()

    if suggestion is None:
        logger.warning(
            "explanation_worker: suggestion %s not found — skipping", suggestion_id
        )
        return

    if suggestion.status != "active":
        logger.debug(
            "explanation_worker: suggestion %s is %s — skipping",
            suggestion_id, suggestion.status,
        )
        return

    # ── RAG retrieval ─────────────────────────────────────────────────────────
    query = f"{suggestion.symbol} {suggestion.signal_direction} trading signal"
    try:
        chunks = await retrieve(db=db, query=query, symbol=suggestion.symbol)
    except Exception as exc:
        logger.warning(
            "explanation_worker: RAG retrieval failed for %s (continuing with no context): %s",
            suggestion_id, exc,
        )
        chunks = []

    context = format_context(chunks)
    source_refs = build_retrieval_source_refs(chunks)
    prompt = _build_explanation_prompt(suggestion, context)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()

    # ── LLM call ──────────────────────────────────────────────────────────────
    t0 = time.monotonic()
    error_message: str | None = None
    raw_output: ExplanationOutput | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Default to primary provider; overwritten with actual serving provider on success.
    model_provider = client._primary.value
    model_id = client._model_name(client._primary)

    try:
        raw_output, usage_info = await client.generate_structured_with_usage(
            prompt=prompt,
            response_model=ExplanationOutput,
            system=_EXPLANATION_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=1500,
        )
        input_tokens  = usage_info["input_tokens"]
        output_tokens = usage_info["output_tokens"]
        model_provider = usage_info["provider"]
        model_id       = usage_info["model_id"]
    except (LLMFallbackExhausted, Exception) as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: LLM call failed for suggestion %s: %s",
            suggestion_id, exc,
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    if error_message is None:
        llm_explanations_total.labels(status="success", provider=model_provider).inc()
        llm_explanation_duration_seconds.labels(provider=model_provider).observe(latency_ms / 1000.0)
    else:
        llm_explanations_total.labels(status="failure", provider=model_provider).inc()

    # ── Guardrails ────────────────────────────────────────────────────────────
    guardrail_events: list[str] = []
    final_output: ExplanationOutput | None = None

    if raw_output is not None:
        final_output, guardrail_events = _apply_guardrails(
            raw_output, has_context=bool(chunks)
        )
        for event in guardrail_events:
            llm_guardrail_events_total.labels(guardrail=event).inc()

    # ── Persist explanation to trade_suggestions ──────────────────────────────
    if final_output is not None:
        now_utc = datetime.now(timezone.utc)
        await db.execute(
            update(TradeSuggestion)
            .where(TradeSuggestion.suggestion_id == UUID(suggestion_id))
            .values(
                llm_summary=final_output.summary,
                llm_explanation=final_output.full_explanation,
                explanation_model=f"{model_provider}/{model_id}",
                explanation_generated_at=now_utc,
                updated_at=now_utc,
            )
        )
        await db.commit()

        logger.info(
            "explanation_worker: explanation written for suggestion %s "
            "symbol=%s latency_ms=%d guardrails=%s",
            suggestion_id,
            suggestion.symbol,
            latency_ms,
            guardrail_events or "none",
        )

    # ── Audit log ─────────────────────────────────────────────────────────────
    output_preview: str | None = None
    if final_output is not None:
        output_preview = final_output.summary[:500]

    await _write_audit_entry(
        db,
        invocation_id=invocation_id,
        invocation_type="explanation",
        reference_id=suggestion_db_id,
        model_provider=model_provider,
        model_id=model_id,
        prompt_hash=prompt_hash,
        source_refs=source_refs if source_refs else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        guardrail_events=guardrail_events,
        output_preview=output_preview,
        error_message=error_message,
    )

    # ── Publish ready notification ────────────────────────────────────────────
    if final_output is not None:
        try:
            from app.core.redis import get_redis
            ready_channel = RedisChannels.LLM_EXPLANATION_READY.format(
                suggestion_id=suggestion_id
            )
            # Include structured source refs so the SSE stream can render the
            # sources panel immediately without a follow-up DB query.
            sources_payload = [
                {
                    "source_name": chunk.source_name,
                    "as_of":       chunk.as_of_timestamp.isoformat(),
                    "source_url":  chunk.source_url,
                }
                for chunk in chunks
            ]
            payload = json.dumps({
                "suggestion_id": suggestion_id,
                "llm_summary":   final_output.summary,
                "model":         f"{model_provider}/{model_id}",
                "generated_at":  datetime.now(timezone.utc).isoformat(),
                "sources":       sources_payload,
            }, default=str)
            await get_redis().publish(ready_channel, payload)
        except Exception as exc:
            logger.warning(
                "explanation_worker: failed to publish ready notification for %s "
                "(non-fatal): %s",
                suggestion_id, exc,
            )

    # Raise if LLM failed so the caller can count retries
    if error_message is not None:
        raise RuntimeError(error_message)


# ── Worker task ───────────────────────────────────────────────────────────────

async def explanation_worker() -> None:
    """
    Persistent application-level background task.

    Subscribes to RedisChannels.LLM_EXPLANATION_PENDING and processes
    explanation requests sequentially.  Registered in main.py lifespan —
    one instance for the lifetime of the process.

    Retry policy: each suggestion_id is attempted at most MAX_ATTEMPTS times.
    After that, the failure is recorded in ai_llm_audit_log and the suggestion
    remains with llm_summary = NULL (frontend shows no explanation, not a
    broken skeleton).

    Reconnect policy: on Redis errors, waits _RECONNECT_DELAY_SECS then
    re-subscribes (identical to cai_redis_listener and suggestions_redis_listener).
    """
    from app.core.database import AsyncSessionLocal
    from app.core.redis import get_redis as _get_redis

    # Per-suggestion retry counter — reset on each worker restart.
    attempt_counts: dict[str, int] = {}

    while True:
        redis = _get_redis()
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(RedisChannels.LLM_EXPLANATION_PENDING)
            logger.info(
                "explanation_worker: subscribed to %s",
                RedisChannels.LLM_EXPLANATION_PENDING,
            )

            while True:
                raw = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.1
                )
                if raw is None:
                    await asyncio.sleep(0)
                    continue

                try:
                    data = json.loads(raw["data"])
                    suggestion_id: str = data["suggestion_id"]
                    suggestion_db_id: int = data["id"]
                except (json.JSONDecodeError, KeyError, TypeError) as exc:
                    logger.warning(
                        "explanation_worker: malformed message on pending channel: %s",
                        exc,
                    )
                    continue

                # Retry guard
                attempts = attempt_counts.get(suggestion_id, 0)
                if attempts >= MAX_ATTEMPTS:
                    logger.error(
                        "explanation_worker: suggestion %s exhausted %d/%d attempts — "
                        "abandoning (llm_summary will remain NULL)",
                        suggestion_id, attempts, MAX_ATTEMPTS,
                    )
                    attempt_counts.pop(suggestion_id, None)
                    continue

                attempt_counts[suggestion_id] = attempts + 1
                logger.info(
                    "explanation_worker: processing suggestion %s (attempt %d/%d)",
                    suggestion_id, attempts + 1, MAX_ATTEMPTS,
                )

                try:
                    async with AsyncSessionLocal() as db:
                        await _generate_explanation(suggestion_id, suggestion_db_id, db)
                    # Success — clear retry counter
                    attempt_counts.pop(suggestion_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error(
                        "explanation_worker: attempt %d/%d failed for suggestion %s: %s",
                        attempts + 1, MAX_ATTEMPTS, suggestion_id, exc,
                    )
                    # Leave attempt_counts[suggestion_id] incremented — a re-publish
                    # will trigger another attempt up to MAX_ATTEMPTS.

        except asyncio.CancelledError:
            logger.info("explanation_worker: cancelled — shutting down")
            raise
        except Exception as exc:
            logger.error(
                "explanation_worker: Redis error: %s — reconnecting in %ds",
                exc, _RECONNECT_DELAY_SECS,
                exc_info=True,
            )
            await asyncio.sleep(_RECONNECT_DELAY_SECS)
        finally:
            try:
                await pubsub.unsubscribe(RedisChannels.LLM_EXPLANATION_PENDING)
                await pubsub.aclose()
            except Exception:
                pass
