"""
LLM Explanation Worker
======================
Async background tasks that generate plain-English explanations for two use cases:

  1. Trade suggestion explanations
       Triggered via ``cortex:stream:explanation:jobs`` Redis Stream consumer group.
       Generates a signal-specific explanation for a committed TradeSuggestion and
       writes it back to the trade_suggestions table.

  2. Instrument market context
       Triggered via ``cortex:stream:context:jobs`` Redis Stream consumer group.
       Generates an instrument-level market context summary for Watchlist items
       with no active trade suggestion.  Written to ai_instrument_context (upsert).

Pipeline — suggestion explanation
----------------------------------
  1. Receive job from cortex:stream:explanation:jobs (XREADGROUP)
  2. DB idempotency check — skip if explanation already exists
  3. In-flight dedup key — skip if another worker is already processing this suggestion
  4. Load TradeSuggestion from DB (Phase 1)
  5. RAG retrieve — top-k news chunks for the symbol in the last 24 hours
  6. LLM structured generation → ExplanationOutput (Phase 2, Gemini native structured output)
  7. Apply output guardrails (disclaimer injection, price-prediction filter, citation check)
  8. Write llm_summary + llm_explanation to trade_suggestions (Phase 3)
  9. Write full payload (with sources) to per-suggestion SSE event store
 10. Append one row to ai_llm_audit_log
 11. Publish routing signal to cortex:llm:explanation:ready:{suggestion_id}
 12. XACK the stream message

Pipeline — instrument market context
--------------------------------------
  1. Receive job from cortex:stream:context:jobs (XREADGROUP)
  2. RAG retrieve — recent news for the symbol (Phase 1)
  3. LLM structured generation → ExplanationOutput (Phase 2)
  4. Apply same guardrails
  5. Upsert into ai_instrument_context (Phase 3)
  6. Write full payload to per-instrument SSE event store
  7. Append one row to ai_llm_audit_log
  8. Publish routing signal to cortex:llm:context:ready:{instrument_key}
  9. XACK the stream message

Delivery architecture
---------------------
  The pub/sub job channels (LLM_EXPLANATION_PENDING, LLM_CONTEXT_PENDING) are
  replaced by Redis Streams consumer groups.  This provides:

  - At-least-once delivery: unACKed messages survive worker restarts (PEL drain).
  - No message loss during LLM processing: the stream buffers jobs while a worker
    is busy; the old pub/sub design dropped any PUBLISH fired during the 10–120s
    LLM call window.
  - Rate-limit back-pressure without blocking: on GeminiRateLimitError the message
    is NOT ACKed.  It stays in the PEL; the housekeeping coroutine re-delivers it
    via XCLAIM after _PEL_IDLE_THRESHOLD_MS.  No asyncio.sleep blocks the consumer
    loop.
  - Dead-letter queue: after MAX_ATTEMPTS deliveries the message is moved to
    cortex:stream:explanation:dlq, a failed-state event is written to the SSE event
    store, and the browser renders "Analysis unavailable" instead of an eternal
    skeleton.

  Pub/sub is retained only as a lightweight wakeup signal after successful generation.
  The actual payload (including RAG source citations) lives in the per-suggestion
  SSE event store (cortex:sse:events:{suggestion_id}) with a 24-hour TTL.

Design invariants
-----------------
  - Failed generations never block indefinitely.  MAX_ATTEMPTS applies per message;
    after exhaustion the DLQ path fires.
  - Two explanation workers run in parallel.  The context worker is a single task
    (context jobs are low-frequency).
  - Every LLM inference — success or failure — writes one ai_llm_audit_log row.
    This is a non-negotiable governance requirement (SR 11-7).
  - The in-flight dedup key (SET NX EX 150) prevents concurrent workers from
    duplicating a Gemini call for the same suggestion_id.

Guardrails (CORTEX_LLM_UPGRADE_PLAN.md §8.1)
---------------------------------------------
  Disclaimer injection  Always appended to full_explanation.
  No price predictions  Regex filter on output; violating sentences are removed.
  Citation check        If RAG returned context, the explanation must contain at
                        least one "[Source:" reference.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AILLMAuditLog
from app.ai.intelligence.llm_client import (
    GeminiQuotaExhausted,
    GeminiRateLimitError,
    LLMTransientExhausted,
    Priority,
    get_intelligence_client,
)
from app.ai.rag.pipeline import build_retrieval_source_refs, format_context, retrieve
from app.core.metrics import (
    gemini_dlq_requeue_total,
    llm_audit_log_writes_total,
    llm_explanation_dedup_total,
    llm_explanation_dlq_total,
    llm_explanation_duration_seconds,
    llm_explanation_worker_active,
    llm_explanations_total,
    llm_guardrail_events_total,
    llm_ready_publish_failures_total,
)
from app.core.redis import RedisChannels, RedisStreams
from app.models.trade_suggestions import TradeSuggestion

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────

# Suggestion explanation: 3 attempts then DLQ + failed-state UI.
MAX_ATTEMPTS = 3
# Instrument context: 5 attempts then ACK+abandon (no DLQ — context is best-effort;
# the user gets a fresh generation on the next watchlist page open).
_MAX_CONTEXT_ATTEMPTS = 5
_RECONNECT_DELAY_SECS = 5

# Hard ceiling on the total LLM call duration (queue wait + HTTP).
_LLM_CALL_TIMEOUT_SECS = 120.0

# Stream consumer configuration
_CONSUMER_GROUP = RedisStreams.CONSUMER_GROUP
_STREAM_BLOCK_MS = 5_000        # block 5s waiting for new messages on each XREADGROUP
_PEL_IDLE_THRESHOLD_MS = 60_000 # XCLAIM PEL entries idle > 60s
_PEL_HOUSEKEEPING_INTERVAL_SECS = 30

# In-flight dedup: longer than _LLM_CALL_TIMEOUT_SECS to cover Phase 3 DB write.
_INFLIGHT_KEY_TTL_SECS = 150

# Context-worker transient backoff: after a 5xx / network exhaustion, skip
# re-processing the same instrument for this many seconds.  Must be longer than
# _PEL_IDLE_THRESHOLD_MS so the cooldown expires before housekeeping re-delivers.
_TRANSIENT_COOLDOWN_TTL_SECS = 180   # 3 min — covers typical Gemini overload spikes

# Post-success dedup window: duplicate stream entries (e.g. scheduler double-enqueue)
# are XACK'd and skipped when the same instrument was successfully processed recently.
# 60 s is sufficient: the scheduler runs every ~90 min; only same-batch duplicates fire
# within this window.
_RECENT_SUCCESS_TTL_SECS = 60

# Context generation distributed lock
_CONTEXT_LOCK_INITIAL_TTL_SECS = 45   # extended by heartbeat every 15s
_LOCK_HEARTBEAT_INTERVAL_SECS = 15

# SSE event store TTLs
_SSE_EXPLANATION_TTL_SECS = 86_400    # 24 h — matches suggestion lifetime
_SSE_CONTEXT_TTL_SECS = 86_400        # 24 h — matches WATCHLIST_CONTEXT_SERVE_MAX_AGE_HOURS
                                       #         so the richer SSE payload (with source citations)
                                       #         stays available for the same window Stage 2 serves
_SSE_EVENT_MAXLEN = 20                 # per-key stream depth

# Stream max lengths (approximate trim — O(1) amortised)
_STREAM_MAXLEN_EXPLANATION = 5_000
_STREAM_MAXLEN_CONTEXT = 1_000

# Lua script for atomic lock renewal (Section 3.4 of fix design doc).
# Checks ownership before extending so a re-acquired lock by another process
# cannot be extended by a stale heartbeat.
_LOCK_RENEW_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('pexpire', KEYS[1], ARGV[2]) "
    "else return 0 end"
)


# ── Output schema ──────────────────────────────────────────────────────────────

class ExplanationOutput(BaseModel):
    """Structured explanation produced by the LLM for a trade suggestion.

    Length is guided by the field descriptions and the system prompt, NOT by hard
    Pydantic constraints: Gemini's native structured output is validated against
    this schema, so a min/max-length bound would turn a merely-terse reply into a
    hard generation failure.  The summary is length-capped post-hoc instead.
    """
    summary: str = Field(
        description=(
            "2–3 sentence plain-English distillation suitable for inline display. "
            "Plain text only — NO markdown headers. Lead with what the ML ensemble "
            "concluded and why, grounded in the signal numbers and (if present) the "
            "retrieved news context."
        ),
    )
    full_explanation: str = Field(
        description=(
            "Full narrative as Markdown with EXACTLY these five section headers, "
            "in this order, each on its own line and prefixed with '### ':\n"
            "### What the models saw\n"
            "### Technical picture\n"
            "### News context\n"
            "### What this suggests\n"
            "### Key risks\n"
            "Under each header write 1–3 sentences grounded ONLY in the numbers and "
            "context provided in the prompt. In 'What the models saw', reference the "
            "ensemble and per-model (XGBoost / GRU) directions, probabilities and "
            "conviction-vs-threshold. Cite news inline as: According to "
            "[Source Name, YYYY-MM-DD]... Do not invent figures or model internals."
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

Your task: explain a machine-generated trade signal in plain English — what the ML
ensemble (XGBoost + GRU) and technical scanner observed, how that evidence combined into
a consensus, and what it implies — grounded strictly in the structured signal data and the
retrieved news articles provided in the prompt.

Write full_explanation as Markdown with EXACTLY these five section headers, in order, each
on its own line:
### What the models saw
### Technical picture
### News context
### What this suggests
### Key risks

Be concise — 1–2 short sentences per section, ≤110 words total for full_explanation.
No preamble, no filler, no restating the prompt; an analyst is skimming this. When a
section has nothing substantive (e.g. no news, no per-model split), state it in one short
line rather than padding.

Section guidance:
- "What the models saw": describe the ensemble direction and calibrated confidence, then
  the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities, and how each
  model's conviction compares to its regime-adaptive threshold. Note agreement or
  disagreement between the two models. Use ONLY the numbers given.
- "Technical picture": summarise the scanner readings provided (e.g. RSI, volume ratio,
  price change). If none are present, say so briefly.
- "News context": summarise the retrieved articles and CITE each factual claim inline as
  According to [Source Name, YYYY-MM-DD]... If no articles were provided, state that no
  recent news context was available — never invent sources.
- "What this suggests": a neutral synthesis of direction, confidence band and time horizon.
  Describe; do NOT advise.
- "Key risks": what could invalidate the setup (model disagreement, low conviction, thin
  news corroboration, regime, etc.).

Mandatory rules:
1. GROUND every ML/technical claim in the numbers provided; never invent figures, prices,
   model internals, events, or sources.
2. CITE news claims inline: According to [Source Name, YYYY-MM-DD]...
3. PROHIBITED language (these will be filtered):
   - Price predictions: "will reach ₹X", "target price", "price target"
   - Guarantees: "guaranteed", "certain to", "will definitely"
   - Advisory language: "you should buy/sell", "recommend buying", "buy now"
4. DISCLAIMER: The system automatically appends the required regulatory disclaimer.
   Do NOT add your own disclaimer — it would duplicate the injected one.
5. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
   The summary is plain text (no headers); full_explanation contains the five sections.\
"""

# ── Context system prompt ─────────────────────────────────────────────────────

_CONTEXT_SYSTEM_PROMPT = """\
You are a market context analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: explain the current read on a specific NSE-listed stock that has no active trade
signal — what the ML ensemble (XGBoost + GRU) is currently leaning toward and why, and what
recent news is relevant — grounded strictly in the structured model snapshot and the
retrieved news articles provided in the prompt.

Write full_explanation as Markdown with EXACTLY these five section headers, in order, each
on its own line:
### What the models saw
### Technical picture
### News context
### What this suggests
### Key risks

Be concise — 1–2 short sentences per section, ≤110 words total for full_explanation.
No preamble, no filler, no restating the prompt; an analyst is skimming this. When a
section has nothing substantive (e.g. no news, no per-model split), state it in one short
line rather than padding.

Section guidance:
- "What the models saw": describe the ensemble's current direction and calibrated
  confidence, then the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities,
  and conviction-vs-threshold. Note agreement or disagreement. Use ONLY the numbers given.
  If no model snapshot is provided, state that no live model read was available.
- "Technical picture": summarise volatility / market-regime signals provided, if any.
- "News context": summarise the retrieved articles and CITE each factual claim inline as
  According to [Source Name, YYYY-MM-DD]... If no articles were provided, state that no
  recent news context was available — never invent sources.
- "What this suggests": a neutral synthesis of the current lean. Describe; do NOT advise.
- "Key risks": model disagreement, low conviction, thin news corroboration, volatility, etc.

Mandatory rules:
1. GROUND every ML claim in the numbers provided; never invent figures, prices, model
   internals, events, or sources.
2. CITE news claims inline: According to [Source Name, YYYY-MM-DD]...
3. PROHIBITED language (these will be filtered):
   - Price predictions: "will reach ₹X", "target price", "price target"
   - Guarantees: "guaranteed", "certain to", "will definitely"
   - Advisory language: "you should buy/sell", "recommend buying", "buy now"
4. DISCLAIMER: The system automatically appends the required regulatory disclaimer.
   Do NOT add your own disclaimer — it would duplicate the injected one.
5. Output JSON only: {"summary": "...", "full_explanation": "...", "sources_used": [...]}
   The summary is plain text (no headers); full_explanation contains the five sections.\
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


def _strip_price_predictions(text: str) -> tuple[str, int]:
    """
    Remove sentences matching the price-prediction guardrail, operating per line
    so the markdown section structure (### headers, blank lines) is preserved.
    Returns ``(filtered_text, n_removed)``.
    """
    removed = 0
    clean_lines: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            clean_lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            if _PRICE_PREDICTION_RE.search(sentence):
                removed += 1
                logger.warning(
                    "explanation_worker: price-prediction guardrail removed sentence: %.80s...",
                    sentence,
                )
            else:
                kept.append(sentence)
        clean_lines.append(" ".join(kept))
    return "\n".join(clean_lines), removed


def _apply_guardrails(
    output: ExplanationOutput,
    has_context: bool,
) -> tuple[ExplanationOutput, list[str]]:
    """
    Apply all output guardrails.  Returns the (possibly modified) output and
    a list of guardrail event names that fired.
    """
    events: list[str] = []

    full_explanation, n_removed = _strip_price_predictions(output.full_explanation)
    events.extend(["price_prediction_filter"] * n_removed)

    summary_sentences = re.split(r"(?<=[.!?])\s+", output.summary)
    clean_summary = " ".join(
        s for s in summary_sentences if not _PRICE_PREDICTION_RE.search(s)
    )
    if not clean_summary.strip():
        clean_summary = output.summary

    if has_context and "[" not in full_explanation:
        events.append("citation_missing")
        logger.warning(
            "explanation_worker: citation guardrail — context was provided but "
            "no [Source] citation found in full_explanation"
        )

    full_explanation_with_disclaimer = full_explanation.rstrip() + _REGULATORY_DISCLAIMER

    return (
        ExplanationOutput(
            summary=clean_summary or output.summary,
            full_explanation=full_explanation_with_disclaimer,
            sources_used=output.sources_used,
        ),
        events,
    )


# ── Prompt builders ───────────────────────────────────────────────────────────

def _fmt_pct(value: Any, default: str = "N/A") -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return default


def _format_probabilities(probs: dict | None) -> str | None:
    if not probs:
        return None
    parts = [
        f"{label} {_fmt_pct(probs[key])}"
        for key, label in (("buy", "BUY"), ("sell", "SELL"), ("hold", "HOLD"))
        if probs.get(key) is not None
    ]
    return " · ".join(parts) if parts else None


def _render_model_breakdown(models: dict | None) -> list[str]:
    if not models:
        return []
    lines: list[str] = []
    for key, label in (("xgboost", "XGBoost"), ("gru", "GRU")):
        m = models.get(key)
        if not m:
            continue
        bits = [f"{label}: {m.get('direction', 'N/A')}"]
        probs = _format_probabilities(m.get("probabilities"))
        if probs:
            bits.append(f"probs {probs}")
        if m.get("conviction_scale") is not None:
            bits.append(f"conviction {_fmt_pct(m['conviction_scale'])}")
        if m.get("threshold") is not None:
            bits.append(f"threshold {_fmt_pct(m['threshold'])}")
        if m.get("weight") is not None:
            try:
                bits.append(f"weight {float(m['weight']):.2f}")
            except (TypeError, ValueError):
                pass
        lines.append("  - " + ", ".join(bits))
    return lines


_SCANNER_LABELS: dict[str, str] = {
    "signal":           "Signal",
    "direction":        "Direction",
    "score":            "Score",
    "rsi":              "RSI-14",
    "volume_ratio":     "Volume Ratio",
    "price_change_pct": "Price Change %",
    "last_price":       "Last Price",
    "previous_close":   "Prev Close",
    "volume":           "Volume",
}


def _render_scanner(scanner: dict | None) -> list[str]:
    if not scanner:
        return []
    lines: list[str] = []
    for key, label in _SCANNER_LABELS.items():
        if key not in scanner:
            continue
        value = scanner[key]
        if value is None or isinstance(value, (dict, list)):
            continue
        if isinstance(value, float):
            value = round(value, 2)
        lines.append(f"{label}: {value}")
    return lines


def _build_explanation_prompt(suggestion: TradeSuggestion, context: str) -> str:
    ml = suggestion.ml_signal or {}
    ai = suggestion.ai_signal or {}
    scanner = suggestion.scanner_signal or {}
    prediction = ml.get("prediction") or {}

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
    if suggestion.trigger_pathway:
        lines.append(
            f"Trigger Pathway:  {suggestion.trigger_pathway.replace('_', ' ').title()}"
        )

    if ml.get("available"):
        lines.append("")
        lines.append("## ML Ensemble Output")
        score = ml.get("score", 0.0) or 0.0
        ens_dir = prediction.get("direction") or (
            "BUY" if score > 0 else "SELL" if score < 0 else "HOLD"
        )
        lines.append(f"Ensemble Direction:     {ens_dir}")
        lines.append(f"Calibrated Confidence:  {_fmt_pct(ml.get('confidence'))}")
        if prediction.get("conviction_scale") is not None:
            lines.append(
                f"Conviction (0=threshold,1=max): {_fmt_pct(prediction['conviction_scale'])}"
            )
        ens_probs = _format_probabilities(prediction.get("probabilities"))
        if ens_probs:
            lines.append(f"Ensemble Probabilities: {ens_probs}")
        if ml.get("model"):
            lines.append(f"Model Versions:         {ml['model']}")
        model_lines = _render_model_breakdown(ml.get("models"))
        if model_lines:
            lines.append("Per-model breakdown:")
            lines.extend(model_lines)

    scanner_lines = _render_scanner(scanner)
    if scanner_lines:
        lines.append("")
        lines.append("## Technical Scanner Readings")
        lines.extend(scanner_lines)

    sentiment_label = ai.get("sentiment_label") or ai.get("sentiment")
    forecast_dir = ai.get("direction")
    forecast_rationale = ai.get("rationale")
    events = ai.get("events") or ai.get("contributing_events") or []
    event_count = ai.get("event_count") or len(events)
    if sentiment_label or forecast_dir or events or event_count:
        lines.append("")
        lines.append("## News & Event Signal")
        if forecast_dir:
            lines.append(
                f"News Forecaster Lean: {forecast_dir} "
                f"(confidence {_fmt_pct(ai.get('confidence'))})"
            )
        if forecast_rationale:
            lines.append(f"News Forecaster View: {forecast_rationale}")
        if sentiment_label:
            lines.append(f"Sentiment:           {sentiment_label}")
        if event_count:
            lines.append(f"Contributing Events: {event_count}")
        for ev in events[:5]:
            title = ev.get("article_title") or ev.get("type") or "event"
            extra: list[str] = []
            if ev.get("impact") is not None:
                try:
                    extra.append(f"impact {float(ev['impact']):+.1f}")
                except (TypeError, ValueError):
                    pass
            if ev.get("source_name"):
                extra.append(str(ev["source_name"]))
            suffix = f" ({', '.join(extra)})" if extra else ""
            lines.append(f"  - {title}{suffix}")

    if context:
        lines.append("")
        lines.append("## Retrieved News Context")
        lines.append(
            "(Use these articles as the factual basis for the News context section. "
            "Cite inline as: According to [Source Name, YYYY-MM-DD]...)"
        )
        lines.append(context)
    else:
        lines.append("")
        lines.append(
            "## Retrieved News Context\n"
            "No recent news articles were found for this symbol. State clearly in the "
            "News context section that no news context was available, and base the rest "
            "of the explanation on the quantitative signal data above."
        )

    return "\n".join(lines)


def _build_context_prompt(
    instrument_key: str,
    symbol: str,
    ml_snapshot: dict | None,
    context: str,
) -> str:
    lines = [
        "## Instrument Overview",
        f"Instrument Key: {instrument_key}",
        f"Symbol:         {symbol}",
    ]

    if ml_snapshot and ml_snapshot.get("available"):
        lines.append("")
        lines.append("## Current ML Ensemble Snapshot")
        lines.append(f"Ensemble Direction:     {ml_snapshot.get('direction', 'N/A')}")
        lines.append(f"Calibrated Confidence:  {_fmt_pct(ml_snapshot.get('confidence'))}")
        if ml_snapshot.get("conviction_scale") is not None:
            lines.append(
                f"Conviction (0=threshold,1=max): {_fmt_pct(ml_snapshot['conviction_scale'])}"
            )
        if ml_snapshot.get("threshold") is not None:
            lines.append(f"Regime Threshold:       {_fmt_pct(ml_snapshot['threshold'])}")
        ens_probs = _format_probabilities(ml_snapshot.get("probabilities"))
        if ens_probs:
            lines.append(f"Ensemble Probabilities: {ens_probs}")
        if ml_snapshot.get("volatility") is not None:
            lines.append(f"Annualised Volatility:  {_fmt_pct(ml_snapshot['volatility'])}")
        if ml_snapshot.get("timeframe"):
            lines.append(f"Timeframe:              {ml_snapshot['timeframe']}")
        model_lines = _render_model_breakdown(ml_snapshot.get("models"))
        if model_lines:
            lines.append("Per-model breakdown:")
            lines.extend(model_lines)

    if context:
        lines.append("")
        lines.append("## Retrieved News Context")
        lines.append(
            "(Use these articles as the factual basis for the News context section. "
            "Cite inline as: According to [Source Name, YYYY-MM-DD]...)"
        )
        lines.append(context)
    else:
        lines.append("")
        lines.append(
            "## Retrieved News Context\n"
            "No recent news articles were found for this instrument. State clearly in "
            "the News context section that no recent news context was available, and "
            "base the rest of the analysis on the ML snapshot above."
        )

    return "\n".join(lines)


# ── Audit log helper ──────────────────────────────────────────────────────────

async def _write_audit_entry(
    db: AsyncSession,
    *,
    invocation_id: UUID,
    invocation_type: str,
    reference_table: str,
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
            reference_table=reference_table,
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


# ── SSE event store helpers ───────────────────────────────────────────────────

async def _write_sse_explanation_event(redis: Any, suggestion_id: str, payload: dict) -> None:
    """
    Write a full explanation payload to the per-suggestion SSE event store.

    Written BEFORE publishing the routing signal so the SSE watcher always finds
    the payload waiting when it reads the event store on signal receipt.
    TTL = 86 400 s (24 h) — matches suggestion lifetime.
    """
    key = RedisStreams.sse_explanation_key(suggestion_id)
    try:
        await redis.xadd(
            key,
            {"data": json.dumps(payload, default=str)},
            maxlen=_SSE_EVENT_MAXLEN,
            approximate=True,
        )
        await redis.expire(key, _SSE_EXPLANATION_TTL_SECS)
    except Exception as exc:
        logger.warning(
            "explanation_worker: failed to write SSE event store for suggestion %s: %s",
            suggestion_id, exc,
        )


async def _write_sse_context_event(redis: Any, instrument_key: str, payload: dict) -> None:
    """
    Write a full context payload to the per-instrument SSE event store.
    TTL = 86 400 s (24 h) — matches WATCHLIST_CONTEXT_SERVE_MAX_AGE_HOURS so the
    richer SSE payload (including RAG source citations) remains available for the
    same duration that Stage 2 in ai_stream.py will serve cached context.
    """
    key = RedisStreams.sse_context_key(instrument_key)
    try:
        await redis.xadd(
            key,
            {"data": json.dumps(payload, default=str)},
            maxlen=_SSE_EVENT_MAXLEN,
            approximate=True,
        )
        await redis.expire(key, _SSE_CONTEXT_TTL_SECS)
    except Exception as exc:
        logger.warning(
            "explanation_worker: failed to write SSE context event store for %s: %s",
            instrument_key, exc,
        )


# ── Core explanation logic ────────────────────────────────────────────────────

async def _generate_explanation(
    suggestion_id: str,
    suggestion_db_id: int,
) -> None:
    """
    Execute the full explanation pipeline for one suggestion.

    Structured as three distinct phases to eliminate the DB connection leak:
      Phase 1 — DB read + idempotency checks; session closed before LLM call.
      Phase 2 — LLM call: outside any DB session; bounded by _LLM_CALL_TIMEOUT_SECS.
      Phase 3 — DB write + SSE event store write + PUBLISH routing signal.

    Raises on unrecoverable errors so the consumer can decide ACK vs PEL.
    Raises GeminiRateLimitError so the consumer leaves the message in PEL
    (no real LLM attempt was made so the delivery counter is not incremented).
    """
    from app.core.database import AsyncSessionLocal
    from app.core.redis import get_redis

    _redis = get_redis()
    client = get_intelligence_client()
    invocation_id = uuid4()
    suggestion_uuid = UUID(suggestion_id)

    # ── Phase 1: DB read — closed before LLM call ────────────────────────────
    chunks: list = []
    prompt: str = ""
    prompt_hash: str = ""
    source_refs: list[dict] = []
    suggestion_symbol: str = ""
    suggestion_instrument_key: str = ""
    suggestion_signal_direction: str | None = None
    suggestion_signal_generated_at: str | None = None

    async with AsyncSessionLocal() as db:
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == suggestion_uuid
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

        # DB idempotency check — skip if explanation was already written
        # (handles XCLAIM redelivery after a crash where Phase 3 completed but ACK failed)
        if suggestion.llm_summary is not None:
            logger.info(
                "explanation_worker: explanation already exists for suggestion %s "
                "(DB idempotency) — skipping Gemini call",
                suggestion_id,
            )
            llm_explanation_dedup_total.labels(layer="db_idempotency").inc()
            return

        suggestion_symbol = suggestion.symbol
        suggestion_instrument_key = suggestion.instrument_key
        suggestion_signal_direction = suggestion.signal_direction
        suggestion_signal_generated_at = (
            suggestion.created_at.isoformat() if suggestion.created_at else None
        )
        query = f"{suggestion.symbol} {suggestion.signal_direction} trading signal"

        try:
            chunks = await retrieve(db=db, query=query, symbol=suggestion.symbol)
        except Exception as exc:
            logger.warning(
                "explanation_worker: RAG retrieval failed for %s "
                "(continuing with no context): %s",
                suggestion_id, exc,
            )
            chunks = []

        context = format_context(chunks)
        source_refs = build_retrieval_source_refs(chunks)
        prompt = _build_explanation_prompt(suggestion, context)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    # ── DB session closed — pool connection returned before LLM call ──────────

    # ── In-flight dedup key — prevents concurrent workers from duplicating the call ──
    inflight_key = RedisStreams.inflight_key(suggestion_id)
    inflight_acquired = await _redis.set(
        inflight_key, "1", nx=True, ex=_INFLIGHT_KEY_TTL_SECS
    )
    if not inflight_acquired:
        logger.info(
            "explanation_worker: in-flight key exists for suggestion %s "
            "— another worker is processing it, releasing without ACK",
            suggestion_id,
        )
        llm_explanation_dedup_total.labels(layer="inflight_key").inc()
        return

    # ── Phase 2: LLM call (no DB session held open) ───────────────────────────
    t0 = time.monotonic()
    error_message: str | None = None
    raw_output: ExplanationOutput | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    _is_quota_exhausted: bool = False
    _is_rate_limited: bool = False
    model_provider, _, model_id = client.model_id.partition("/")

    try:
        llm_explanation_worker_active.inc()
        try:
            raw_output, usage_info = await asyncio.wait_for(
                client.generate_structured_with_usage(
                    prompt=prompt,
                    response_model=ExplanationOutput,
                    system=_EXPLANATION_SYSTEM_PROMPT,
                    temperature=0.2,
                    max_tokens=1400,
                    priority=Priority.HIGH,
                ),
                timeout=_LLM_CALL_TIMEOUT_SECS,
            )
            input_tokens  = usage_info.get("input_tokens")
            output_tokens = usage_info.get("output_tokens")
            model_provider = usage_info.get("provider", model_provider)
            model_id       = usage_info.get("model_id", model_id)
        finally:
            llm_explanation_worker_active.dec()
    except asyncio.TimeoutError:
        error_message = f"LLMTimeoutError: call exceeded {_LLM_CALL_TIMEOUT_SECS:.0f}s ceiling"
        logger.error(
            "explanation_worker: LLM call timed out for suggestion %s",
            suggestion_id,
        )
    except GeminiRateLimitError as exc:
        _is_rate_limited = True
        logger.warning(
            "explanation_worker: rate-limited for suggestion %s — releasing inflight key, "
            "message stays in PEL for housekeeping re-delivery: %s",
            suggestion_id, exc,
        )
        with contextlib.suppress(Exception):
            await _redis.delete(inflight_key)
        raise  # propagate — consumer must NOT XACK; message stays in PEL
    except GeminiQuotaExhausted as exc:
        _is_quota_exhausted = True
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: Gemini daily quota exhausted for suggestion %s",
            suggestion_id,
        )
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: LLM call failed for suggestion %s: %s",
            suggestion_id, exc,
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    if error_message is None:
        llm_explanations_total.labels(status="success", provider=model_provider).inc()
        llm_explanation_duration_seconds.labels(provider=model_provider).observe(
            latency_ms / 1000.0
        )
    else:
        llm_explanations_total.labels(status="failure", provider=model_provider).inc()

    # ── Guardrails ────────────────────────────────────────────────────────────
    guardrail_events: list[str] = []
    final_output: ExplanationOutput | None = None

    if raw_output is not None:
        final_output, guardrail_events = _apply_guardrails(raw_output, has_context=bool(chunks))
        for event in guardrail_events:
            llm_guardrail_events_total.labels(guardrail=event).inc()

    # ── Phase 3: DB write + event store + publish ─────────────────────────────
    output_preview: str | None = None
    sources_payload: list[dict] = []

    if final_output is not None:
        output_preview = final_output.summary[:500]
        sources_payload = [
            {
                "source_name": chunk.source_name,
                "as_of":       chunk.as_of_timestamp.isoformat(),
                "source_url":  chunk.source_url,
            }
            for chunk in chunks
        ]

    async with AsyncSessionLocal() as db:
        if final_output is not None:
            now_utc = datetime.now(timezone.utc)
            await db.execute(
                update(TradeSuggestion)
                .where(TradeSuggestion.suggestion_id == suggestion_uuid)
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
                suggestion_symbol,
                latency_ms,
                guardrail_events or "none",
            )

        await _write_audit_entry(
            db,
            invocation_id=invocation_id,
            invocation_type="explanation",
            reference_table="trade_suggestions",
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

    # ── Write to SSE event store BEFORE publishing routing signal ─────────────
    if final_output is not None:
        sse_payload = {
            "available":           True,
            "failed":              False,
            "summary":             final_output.summary,
            "full_explanation":    final_output.full_explanation,
            "model":               f"{model_provider}/{model_id}",
            "generated_at":        datetime.now(timezone.utc).isoformat(),
            "sources":             sources_payload,
            "context_type":        "suggestion_explanation",
            "signal_direction":    suggestion_signal_direction,
            "signal_generated_at": suggestion_signal_generated_at,
        }
        await _write_sse_explanation_event(_redis, suggestion_id, sse_payload)

        # Publish routing signal only — payload lives in the event store
        try:
            ready_channel = RedisChannels.LLM_EXPLANATION_READY.format(
                suggestion_id=suggestion_id
            )
            routing_signal = json.dumps({
                "suggestion_id":  suggestion_id,
                "instrument_key": suggestion_instrument_key,
            }, default=str)
            await _redis.publish(ready_channel, routing_signal)
        except Exception as exc:
            llm_ready_publish_failures_total.labels(job_type="explanation").inc()
            logger.warning(
                "explanation_worker: failed to publish ready signal for suggestion %s "
                "(non-fatal — browser recovers via 30s poll): %s",
                suggestion_id, exc,
            )

    # ── Clean up inflight key ─────────────────────────────────────────────────
    with contextlib.suppress(Exception):
        await _redis.delete(inflight_key)

    if error_message is not None:
        if _is_quota_exhausted:
            raise GeminiQuotaExhausted(error_message)
        raise RuntimeError(error_message)


# ── Instrument context generation ────────────────────────────────────────────

async def _lock_heartbeat(
    redis: Any,
    lock_key: str,
    lock_token: str,
) -> None:
    """
    Renew the context generation distributed lock every _LOCK_HEARTBEAT_INTERVAL_SECS.

    Uses an atomic Lua CAS script: only extends the TTL if we still own the lock.
    If the lock was re-acquired by another process (token mismatch), the heartbeat
    logs a warning and exits rather than extending a lock we no longer own.
    """
    while True:
        await asyncio.sleep(_LOCK_HEARTBEAT_INTERVAL_SECS)
        try:
            result = await redis.eval(
                _LOCK_RENEW_SCRIPT,
                1,
                lock_key,
                lock_token,
                str(_CONTEXT_LOCK_INITIAL_TTL_SECS * 1000),
            )
            if result == 0:
                logger.warning(
                    "explanation_worker: lock heartbeat — ownership lost for %s "
                    "(another process re-acquired the lock), stopping heartbeat",
                    lock_key,
                )
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "explanation_worker: lock heartbeat failed for %s: %s", lock_key, exc
            )


async def _generate_instrument_context(
    instrument_key: str,
    symbol: str | None,
    ml_snapshot: dict | None,
    lock_key: str | None = None,
    lock_token: str | None = None,
    force: bool = False,
) -> None:
    """
    Generate a market context summary for an instrument with no active signal.

    Structured as three distinct phases — same pattern as _generate_explanation —
    to ensure no DB connection is held during the LLM call.

    If ``lock_key`` and ``lock_token`` are provided (from the stream message payload),
    a heartbeat coroutine extends the distributed lock every 15 seconds so it cannot
    expire during a slow LLM call and allow duplicate generation.
    """
    from app.ai.fusion.models import AIInstrumentContext
    from app.core.database import AsyncSessionLocal
    from app.core.redis import get_redis
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    _redis = get_redis()
    client = get_intelligence_client()
    invocation_id = uuid4()

    eff_symbol: str = symbol or (
        instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
    )

    # ── Phase 1: DB read — closed before LLM call ────────────────────────────
    chunks: list = []
    prompt: str = ""
    prompt_hash: str = ""
    source_refs: list[dict] = []

    async with AsyncSessionLocal() as db:
        # Idempotency guard — skip if unexpired context already exists.
        # Handles PEL re-delivery after a crash between Phase 3 DB write and XACK.
        # Bypassed when force=True (watchlist scheduler controls its own cadence).
        if not force:
            existing = await db.execute(
                select(AIInstrumentContext).where(
                    AIInstrumentContext.instrument_key == instrument_key,
                    AIInstrumentContext.expires_at > datetime.now(timezone.utc),
                )
            )
            if existing.scalar_one_or_none() is not None:
                logger.info(
                    "explanation_worker: context idempotency — unexpired record exists "
                    "for %s, skipping Gemini call (PEL re-delivery after Phase-3 crash)",
                    instrument_key,
                )
                llm_explanation_dedup_total.labels(layer="db_idempotency").inc()
                return
        else:
            logger.debug(
                "explanation_worker: force=True — bypassing idempotency check for %s "
                "(scheduler-initiated refresh)",
                instrument_key,
            )

        query = f"{eff_symbol} market analysis news"
        try:
            chunks = await retrieve(db=db, query=query, symbol=eff_symbol)
        except Exception as exc:
            logger.warning(
                "explanation_worker: RAG retrieval failed for context %s "
                "(continuing with no context): %s",
                instrument_key, exc,
            )
            chunks = []

        context     = format_context(chunks)
        source_refs = build_retrieval_source_refs(chunks)
        prompt      = _build_context_prompt(instrument_key, eff_symbol, ml_snapshot, context)
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
    # ── DB session closed ─────────────────────────────────────────────────────

    # ── Phase 2: LLM call with optional lock heartbeat ───────────────────────
    heartbeat_task: asyncio.Task | None = None
    if lock_key and lock_token:
        heartbeat_task = asyncio.create_task(
            _lock_heartbeat(_redis, lock_key, lock_token),
            name=f"context_lock_heartbeat_{instrument_key[:32]}",
        )

    t0 = time.monotonic()
    error_message:       str | None              = None
    raw_output:          ExplanationOutput | None = None
    input_tokens:        int | None               = None
    output_tokens:       int | None               = None
    _is_quota_exhausted: bool                     = False
    model_provider, _, model_id = client.model_id.partition("/")

    try:
        raw_output, usage_info = await asyncio.wait_for(
            client.generate_structured_with_usage(
                prompt=prompt,
                response_model=ExplanationOutput,
                system=_CONTEXT_SYSTEM_PROMPT,
                temperature=0.2,
                max_tokens=1400,
                priority=Priority.HIGH,
            ),
            timeout=_LLM_CALL_TIMEOUT_SECS,
        )
        input_tokens  = usage_info.get("input_tokens")
        output_tokens = usage_info.get("output_tokens")
        model_provider = usage_info.get("provider", model_provider)
        model_id       = usage_info.get("model_id", model_id)
    except asyncio.TimeoutError:
        error_message = f"LLMTimeoutError: call exceeded {_LLM_CALL_TIMEOUT_SECS:.0f}s ceiling"
        logger.error(
            "explanation_worker: context LLM call timed out for %s", instrument_key
        )
    except GeminiRateLimitError as exc:
        logger.warning(
            "explanation_worker: rate-limited for context %s — message stays in PEL: %s",
            instrument_key, exc,
        )
        raise
    except GeminiQuotaExhausted as exc:
        _is_quota_exhausted = True
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: quota exhausted for context %s", instrument_key
        )
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: context generation failed for %s: %s",
            instrument_key, exc,
        )
    finally:
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task

    latency_ms = int((time.monotonic() - t0) * 1000)

    if error_message is None:
        llm_explanations_total.labels(status="success", provider=model_provider).inc()
        llm_explanation_duration_seconds.labels(provider=model_provider).observe(
            latency_ms / 1000.0
        )
    else:
        llm_explanations_total.labels(status="failure", provider=model_provider).inc()

    # ── Guardrails ────────────────────────────────────────────────────────────
    guardrail_events: list[str]           = []
    final_output:     ExplanationOutput | None = None

    if raw_output is not None:
        final_output, guardrail_events = _apply_guardrails(raw_output, has_context=bool(chunks))
        for event in guardrail_events:
            llm_guardrail_events_total.labels(guardrail=event).inc()

    # ── Phase 3: DB write + event store + publish ─────────────────────────────
    sources_payload: list[dict] = []
    output_preview: str | None = None
    if final_output is not None:
        output_preview = final_output.summary[:500]
        sources_payload = [
            {
                "source_name": chunk.source_name,
                "as_of":       chunk.as_of_timestamp.isoformat(),
                "source_url":  chunk.source_url,
            }
            for chunk in chunks
        ]

    async with AsyncSessionLocal() as db:
        if final_output is not None:
            now_utc    = datetime.now(timezone.utc)
            expires_at = now_utc + timedelta(hours=2)
            model_str  = f"{model_provider}/{model_id}"

            upsert_stmt = (
                pg_insert(AIInstrumentContext)
                .values(
                    instrument_key=instrument_key,
                    symbol=symbol,
                    context_summary=final_output.summary,
                    context_full=final_output.full_explanation,
                    model_used=model_str,
                    source_refs=sources_payload or None,
                    generated_at=now_utc,
                    expires_at=expires_at,
                )
                .on_conflict_do_update(
                    index_elements=["instrument_key"],
                    set_={
                        "symbol":          symbol,
                        "context_summary": final_output.summary,
                        "context_full":    final_output.full_explanation,
                        "model_used":      model_str,
                        "source_refs":     sources_payload or None,
                        "generated_at":    now_utc,
                        "expires_at":      expires_at,
                    },
                )
            )
            await db.execute(upsert_stmt)
            await db.commit()

            logger.info(
                "explanation_worker: instrument context written for %s "
                "symbol=%s latency_ms=%d guardrails=%s",
                instrument_key,
                eff_symbol,
                latency_ms,
                guardrail_events or "none",
            )

        await _write_audit_entry(
            db,
            invocation_id=invocation_id,
            invocation_type="instrument_context",
            reference_table="ai_instrument_context",
            reference_id=None,
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

    # ── Write to SSE event store BEFORE publishing routing signal ─────────────
    if final_output is not None:
        ctx_payload = {
            "available":        True,
            "failed":           False,
            "summary":          final_output.summary,
            "full_explanation": final_output.full_explanation,
            "model":            f"{model_provider}/{model_id}",
            "generated_at":     datetime.now(timezone.utc).isoformat(),
            "sources":          sources_payload,
            "context_type":     "instrument_context",
            "signal_direction":   None,
            "signal_generated_at": None,
        }
        await _write_sse_context_event(_redis, instrument_key, ctx_payload)

        try:
            ready_channel = RedisChannels.LLM_CONTEXT_READY.format(
                instrument_key=instrument_key
            )
            routing_signal = json.dumps({
                "instrument_key": instrument_key,
            }, default=str)
            await _redis.publish(ready_channel, routing_signal)
        except Exception as exc:
            llm_ready_publish_failures_total.labels(job_type="context").inc()
            logger.warning(
                "explanation_worker: failed to publish context ready signal for %s "
                "(non-fatal): %s",
                instrument_key, exc,
            )

    if error_message is not None:
        if _is_quota_exhausted:
            raise GeminiQuotaExhausted(error_message)
        raise RuntimeError(error_message)


# ── DLQ and PEL helpers ───────────────────────────────────────────────────────

async def _publish_failed_state(
    redis: Any,
    suggestion_id: str,
    instrument_key: str,
) -> None:
    """
    Write a permanent-failure event to the SSE event store and publish the
    wakeup signal so the browser renders "Analysis unavailable" rather than
    an eternal skeleton.
    """
    failed_payload: dict = {
        "available":           False,
        "failed":              True,
        "summary":             None,
        "full_explanation":    None,
        "model":               None,
        "generated_at":        datetime.now(timezone.utc).isoformat(),
        "sources":             [],
        "context_type":        "suggestion_explanation",
        "signal_direction":    None,
        "signal_generated_at": None,
    }
    await _write_sse_explanation_event(redis, suggestion_id, failed_payload)

    try:
        ready_channel = RedisChannels.LLM_EXPLANATION_READY.format(
            suggestion_id=suggestion_id
        )
        await redis.publish(ready_channel, json.dumps({
            "suggestion_id":  suggestion_id,
            "instrument_key": instrument_key,
            "failed":         True,
        }, default=str))
    except Exception as exc:
        logger.warning(
            "explanation_worker: failed to publish failed state for suggestion %s: %s",
            suggestion_id, exc,
        )


async def _move_to_dlq(
    redis: Any,
    stream: str,
    group: str,
    msg_id: str,
    consumer_name: str,
    reason: str,
    fields: dict,
) -> None:
    """
    Move a message to the explanation DLQ, publish a failed-state SSE event,
    and XACK to remove it from the PEL.
    """
    try:
        dlq_entry = {
            "original_stream": stream,
            "original_msg_id": msg_id,
            "consumer":        consumer_name,
            "reason":          reason,
            "fields":          json.dumps(fields, default=str),
            "moved_at":        datetime.now(timezone.utc).isoformat(),
        }
        await redis.xadd(
            RedisStreams.EXPLANATION_DLQ,
            dlq_entry,
            maxlen=1000,
            approximate=True,
        )
        await redis.xack(stream, group, msg_id)
        llm_explanation_dlq_total.labels(job_type="explanation").inc()
        logger.error(
            "explanation_worker: message %s moved to DLQ (reason=%s consumer=%s)",
            msg_id, reason, consumer_name,
        )
    except Exception as exc:
        logger.error(
            "explanation_worker: DLQ write failed for message %s: %s", msg_id, exc
        )
        return

    # Publish failed state if we have enough routing information
    suggestion_id  = fields.get("suggestion_id", "")
    instrument_key = fields.get("instrument_key", "")
    if suggestion_id:
        await _publish_failed_state(redis, suggestion_id, instrument_key)


async def _drain_pel(
    redis: Any,
    stream: str,
    group: str,
    consumer_name: str,
) -> None:
    """
    Re-deliver all PEL entries for this consumer (handles pre-crash unACKed messages).

    Called once at startup before entering the main XREADGROUP `>` loop.
    Messages with delivery_count > MAX_ATTEMPTS are moved to DLQ.
    Messages within the retry budget are XCLAIM'd back to this consumer and processed.
    """
    try:
        pending_entries = await redis.xpending_range(
            name=stream,
            groupname=group,
            min="-",
            max="+",
            count=200,
            consumername=consumer_name,
        )
    except Exception as exc:
        logger.warning(
            "explanation_worker: PEL drain query failed for %s (continuing): %s",
            consumer_name, exc,
        )
        return

    if not pending_entries:
        return

    logger.info(
        "explanation_worker: draining %d PEL entries for consumer %s",
        len(pending_entries), consumer_name,
    )

    for entry in pending_entries:
        msg_id         = entry["message_id"]
        delivery_count = entry["times_delivered"]

        if delivery_count > MAX_ATTEMPTS:
            # Need to XCLAIM first to get the fields, then move to DLQ
            try:
                claimed = await redis.xclaim(
                    name=stream,
                    groupname=group,
                    consumername=consumer_name,
                    min_idle_time=0,
                    message_ids=[msg_id],
                )
                for _, fields in claimed:
                    await _move_to_dlq(
                        redis, stream, group, msg_id, consumer_name,
                        "max_attempts_exceeded_on_startup_drain", fields or {},
                    )
            except Exception as exc:
                logger.warning(
                    "explanation_worker: PEL drain — DLQ move failed for %s: %s",
                    msg_id, exc,
                )
            continue

        # Re-claim and re-process
        try:
            claimed = await redis.xclaim(
                name=stream,
                groupname=group,
                consumername=consumer_name,
                min_idle_time=0,
                message_ids=[msg_id],
            )
            for claim_id, fields in claimed:
                if not fields:
                    continue
                await _process_explanation_message(
                    redis, consumer_name, claim_id, fields,
                    group, stream, delivery_count=delivery_count,
                )
        except Exception as exc:
            logger.warning(
                "explanation_worker: PEL drain — process failed for %s: %s",
                msg_id, exc,
            )


async def _pel_housekeeping(
    redis: Any,
    stream: str,
    group: str,
    consumer_name: str,
) -> None:
    """
    Periodically scan the PEL for entries idle > _PEL_IDLE_THRESHOLD_MS and
    re-claim them for this consumer.  This handles rate-limited messages that
    were not ACKed (they sit in PEL until re-claimed).

    Entries that exceed MAX_ATTEMPTS are moved to DLQ.
    """
    while True:
        try:
            await asyncio.sleep(_PEL_HOUSEKEEPING_INTERVAL_SECS)

            pending_entries = await redis.xpending_range(
                name=stream,
                groupname=group,
                min="-",
                max="+",
                count=50,
                consumername=consumer_name,
                idle=_PEL_IDLE_THRESHOLD_MS,
            )

            if not pending_entries:
                continue

            for entry in pending_entries:
                msg_id         = entry["message_id"]
                delivery_count = entry["times_delivered"]

                if delivery_count > MAX_ATTEMPTS:
                    try:
                        claimed = await redis.xclaim(
                            name=stream,
                            groupname=group,
                            consumername=consumer_name,
                            min_idle_time=_PEL_IDLE_THRESHOLD_MS,
                            message_ids=[msg_id],
                        )
                        for _, fields in claimed:
                            await _move_to_dlq(
                                redis, stream, group, msg_id, consumer_name,
                                "max_attempts_exceeded_in_housekeeping", fields or {},
                            )
                    except Exception as exc:
                        logger.warning(
                            "explanation_worker: housekeeping DLQ move failed for %s: %s",
                            msg_id, exc,
                        )
                    continue

                try:
                    claimed = await redis.xclaim(
                        name=stream,
                        groupname=group,
                        consumername=consumer_name,
                        min_idle_time=_PEL_IDLE_THRESHOLD_MS,
                        message_ids=[msg_id],
                    )
                    for claim_id, fields in claimed:
                        if not fields:
                            continue
                        logger.info(
                            "explanation_worker: housekeeping re-claiming message %s "
                            "(delivery_count=%d) for consumer %s",
                            claim_id, delivery_count, consumer_name,
                        )
                        await _process_explanation_message(
                            redis, consumer_name, claim_id, fields,
                            group, stream, delivery_count=delivery_count,
                        )
                except Exception as exc:
                    logger.warning(
                        "explanation_worker: housekeeping re-claim failed for %s: %s",
                        msg_id, exc,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("explanation_worker: PEL housekeeping error: %s", exc)


# ── Message processors ────────────────────────────────────────────────────────

async def _process_explanation_message(
    redis: Any,
    consumer_name: str,
    msg_id: str,
    fields: dict,
    group: str,
    stream: str,
    delivery_count: int = 1,
) -> None:
    """Process a single explanation job from the stream."""
    try:
        suggestion_id  = fields.get("suggestion_id", "")
        suggestion_db_id = int(fields.get("id", 0))
        instrument_key = fields.get("instrument_key", "")
        if not suggestion_id:
            raise ValueError("missing suggestion_id")
    except (ValueError, TypeError) as exc:
        logger.warning(
            "explanation_worker[%s]: malformed message %s: %s", consumer_name, msg_id, exc
        )
        await redis.xack(stream, group, msg_id)
        return

    if delivery_count > MAX_ATTEMPTS:
        await _move_to_dlq(
            redis, stream, group, msg_id, consumer_name,
            "max_attempts_exceeded", fields,
        )
        return

    logger.info(
        "explanation_worker[%s]: processing suggestion %s (delivery=%d/%d)",
        consumer_name, suggestion_id, delivery_count, MAX_ATTEMPTS,
    )

    try:
        await _generate_explanation(suggestion_id, suggestion_db_id)
        await redis.xack(stream, group, msg_id)

    except asyncio.CancelledError:
        raise

    except GeminiRateLimitError:
        # Not ACKing — message stays in PEL for housekeeping to re-deliver
        logger.warning(
            "explanation_worker[%s]: rate-limited for suggestion %s "
            "— not ACKing, housekeeping will re-deliver after %ds idle",
            consumer_name, suggestion_id, _PEL_IDLE_THRESHOLD_MS // 1000,
        )

    except GeminiQuotaExhausted:
        # Daily quota exhausted — move straight to DLQ
        await _move_to_dlq(
            redis, stream, group, msg_id, consumer_name,
            "gemini_quota_exhausted", fields,
        )

    except Exception as exc:
        logger.error(
            "explanation_worker[%s]: attempt failed for suggestion %s "
            "(delivery=%d/%d): %s",
            consumer_name, suggestion_id, delivery_count, MAX_ATTEMPTS, exc,
        )
        # Not ACKing — stays in PEL; housekeeping will re-deliver for retry


async def _process_context_message(
    redis: Any,
    consumer_name: str,
    msg_id: str,
    fields: dict,
    group: str,
    stream: str,
    delivery_count: int = 1,
) -> None:
    """Process a single instrument context generation job from the stream.

    ``delivery_count`` is the number of times this message has been delivered
    (sourced from the PEL at re-claim time or from xpending_range at startup).
    New messages delivered via XREADGROUP '>' always start at 1.

    Retry policy:
      - GeminiRateLimitError: NOT ACKed — stays in PEL, housekeeping retries after 60 s idle.
      - GeminiQuotaExhausted: ACK + abandon — daily quota is exhausted; retrying is pointless.
      - Other errors: NOT ACKed up to _MAX_CONTEXT_ATTEMPTS, then ACK + abandon.
      - delivery_count > _MAX_CONTEXT_ATTEMPTS: ACK + abandon immediately.
    Context jobs have no DLQ: they are best-effort and the user gets a fresh generation
    the next time the watchlist item is opened (Stage 3 re-triggers via ai_stream.py).
    """
    # ── Parse fields ──────────────────────────────────────────────────────────
    try:
        instrument_key = fields.get("instrument_key", "")
        sym = fields.get("symbol") or None
        if sym == "":
            sym = None
        lock_key   = fields.get("lock_key") or None
        lock_token = fields.get("lock_token") or None
        prediction_data_raw = fields.get("prediction_data", "")

        ml_snapshot: dict | None = None
        if prediction_data_raw:
            try:
                ml_snapshot = json.loads(prediction_data_raw)
            except (json.JSONDecodeError, TypeError):
                pass

        force  = fields.get("force",  "0") == "1"
        source = fields.get("source", "on_demand")

        if not instrument_key:
            raise ValueError("missing instrument_key")
    except (ValueError, TypeError) as exc:
        logger.warning(
            "context_worker[%s]: malformed message %s: %s", consumer_name, msg_id, exc
        )
        await redis.xack(stream, group, msg_id)
        return

    # ── Delivery cap — ACK and abandon to prevent unbounded retries ───────────
    if delivery_count > _MAX_CONTEXT_ATTEMPTS:
        logger.error(
            "context_worker[%s]: abandoning context job %s for instrument=%s "
            "after %d/%d deliveries — ACKing to clear PEL "
            "(user will get fresh context on next watchlist open)",
            consumer_name, msg_id, instrument_key, delivery_count, _MAX_CONTEXT_ATTEMPTS,
        )
        await redis.xack(stream, group, msg_id)
        return

    # ── Per-instrument guard keys ──────────────────────────────────────────────
    # Defined before the try block so both guard clauses and exception handlers
    # share the same key names without repeated f-string construction.
    recent_key   = f"cortex:context:recent:{instrument_key}"
    cooldown_key = f"cortex:context:cooldown:{instrument_key}"

    # Post-success dedup: if this instrument was successfully processed within
    # _RECENT_SUCCESS_TTL_SECS, the current message is a duplicate (e.g. scheduler
    # double-enqueue).  ACK and skip so the PEL stays clean.
    if await redis.exists(recent_key):
        logger.info(
            "context_worker[%s]: skipping duplicate job for %s "
            "(successfully processed within last %ds) — ACKing",
            consumer_name, instrument_key, _RECENT_SUCCESS_TTL_SECS,
        )
        await redis.xack(stream, group, msg_id)
        return

    # Transient cooldown: if a 5xx / network exhaustion was recorded for this
    # instrument, leave the message in PEL and wait for the backoff to expire
    # before retrying.  This prevents a burst of duplicate messages from hammering
    # an overloaded Gemini endpoint back-to-back.
    if await redis.exists(cooldown_key):
        ttl = await redis.ttl(cooldown_key)
        logger.debug(
            "context_worker[%s]: %s in transient cooldown (%ds remaining) — "
            "not ACKing, housekeeping will re-deliver after cooldown lifts",
            consumer_name, instrument_key, max(ttl, 0),
        )
        return

    logger.info(
        "context_worker[%s]: processing context for %s "
        "(delivery=%d/%d, force=%s, source=%s)",
        consumer_name, instrument_key, delivery_count, _MAX_CONTEXT_ATTEMPTS,
        force, source,
    )

    try:
        await _generate_instrument_context(
            instrument_key, sym, ml_snapshot, lock_key, lock_token, force=force
        )
        await redis.xack(stream, group, msg_id)
        # Record success so duplicate queued jobs are skipped within the dedup window.
        await redis.set(recent_key, "1", ex=_RECENT_SUCCESS_TTL_SECS)

    except asyncio.CancelledError:
        raise

    except LLMTransientExhausted:
        # 5xx / network / timeout — Gemini is temporarily overloaded.  Set a cooldown
        # so subsequent duplicate messages for this instrument are skipped until the
        # endpoint recovers, then leave this message in PEL for housekeeping to
        # re-deliver once both the cooldown and PEL idle threshold have elapsed.
        await redis.set(cooldown_key, "1", ex=_TRANSIENT_COOLDOWN_TTL_SECS)
        logger.warning(
            "context_worker[%s]: transient LLM failure for %s (delivery=%d/%d) — "
            "cooldown set for %ds, not ACKing",
            consumer_name, instrument_key, delivery_count, _MAX_CONTEXT_ATTEMPTS,
            _TRANSIENT_COOLDOWN_TTL_SECS,
        )

    except GeminiRateLimitError:
        logger.warning(
            "context_worker[%s]: rate-limited for %s (delivery=%d/%d) — "
            "not ACKing, housekeeping will re-deliver after %ds idle",
            consumer_name, instrument_key, delivery_count, _MAX_CONTEXT_ATTEMPTS,
            _PEL_IDLE_THRESHOLD_MS // 1000,
        )

    except GeminiQuotaExhausted:
        await redis.xack(stream, group, msg_id)
        logger.error(
            "context_worker[%s]: quota exhausted for %s — ACKing and abandoning "
            "until quota resets at midnight PT",
            consumer_name, instrument_key,
        )

    except Exception as exc:
        logger.error(
            "context_worker[%s]: attempt %d/%d failed for %s: %s",
            consumer_name, delivery_count, _MAX_CONTEXT_ATTEMPTS, instrument_key, exc,
        )
        # Not ACKing — stays in PEL; housekeeping re-delivers after _PEL_IDLE_THRESHOLD_MS


# ── DLQ quota recovery ────────────────────────────────────────────────────────

# Redis key prefix for per-suggestion requeue dedup guard (SET NX, 48-hour TTL).
# Prevents the same DLQ entry from being re-added to the jobs stream more than
# once per quota cycle even if the recovery function runs multiple times.
_DLQ_REQUEUE_DEDUP_TTL_SECS = 172_800  # 48 h — spans current + next quota day


async def _requeue_quota_dlq_entries(redis: Any, *, trigger: str) -> int:
    """
    Scan the explanation DLQ for ``gemini_quota_exhausted`` entries from the
    previous quota day and re-publish them to the explanation jobs stream.

    Called in two scenarios:
      - ``trigger="boot"``        — worker startup; only requeues entries whose
                                    ``moved_at`` timestamp falls before today's
                                    Pacific Time midnight (safe because the Gemini
                                    RPD quota boundary is midnight PT).
      - ``trigger="quota_reset"`` — fired by the pub/sub signal from
                                    GeminiRequestManager after midnight PT reset;
                                    same cutoff applies (reset fires after midnight
                                    so all previous-day entries qualify).

    De-duplication: a per-suggestion Redis key (SET NX, 48 h TTL) prevents the
    same suggestion from being requeued twice within a single quota cycle, even
    if this function is called multiple times (e.g. boot AND pub/sub signal fire
    in the same session after a restart near midnight).

    Returns the number of entries successfully requeued.
    """
    pt = ZoneInfo("America/Los_Angeles")
    today_midnight_pt = datetime.now(pt).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=pt
    )
    requeued = 0
    last_id = "0-0"

    while True:
        try:
            entries = await redis.xrange(
                RedisStreams.EXPLANATION_DLQ,
                min=last_id,
                count=50,
            )
        except Exception as exc:
            logger.error(
                "explanation_worker: DLQ quota recovery — XRANGE failed: %s "
                "(partial recovery may have occurred, requeued=%d so far)",
                exc, requeued,
            )
            break

        if not entries:
            break

        for msg_id, fields in entries:
            last_id = msg_id  # advance cursor regardless of match

            if fields.get("reason") != "gemini_quota_exhausted":
                continue

            # Only requeue entries from before today's PT midnight — entries
            # from the current quota day may still be within an active outage.
            moved_at_str = fields.get("moved_at", "")
            try:
                moved_at = datetime.fromisoformat(moved_at_str)
                if moved_at.astimezone(pt) >= today_midnight_pt:
                    continue
            except (ValueError, TypeError, AttributeError):
                logger.warning(
                    "explanation_worker: DLQ recovery — skipping entry %s "
                    "with unparseable moved_at=%r",
                    msg_id, moved_at_str,
                )
                continue

            # Recover the original job fields.
            try:
                original_fields = json.loads(fields.get("fields", "{}"))
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    "explanation_worker: DLQ recovery — skipping entry %s "
                    "with malformed fields payload",
                    msg_id,
                )
                continue

            suggestion_id = original_fields.get("suggestion_id", "")
            if not suggestion_id:
                continue

            # Dedup guard: skip if already requeued in this quota cycle.
            dedup_key = f"cortex:gemini:dlq:requeued:{suggestion_id}"
            try:
                acquired = await redis.set(
                    dedup_key, "1", nx=True, ex=_DLQ_REQUEUE_DEDUP_TTL_SECS
                )
                if acquired is None:
                    # Key already existed — this suggestion was already requeued.
                    logger.debug(
                        "explanation_worker: DLQ recovery — suggestion %s already "
                        "requeued this cycle (dedup key exists), skipping.",
                        suggestion_id,
                    )
                    continue
            except Exception as exc:
                logger.warning(
                    "explanation_worker: DLQ recovery — dedup key check failed "
                    "for suggestion %s: %s — proceeding without dedup guard.",
                    suggestion_id, exc,
                )

            # Re-publish to the jobs stream.
            try:
                await redis.xadd(
                    RedisStreams.EXPLANATION_JOBS,
                    original_fields,
                    maxlen=_STREAM_MAXLEN_EXPLANATION,
                    approximate=True,
                )
                requeued += 1
                gemini_dlq_requeue_total.labels(trigger=trigger).inc()
                logger.info(
                    "explanation_worker: DLQ quota recovery — requeued suggestion "
                    "%s (originally DLQ'd %s, trigger=%s)",
                    suggestion_id, moved_at_str, trigger,
                )
            except Exception as exc:
                logger.error(
                    "explanation_worker: DLQ recovery — XADD failed for suggestion "
                    "%s: %s",
                    suggestion_id, exc,
                )
                # Undo the dedup key so a retry can attempt this entry again.
                try:
                    await redis.delete(dedup_key)
                except Exception:
                    pass

        if len(entries) < 50:
            break

    if requeued > 0:
        logger.info(
            "explanation_worker: DLQ quota recovery complete — %d suggestion(s) "
            "requeued to jobs stream (trigger=%s)",
            requeued, trigger,
        )

    return requeued


async def _quota_reset_listener(redis: Any) -> None:
    """
    Subscribe to the Gemini quota reset pub/sub channel and auto-requeue
    DLQ entries whenever the nightly circuit reset fires.

    This is the realtime complement to the boot-time DLQ scan: it handles the
    case where the process stays running through midnight PT (no restart) but
    the quota resets and previously-DLQ'd explanations can now be retried.

    Runs as a background task inside ``explanation_worker()``; cancelled cleanly
    on shutdown.  Crashes are logged and the task self-heals with a 30s delay.
    """
    while True:
        ps = None
        try:
            ps = redis.pubsub()
            await ps.subscribe(RedisChannels.GEMINI_QUOTA_RESET)
            logger.debug(
                "explanation_worker: subscribed to %s for DLQ auto-recovery",
                RedisChannels.GEMINI_QUOTA_RESET,
            )
            async for message in ps.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    keys_reset = payload.get("keys_reset", 0)
                    reset_at = payload.get("reset_at", "unknown")
                except (json.JSONDecodeError, TypeError, AttributeError):
                    payload = {}
                    keys_reset = 0
                    reset_at = "unknown"

                logger.info(
                    "explanation_worker: received Gemini quota reset signal "
                    "(keys_reset=%d reset_at=%s) — scanning DLQ for recovery",
                    keys_reset, reset_at,
                )
                await _requeue_quota_dlq_entries(redis, trigger="quota_reset")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "explanation_worker: quota reset listener error: %s — "
                "restarting in 30 s",
                exc,
            )
            await asyncio.sleep(30.0)
        finally:
            if ps is not None:
                with contextlib.suppress(Exception):
                    await ps.unsubscribe(RedisChannels.GEMINI_QUOTA_RESET)
                with contextlib.suppress(Exception):
                    await ps.aclose()


# ── Worker entry points ───────────────────────────────────────────────────────

async def explanation_worker(worker_id: int = 0) -> None:
    """
    Persistent Redis Streams consumer for LLM explanation jobs.

    Reads from ``cortex:stream:explanation:jobs`` using XREADGROUP for
    at-least-once delivery.  Two instances run in parallel (worker_id 0 and 1)
    spawned by ``main.py`` lifespan.

    Startup sequence:
      1. PEL drain — re-process any unACKed messages from before the last restart.
      2. Main loop — XREADGROUP `>` with 5s BLOCK; processes one message at a time.
      3. PEL housekeeping — separate background coroutine reclaims idle PEL entries.

    Retry policy:
      - GeminiRateLimitError: NOT ACKed, stays in PEL, housekeeping re-delivers after 60s.
      - GeminiQuotaExhausted: moved straight to DLQ with failed-state UI notification.
      - Other errors: NOT ACKed, PEL retry, DLQ after MAX_ATTEMPTS deliveries.
    """
    from app.core.redis import get_redis as _get_redis

    consumer_name = f"explanation-worker-{worker_id}"
    stream = RedisStreams.EXPLANATION_JOBS
    group  = _CONSUMER_GROUP

    _redis = _get_redis()

    # Boot-time DLQ recovery (worker_id=0 only — prevents duplicate requeues when
    # both workers start simultaneously; worker 1 starts its listener for realtime
    # recovery instead).
    if worker_id == 0:
        await _requeue_quota_dlq_entries(_redis, trigger="boot")

    # PEL drain before entering the main loop
    await _drain_pel(_redis, stream, group, consumer_name)

    logger.info(
        "explanation_worker[%d]: consumer=%s ready", worker_id, consumer_name
    )

    housekeeping_task = asyncio.create_task(
        _pel_housekeeping(_redis, stream, group, consumer_name),
        name=f"explanation_pel_housekeeping_{worker_id}",
    )

    # Realtime DLQ recovery: listen for midnight PT quota resets from the manager.
    # Only worker 0 subscribes — one listener per process is sufficient.
    quota_listener_task: asyncio.Task | None = None
    if worker_id == 0:
        quota_listener_task = asyncio.create_task(
            _quota_reset_listener(_redis),
            name="gemini_quota_reset_dlq_listener",
        )

    try:
        while True:
            try:
                messages = await _redis.xreadgroup(
                    groupname=group,
                    consumername=consumer_name,
                    streams={stream: ">"},
                    count=1,
                    block=_STREAM_BLOCK_MS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "explanation_worker[%d]: Redis read error: %s — reconnecting in %ds",
                    worker_id, exc, _RECONNECT_DELAY_SECS,
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECS)
                _redis = _get_redis()
                continue

            if not messages:
                continue

            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    await _process_explanation_message(
                        _redis, consumer_name, msg_id, fields, group, stream,
                    )

    except asyncio.CancelledError:
        logger.info("explanation_worker[%d]: cancelled — shutting down", worker_id)
        raise
    finally:
        housekeeping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await housekeeping_task
        if quota_listener_task is not None:
            quota_listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await quota_listener_task


async def _context_pel_housekeeping(
    redis: Any,
    stream: str,
    group: str,
    consumer_name: str,
) -> None:
    """
    Periodically re-claim context PEL entries idle > _PEL_IDLE_THRESHOLD_MS.

    Mirrors _pel_housekeeping for explanation jobs but without a DLQ path:
    context jobs are best-effort and idempotent.  After _MAX_CONTEXT_ATTEMPTS
    the re-claimed message is ACKed and abandoned by _process_context_message.

    The current delivery count from xpending_range is passed through to
    _process_context_message so the processor can enforce the retry cap and
    include accurate attempt numbers in structured log output.
    """
    while True:
        try:
            await asyncio.sleep(_PEL_HOUSEKEEPING_INTERVAL_SECS)

            pending_entries = await redis.xpending_range(
                name=stream,
                groupname=group,
                min="-",
                max="+",
                count=50,
                consumername=consumer_name,
                idle=_PEL_IDLE_THRESHOLD_MS,
            )

            if not pending_entries:
                continue

            for entry in pending_entries:
                msg_id         = entry["message_id"]
                delivery_count = entry["times_delivered"]
                try:
                    claimed = await redis.xclaim(
                        name=stream,
                        groupname=group,
                        consumername=consumer_name,
                        min_idle_time=_PEL_IDLE_THRESHOLD_MS,
                        message_ids=[msg_id],
                    )
                    for claim_id, fields in claimed:
                        if not fields:
                            continue
                        logger.info(
                            "context_worker: housekeeping re-claiming message %s "
                            "(delivery_count=%d/%d) for consumer %s",
                            claim_id, delivery_count, _MAX_CONTEXT_ATTEMPTS, consumer_name,
                        )
                        await _process_context_message(
                            redis, consumer_name, claim_id, fields, group, stream,
                            delivery_count=delivery_count,
                        )
                except Exception as exc:
                    logger.warning(
                        "context_worker: housekeeping re-claim failed for %s: %s",
                        msg_id, exc,
                    )

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("context_worker: PEL housekeeping error: %s", exc)


async def context_worker() -> None:
    """
    Persistent Redis Streams consumer for instrument context generation jobs.

    Reads from ``cortex:stream:context:jobs`` using XREADGROUP.  A single instance
    is sufficient because context jobs are low-frequency and already protected by
    the distributed lock in ai_stream.py Stage 3.

    Startup sequence:
      1. PEL drain — re-process any unACKed messages from before the last restart,
         passing the PEL delivery_count through so the processor can enforce the
         retry cap and skip redundant LLM calls via the DB idempotency check.
      2. Main loop — XREADGROUP '>' with 5 s BLOCK; processes one message at a time.
      3. PEL housekeeping — background coroutine reclaims idle PEL entries every 30 s.

    Retry policy:
      - GeminiRateLimitError: NOT ACKed — housekeeping re-delivers after 60 s idle.
      - GeminiQuotaExhausted: ACK + abandon — no point retrying an exhausted quota.
      - Other failures: NOT ACKed, up to _MAX_CONTEXT_ATTEMPTS (5), then ACK + abandon.
      - No DLQ: context is best-effort; the user gets fresh context on the next
        watchlist page open (Stage 3 in ai_stream.py re-triggers generation).
    """
    from app.core.redis import get_redis as _get_redis

    consumer_name = "context-worker-0"
    stream = RedisStreams.CONTEXT_JOBS
    group  = _CONSUMER_GROUP

    _redis = _get_redis()

    # ── PEL drain on startup ──────────────────────────────────────────────────
    # Re-process any messages that were delivered but not ACKed before the last
    # restart (e.g. process killed between Phase 3 completion and XACK).
    # delivery_count from xpending_range is passed through so the processor can
    # enforce the retry cap and avoid redundant LLM calls via the idempotency check.
    try:
        pending_entries = await _redis.xpending_range(
            name=stream, groupname=group, min="-", max="+", count=50,
            consumername=consumer_name,
        )
        if pending_entries:
            logger.info(
                "context_worker: draining %d PEL entries on startup", len(pending_entries)
            )
            for entry in pending_entries:
                msg_id         = entry["message_id"]
                delivery_count = entry["times_delivered"]
                try:
                    claimed = await _redis.xclaim(
                        name=stream, groupname=group, consumername=consumer_name,
                        min_idle_time=0, message_ids=[msg_id],
                    )
                    for claim_id, fields in claimed:
                        if fields:
                            await _process_context_message(
                                _redis, consumer_name, claim_id, fields, group, stream,
                                delivery_count=delivery_count,
                            )
                except Exception as exc:
                    logger.warning(
                        "context_worker: PEL drain failed for message %s: %s", msg_id, exc
                    )
    except Exception as exc:
        logger.warning("context_worker: PEL drain query failed (continuing): %s", exc)

    logger.info("context_worker: consumer=%s ready", consumer_name)

    housekeeping_task = asyncio.create_task(
        _context_pel_housekeeping(_redis, stream, group, consumer_name),
        name="context_pel_housekeeping",
    )

    try:
        while True:
            try:
                messages = await _redis.xreadgroup(
                    groupname=group,
                    consumername=consumer_name,
                    streams={stream: ">"},
                    count=1,
                    block=_STREAM_BLOCK_MS,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "context_worker: Redis read error: %s — reconnecting in %ds",
                    exc, _RECONNECT_DELAY_SECS,
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECS)
                _redis = _get_redis()
                continue

            if not messages:
                continue

            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    await _process_context_message(
                        _redis, consumer_name, msg_id, fields, group, stream
                    )

    except asyncio.CancelledError:
        logger.info("context_worker: cancelled — shutting down")
        raise
    finally:
        housekeeping_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await housekeeping_task
