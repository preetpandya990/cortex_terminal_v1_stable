"""
LLM Explanation Worker
======================
Async background tasks that generate plain-English explanations for two use cases:

  1. Trade suggestion explanations
       Triggered via the ``cortex.explanation.jobs`` Kafka topic (Redpanda).
       Generates a signal-specific explanation for a committed TradeSuggestion and
       writes it back to the trade_suggestions table.

  2. Instrument market context
       Triggered via the ``cortex.context.jobs`` Kafka topic.
       Generates an instrument-level market context summary for Watchlist items
       with no active trade suggestion.  Written to ai_instrument_context (upsert).

Pipeline — suggestion explanation
----------------------------------
  1. Receive job from cortex.explanation.jobs (manual-commit consumer)
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
 12. Commit the Kafka offset

Pipeline — instrument market context
--------------------------------------
  1. Receive job from cortex.context.jobs (manual-commit consumer)
  2. RAG retrieve — recent news for the symbol (Phase 1)
  3. LLM structured generation → ExplanationOutput (Phase 2)
  4. Apply same guardrails
  5. Upsert into ai_instrument_context (Phase 3)
  6. Write full payload to per-instrument SSE event store
  7. Append one row to ai_llm_audit_log
  8. Publish routing signal to cortex:llm:context:ready:{instrument_key}
  9. Commit the Kafka offset

Delivery architecture
---------------------
  Durable queueing lives on Kafka topics (Redpanda) with manual-commit consumer
  groups.  This provides:

  - At-least-once delivery: an offset is committed only after the message reaches
    a terminal outcome (success, DLQ, abandon, or a republished retry).  Crash
    before commit → redelivery from the committed offset on restart.
  - No message loss during LLM processing: the topic buffers jobs while a worker
    is busy; the old pub/sub design dropped any PUBLISH fired during the 10–120s
    LLM call window.
  - Retries without blocking the partition: Kafka has no delivery counter, so a
    retryable failure republishes the same payload to the topic tail with
    ``attempts+1`` and a ``not_before`` deadline in the headers, then commits the
    original.  On delivery, a deadline ≤60s away is slept off (aiokafka heartbeats
    in the background — far below the 300s max_poll_interval_ms); a longer one is
    republished to the tail unchanged so the consumer is never evicted from the
    group for sleeping.
  - Dead-letter queue: after MAX_ATTEMPTS the message is published to
    cortex.explanation.dlq, a failed-state event is written to the SSE event
    store, and the browser renders "Analysis unavailable" instead of an eternal
    skeleton.

  Pub/sub is retained only as a lightweight wakeup signal after successful generation.
  The actual payload (including RAG source citations) lives in the per-suggestion
  SSE event store (cortex:sse:events:{suggestion_id}) with a 24-hour TTL.

Design invariants
-----------------
  - Failed generations never block indefinitely.  MAX_ATTEMPTS applies per message;
    after exhaustion the DLQ path fires.
  - Two explanation workers run in parallel (cortex.explanation.jobs has two
    partitions keyed by suggestion_id — no head-of-line blocking).  The context
    worker is a single task (context jobs are low-frequency).
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

from pydantic import BaseModel, Field, model_validator
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
from app.core.kafka import (
    KafkaGroups,
    KafkaTopics,
    get_attempts,
    get_not_before,
    new_consumer,
    pending_count,
    publish,
    retry_headers,
)
from app.core.metrics import (
    explanation_demand_latency_seconds,
    explanation_ungrounded_numbers_total,
    gemini_dlq_requeue_total,
    llm_audit_log_writes_total,
    llm_explanation_dedup_total,
    llm_explanation_dlq_total,
    llm_explanation_duration_seconds,
    llm_explanation_worker_active,
    llm_explanations_total,
    llm_guardrail_events_total,
    llm_ready_publish_failures_total,
    llm_stream_queue_depth,
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

# Retry pacing: retryable failures republish with not_before = now + this delay.
_RETRY_DELAY_SECS = 60

# Demand jobs (WS7): a user is staring at a generating skeleton, so fail fast —
# fewer attempts, short delay, and rate-limit backpressure is TERMINAL (DLQ →
# failed push → PanelFailed with a retry button) instead of a queued retry the
# user can't see.
_DEMAND_MAX_ATTEMPTS = 2
_DEMAND_RETRY_DELAY_SECS = 5
# not_before hybrid rule (see module docstring): a deadline this close is slept
# off in the consumer; anything further is republished to the tail unchanged so
# the consumer never risks max_poll_interval_ms (300s) group eviction.
_NOT_BEFORE_SLEEP_CAP_SECS = 60
# Cadence of the llm_stream_queue_depth gauge tick (consumer-lag based).
_QUEUE_DEPTH_TICK_SECS = 30

# In-flight dedup: longer than _LLM_CALL_TIMEOUT_SECS to cover Phase 3 DB write.
_INFLIGHT_KEY_TTL_SECS = 150

# Context-worker transient backoff: after a 5xx / network exhaustion, skip
# re-processing the same instrument for this many seconds.  The retry republish
# carries not_before = now + this value so redelivery lands after the cooldown.
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


class NewsForecastOutput(BaseModel):
    """News-conditioned forecast fields produced alongside a DEMAND explanation.

    Written back to TradeSuggestion.ai_signal and the shared forecast cache in
    the same transaction as the explanation (WS7) — closing the async-forecast
    race: what the narrative says and what the system knows can no longer
    diverge, and the dead ``sentiment_label`` key finally has a producer.
    """
    direction: str = Field(
        description="Directional lean from the news: BUY, SELL, or HOLD."
    )
    sentiment_label: str = Field(
        description="Overall news sentiment: bullish, bearish, or neutral."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence in the directional lean (0.0-1.0).",
    )
    rationale: str = Field(
        description="One-sentence rationale for the lean, grounded in the articles.",
    )


class MLAssessmentOutput(BaseModel):
    """Structured, machine-consumable read on the ML call itself (WS10/C.B2).

    The demand prompt includes the actual OHLCV price action, so the LLM can
    judge whether the ML's directional call squares with what price is doing.
    Enumerated fields (never free text) feed ``compute_sample_weight`` as an
    additional retraining multiplier — gated behind
    FEEDBACK_LLM_ASSESSMENT_ENABLED and an offline non-regression comparison.
    """
    likely_missed_pattern: str = Field(
        description=(
            "The single most plausible price pattern the ML may have missed, "
            "judged ONLY from the Recent Price Action data: one of none, "
            "trend_reversal, breakout_failure, volume_divergence, news_shock, "
            "range_bound_chop, other."
        ),
    )
    confidence_should_have_been_lower: bool = Field(
        description=(
            "True when the provided price action clearly warranted less "
            "conviction than the ML's stated confidence."
        ),
    )
    price_action_agreement: str = Field(
        description=(
            "Whether recent price action supports the ML direction: "
            "agrees, partial, or contradicts."
        ),
    )

    @model_validator(mode="after")
    def _coerce_enums(self) -> "MLAssessmentOutput":
        """Coerce out-of-vocabulary values to safe defaults instead of
        raising — a flaky enum must never fail the whole generation (the
        narrative rides in the same structured response)."""
        if self.likely_missed_pattern not in _VALID_MISSED_PATTERNS:
            self.likely_missed_pattern = "other"
        if self.price_action_agreement not in _VALID_AGREEMENT:
            self.price_action_agreement = "partial"
        return self


# Version stamp persisted alongside every assessment (LLM-as-judge practice:
# judge-model or prompt changes shift score distributions, so weights from
# different versions are not comparable — the offline retrain comparison must
# be re-run whenever this bumps or the model id changes).
_ASSESSMENT_PROMPT_VERSION = 1

_VALID_MISSED_PATTERNS = frozenset({
    "none", "trend_reversal", "breakout_failure", "volume_divergence",
    "news_shock", "range_bound_chop", "other",
})
_VALID_AGREEMENT = frozenset({"agrees", "partial", "contradicts"})


class DemandExplanationOutput(ExplanationOutput):
    """Extended schema for demand-triggered generations (WS7): one structured
    Gemini call returns the narrative AND the news-forecast fields together,
    plus the ML assessment (WS10) — near-zero marginal tokens since the model
    is already reasoning over the OHLCV summary."""
    news_forecast: NewsForecastOutput
    ml_assessment: MLAssessmentOutput


# ── System prompts (per CORTEX_LLM_UPGRADE_PLAN.md §8.2) ─────────────────────
#
# Composed from shared blocks + per-mode task/guidance suffixes: the two
# prompts were ~95% duplicated text, and a rule fixed in one was easily
# missed in the other. Anything true of BOTH modes lives in a shared block.

_PROMPT_STRUCTURE_BLOCK = """\
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
line rather than padding.\
"""

_PROMPT_RULES_BLOCK = """\
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
5. Output JSON only, matching the response schema exactly.
   The summary is plain text (no headers); full_explanation contains the five sections.\
"""

_SHARED_GUIDANCE_NEWS = """\
- "News context": summarise the retrieved articles and CITE each factual claim inline as
  According to [Source Name, YYYY-MM-DD]... If no articles were provided, state that no
  recent news context was available — never invent sources.\
"""

_EXPLANATION_SYSTEM_PROMPT = f"""\
You are a financial signal analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: explain a machine-generated trade signal in plain English — what the ML
ensemble (XGBoost + GRU) and technical scanner observed, how that evidence combined into
a consensus, and what it implies — grounded strictly in the structured signal data and the
retrieved news articles provided in the prompt.

{_PROMPT_STRUCTURE_BLOCK}

Section guidance:
- "What the models saw": describe the ensemble direction and calibrated confidence, then
  the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities, and how each
  model's conviction compares to its regime-adaptive threshold. Note agreement or
  disagreement between the two models. Use ONLY the numbers given.
- "Technical picture": summarise the scanner readings provided (e.g. RSI, volume ratio,
  price change) and, when a Recent Price Action section is present, how price has actually
  behaved. If neither is present, say so briefly.
{_SHARED_GUIDANCE_NEWS}
- "What this suggests": a neutral synthesis of direction, confidence band and time horizon.
  Describe; do NOT advise.
- "Key risks": what could invalidate the setup (model disagreement, low conviction, thin
  news corroboration, regime, price action contradicting the signal, etc.).

{_PROMPT_RULES_BLOCK}\
"""

_DEMAND_FORECAST_INSTRUCTION = """

Additionally, populate news_forecast from the retrieved articles: direction (BUY/SELL/HOLD
lean the news supports), sentiment_label (bullish/bearish/neutral), confidence (0.0-1.0),
and a one-sentence rationale. Base it ONLY on the provided articles; with no articles,
return direction HOLD, sentiment_label neutral, confidence 0.0.

Also populate ml_assessment by comparing the ML signal against the Recent Price Action
section (judge ONLY from the numbers provided — never outside knowledge):
- likely_missed_pattern: the single most plausible pattern the ML may have missed, one of
  none | trend_reversal | breakout_failure | volume_divergence | news_shock |
  range_bound_chop | other. Use "none" when nothing stands out.
- confidence_should_have_been_lower: true only when the price action clearly warranted
  less conviction than the ML's stated confidence.
- price_action_agreement: agrees | partial | contradicts — whether recent price action
  supports the ML direction. With no Recent Price Action section, return
  likely_missed_pattern none, confidence_should_have_been_lower false, agreement partial.\
"""

_DEMAND_EXPLANATION_SYSTEM_PROMPT = _EXPLANATION_SYSTEM_PROMPT + _DEMAND_FORECAST_INSTRUCTION

_CONTEXT_SYSTEM_PROMPT = f"""\
You are a market context analysis tool for the Cortex algorithmic trading platform.
You are NOT a licensed financial advisor and must not provide investment recommendations.

Your task: explain the current read on a specific NSE-listed stock that has no active trade
signal — what the ML ensemble (XGBoost + GRU) is currently leaning toward and why, and what
recent news is relevant — grounded strictly in the structured model snapshot and the
retrieved news articles provided in the prompt.

{_PROMPT_STRUCTURE_BLOCK}

Section guidance:
- "What the models saw": describe the ensemble's current direction and calibrated
  confidence, then the per-model (XGBoost and GRU) directions, buy/sell/hold probabilities,
  and conviction-vs-threshold. Note agreement or disagreement. Use ONLY the numbers given.
  If no model snapshot is provided, state that no live model read was available.
- "Technical picture": summarise volatility / market-regime signals provided, if any.
{_SHARED_GUIDANCE_NEWS}
- "What this suggests": a neutral synthesis of the current lean. Describe; do NOT advise.
- "Key risks": model disagreement, low conviction, thin news corroboration, volatility, etc.

{_PROMPT_RULES_BLOCK}\
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


# Numeric tokens the grounding check verifies: percentages ("62%", "3.5%")
# and RSI readings ("RSI 71", "RSI-14: 71.2"). Deliberately limited to these
# two families (full numeric NLI would be over-engineering): they are the
# figures the prompt always provides exactly, so any novel one is invented.
_GROUNDING_TOKEN_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%"
    r"|RSI(?:-14)?[\s:]*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _number_variants(num: str) -> set[str]:
    """String forms under which a numeric literal may appear in the prompt."""
    variants = {num}
    try:
        value = float(num)
        variants.add(f"{value:.0f}")
        variants.add(f"{value:.1f}")
        variants.add(f"{value:.2f}")
        if value == int(value):
            variants.add(str(int(value)))
    except ValueError:
        pass
    return variants


def _strip_ungrounded_numbers(text: str, prompt: str) -> tuple[str, int]:
    """
    Remove sentences citing a %/RSI figure that does not appear in the prompt
    (§1: a hallucinated confidence % or RSI value passed all guardrails
    silently). Same per-line mechanics as ``_strip_price_predictions`` so the
    markdown section structure survives. Returns ``(filtered_text, n_removed)``.
    """
    removed = 0
    clean_lines: list[str] = []
    for line in text.split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            clean_lines.append(line)
            continue
        kept: list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            grounded = True
            for match in _GROUNDING_TOKEN_RE.finditer(sentence):
                num = match.group(1) or match.group(2)
                if not any(v in prompt for v in _number_variants(num)):
                    grounded = False
                    break
            if grounded:
                kept.append(sentence)
            else:
                removed += 1
                explanation_ungrounded_numbers_total.inc()
                logger.warning(
                    "explanation_worker: ungrounded-number guardrail removed "
                    "sentence: %.100s...",
                    sentence,
                )
        clean_lines.append(" ".join(kept))
    return "\n".join(clean_lines), removed


def _apply_guardrails(
    output: ExplanationOutput,
    has_context: bool,
    prompt: str = "",
) -> tuple[ExplanationOutput, list[str]]:
    """
    Apply all output guardrails.  Returns the (possibly modified) output and
    a list of guardrail event names that fired.
    """
    events: list[str] = []

    full_explanation, n_removed = _strip_price_predictions(output.full_explanation)
    events.extend(["price_prediction_filter"] * n_removed)

    if prompt:
        full_explanation, n_ungrounded = _strip_ungrounded_numbers(
            full_explanation, prompt
        )
        events.extend(["ungrounded_number_filter"] * n_ungrounded)

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

    # model_copy (not reconstruction) so subclass fields — e.g. the demand
    # path's news_forecast — survive the guardrail pass.
    return (
        output.model_copy(update={
            "summary": clean_summary or output.summary,
            "full_explanation": full_explanation_with_disclaimer,
        }),
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


def _summarize_price_action(candles: list[dict]) -> list[str]:
    """
    Compact, token-conscious price-action summary from daily OHLCV candles
    (C.A): trend, swing high/low, realized volatility, last-5-bar behavior —
    ~6 text lines, never a raw per-bar dump. Pure function; empty input or
    too few bars yields an honest one-liner instead of fabricated statistics.
    """
    if len(candles) < 5:
        return ["Insufficient OHLCV history for a price-action summary."]

    closes = [float(c["close"]) for c in candles]
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]

    first, last = closes[0], closes[-1]
    window_change_pct = (last - first) / first * 100 if first else 0.0
    swing_high, swing_low = max(highs), min(lows)

    daily_returns = [
        (closes[i] - closes[i - 1]) / closes[i - 1]
        for i in range(1, len(closes))
        if closes[i - 1]
    ]
    if daily_returns:
        mean_r = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns)
        daily_vol_pct = variance ** 0.5 * 100
    else:
        daily_vol_pct = 0.0

    last5 = closes[-5:]
    last5_change_pct = (last5[-1] - last5[0]) / last5[0] * 100 if last5[0] else 0.0
    up_days = sum(1 for i in range(1, len(last5)) if last5[i] > last5[i - 1])

    return [
        f"Window: {len(candles)} daily bars",
        f"Close: {last:.2f} ({window_change_pct:+.1f}% over the window)",
        f"Swing high/low: {swing_high:.2f} / {swing_low:.2f}",
        f"Realized daily volatility: {daily_vol_pct:.1f}%",
        f"Last 5 bars: {last5_change_pct:+.1f}% ({up_days}/4 up days)",
    ]


def _format_demand_context(chunks: list, max_articles: int, max_chars: int) -> str:
    """
    Full-article context block for demand generations: the top ``max_articles``
    retrieved chunks with bodies truncated to ``max_chars`` each, rendered
    through the standard [Source: ...] header format so inline citations stay
    verifiable against the audit log.
    """
    import dataclasses

    capped = [
        dataclasses.replace(chunk, content=chunk.content[:max_chars])
        for chunk in chunks[:max_articles]
    ]
    return format_context(capped)


def _build_explanation_prompt(
    suggestion: TradeSuggestion,
    context: str,
    price_action: list[str] | None = None,
) -> str:
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
    lines.append("")
    lines.append("## Technical Scanner Readings")
    if scanner_lines:
        lines.extend(scanner_lines)
    else:
        # §2: an omitted section let the LLM confuse "missing" with
        # "genuinely neutral" — state unavailability explicitly instead.
        lines.append(
            "Technical scanner data unavailable — do not infer or invent "
            "technical readings."
        )

    if price_action:
        lines.append("")
        lines.append("## Recent Price Action (daily bars)")
        lines.extend(price_action)

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
        if ml_snapshot.get("prediction_generated_at"):
            # §3: the snapshot can be up to 60s old at dispatch — surface its
            # actual age so the narrative (and audit) reflect reality.
            lines.append(f"(Snapshot as of {ml_snapshot['prediction_generated_at']})")
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
    *,
    trigger: str = "auto",
) -> None:
    """
    Execute the full explanation pipeline for one suggestion.

    Structured as three distinct phases to eliminate the DB connection leak:
      Phase 1 — DB read + idempotency checks; session closed before LLM call.
      Phase 2 — LLM call: outside any DB session; bounded by _LLM_CALL_TIMEOUT_SECS.
      Phase 3 — DB write + SSE event store write + PUBLISH routing signal.

    ``trigger == "demand"`` (WS7) upgrades the generation: full article bodies
    + an OHLCV price-action summary go into the prompt, ONE structured Gemini
    call returns narrative + news-forecast fields, and Phase 3 additionally
    writes the forecast back to TradeSuggestion.ai_signal and the shared
    forecast cache — closing the async-forecast race for this suggestion.

    Raises on unrecoverable errors so the consumer can decide ACK vs PEL.
    Raises GeminiRateLimitError so the consumer leaves the message in PEL
    (no real LLM attempt was made so the delivery counter is not incremented).
    """
    from app.core.config import get_settings
    from app.core.database import AsyncSessionLocal
    from app.core.redis import get_redis
    from app.services.regime_service import RegimeService

    _redis = get_redis()
    client = get_intelligence_client()
    settings = get_settings()
    invocation_id = uuid4()
    suggestion_uuid = UUID(suggestion_id)
    is_demand = trigger == "demand"

    # ── Phase 1: DB read — closed before LLM call ────────────────────────────
    chunks: list = []
    prompt: str = ""
    prompt_hash: str = ""
    source_refs: list[dict] = []
    suggestion_symbol: str = ""
    suggestion_instrument_key: str = ""
    suggestion_signal_direction: str | None = None
    suggestion_signal_generated_at: str | None = None
    # Demand write-back inputs captured while the session is open.
    captured_ai_signal: dict[str, Any] = {}
    captured_indicators: dict[str, Any] = {}
    forecast_cache_symbol: str = ""

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

        price_action: list[str] | None = None
        if is_demand:
            # Full article bodies (capped) instead of the standard context
            # block, plus the OHLCV price-action summary (C.A). Failures
            # degrade to the standard path — demand must not fail harder
            # than legacy over an optional enrichment.
            context = _format_demand_context(
                chunks,
                settings.EXPLANATION_MAX_ARTICLES,
                settings.EXPLANATION_ARTICLE_MAX_CHARS,
            )
            try:
                candles = await RegimeService.load_ohlcv(
                    db, suggestion.instrument_key, limit=60
                )
                price_action = _summarize_price_action(candles)
            except Exception as exc:
                logger.warning(
                    "explanation_worker: OHLCV load failed for %s "
                    "(continuing without price action): %s",
                    suggestion_id, exc,
                )
            # Write-back inputs: the forecast cache key must be derived
            # through the SHARED builder over the same inputs the assembler
            # hashes (events + rounded indicators).
            captured_ai_signal = dict(suggestion.ai_signal or {})
            captured_indicators = (suggestion.ml_signal or {}).get("indicators") or {}
            # Pathway 1 keys the forecast cache by instrument_key; Pathway 2
            # by trading symbol (mirrors what gather_news_forecast received).
            forecast_cache_symbol = (
                suggestion.instrument_key
                if suggestion.trigger_pathway == "scanner_anomaly"
                else (suggestion.trading_symbol or suggestion.symbol)
            )
        else:
            context = format_context(chunks)

        source_refs = build_retrieval_source_refs(chunks)
        prompt = _build_explanation_prompt(suggestion, context, price_action)
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
                    # Demand jobs return narrative + news-forecast fields in
                    # ONE structured call (WS7); priority HIGH per the
                    # request-manager convention for user-facing explanations.
                    response_model=(
                        DemandExplanationOutput if is_demand else ExplanationOutput
                    ),
                    system=(
                        _DEMAND_EXPLANATION_SYSTEM_PROMPT
                        if is_demand else _EXPLANATION_SYSTEM_PROMPT
                    ),
                    temperature=0.2,
                    max_tokens=1600 if is_demand else 1400,
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
        final_output, guardrail_events = _apply_guardrails(
            raw_output, has_context=bool(chunks), prompt=prompt
        )
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

    demand_forecast: NewsForecastOutput | None = (
        final_output.news_forecast
        if is_demand and isinstance(final_output, DemandExplanationOutput)
        else None
    )

    async with AsyncSessionLocal() as db:
        if final_output is not None:
            now_utc = datetime.now(timezone.utc)
            values: dict[str, Any] = dict(
                llm_summary=final_output.summary,
                llm_explanation=final_output.full_explanation,
                explanation_model=f"{model_provider}/{model_id}",
                explanation_generated_at=now_utc,
                updated_at=now_utc,
            )
            if demand_forecast is not None:
                # WS7 write-back, same transaction as the narrative: the
                # suggestion's ai_signal now reflects what the explanation
                # actually says (closes the §0 propagation gap and finally
                # produces the sentiment_label the prompt reads).
                values["ai_signal"] = {
                    **captured_ai_signal,
                    "direction":       demand_forecast.direction.upper(),
                    "sentiment_label": demand_forecast.sentiment_label.lower(),
                    "rationale":       demand_forecast.rationale,
                    "confidence":      demand_forecast.confidence,
                    "available":       True,
                    "forecast_source": "demand",
                    "refreshed_at":    now_utc.isoformat(),
                }
            if isinstance(final_output, DemandExplanationOutput):
                # ML assessment (WS10/C.B2), with judge identity: model +
                # prompt version travel with every verdict so the retraining
                # gate can detect non-comparable judge generations.
                assessment = final_output.ml_assessment
                values["llm_ml_assessment"] = {
                    "likely_missed_pattern": assessment.likely_missed_pattern,
                    "confidence_should_have_been_lower": (
                        assessment.confidence_should_have_been_lower
                    ),
                    "price_action_agreement": assessment.price_action_agreement,
                    "model": f"{model_provider}/{model_id}",
                    "prompt_version": _ASSESSMENT_PROMPT_VERSION,
                    "assessed_at": now_utc.isoformat(),
                }
            await db.execute(
                update(TradeSuggestion)
                .where(TradeSuggestion.suggestion_id == suggestion_uuid)
                .values(**values)
            )
            await db.commit()

            logger.info(
                "explanation_worker: explanation written for suggestion %s "
                "symbol=%s trigger=%s latency_ms=%d guardrails=%s",
                suggestion_id,
                suggestion_symbol,
                trigger,
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

    # ── Forecast cache write (demand path) ────────────────────────────────────
    # Through the SHARED key builder over the same inputs the assembler hashes
    # — the next consensus cycle for this symbol reuses the demand forecast
    # instead of re-enqueueing a batch job. Best-effort: a miss costs one
    # batch forecast, never correctness.
    if demand_forecast is not None and forecast_cache_symbol:
        try:
            from app.ai.fusion.forecast_cache import forecast_cache_key
            from app.ai.fusion.news_forecaster import score_from_direction

            cache_events = captured_ai_signal.get("events") or []
            cache_value = {
                "score":           score_from_direction(
                    demand_forecast.direction.upper(), demand_forecast.confidence
                ),
                "confidence":      demand_forecast.confidence,
                "available":       True,
                "events":          cache_events,
                "event_count":     captured_ai_signal.get("event_count")
                                   or len(cache_events),
                "direction":       demand_forecast.direction.upper(),
                "sentiment_label": demand_forecast.sentiment_label.lower(),
                "rationale":       demand_forecast.rationale,
                "forecast_source": "demand",
                "model":           f"{model_provider}/{model_id}",
            }
            await _redis.setex(
                forecast_cache_key(
                    forecast_cache_symbol, cache_events, captured_indicators
                ),
                settings.NEWS_FORECAST_CACHE_TTL,
                json.dumps(cache_value, default=str),
            )
        except Exception as exc:
            logger.warning(
                "explanation_worker: demand forecast cache write failed for %s "
                "(non-fatal): %s",
                suggestion_id, exc,
            )

    # ── Write to SSE event store BEFORE publishing routing signal ─────────────
    if final_output is not None:
        if is_demand:
            explanation_demand_latency_seconds.observe(latency_ms / 1000.0)
        sse_payload = {
            "available":           True,
            "failed":              False,
            "status":              "ready",
            "suggestion_id":       suggestion_id,
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

    # ── Clean up inflight keys ────────────────────────────────────────────────
    with contextlib.suppress(Exception):
        await _redis.delete(inflight_key)
    if is_demand:
        # Free the explanation_service demand lock so a later viewer can
        # re-trigger if this attempt ultimately fails (retries hold their own
        # worker inflight key per attempt).
        from app.ai.intelligence.explanation_service import DEMAND_INFLIGHT_KEY
        with contextlib.suppress(Exception):
            await _redis.delete(DEMAND_INFLIGHT_KEY.format(suggestion_id=suggestion_id))

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
        final_output, guardrail_events = _apply_guardrails(
            raw_output, has_context=bool(chunks), prompt=prompt
        )
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


# ── DLQ and retry helpers ─────────────────────────────────────────────────────

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
        "status":              "failed",
        "suggestion_id":       suggestion_id,
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

    # Free the demand-trigger lock so the retry button (or a later view) can
    # re-request generation without waiting for the TTL.
    from app.ai.intelligence.explanation_service import DEMAND_INFLIGHT_KEY
    with contextlib.suppress(Exception):
        await redis.delete(DEMAND_INFLIGHT_KEY.format(suggestion_id=suggestion_id))

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


async def _send_to_dlq(
    redis: Any,
    reason: str,
    payload: dict,
    attempts: int,
) -> None:
    """
    Publish a terminally-failed explanation job to the DLQ topic and write the
    failed-state SSE event so the browser renders "Analysis unavailable".

    Raises on DLQ publish failure — the caller must NOT commit in that case so
    the original message is redelivered (parity with the old no-XACK path).
    """
    suggestion_id = str(payload.get("suggestion_id", "") or "")
    dlq_entry = {
        "original_topic": KafkaTopics.EXPLANATION_JOBS,
        "reason":         reason,
        "fields":         payload,
        "attempts":       attempts,
        "moved_at":       datetime.now(timezone.utc).isoformat(),
    }
    await publish(
        KafkaTopics.EXPLANATION_DLQ,
        dlq_entry,
        key=suggestion_id or None,
    )
    llm_explanation_dlq_total.labels(job_type="explanation").inc()
    logger.error(
        "explanation_worker: suggestion %s moved to DLQ (reason=%s attempts=%d)",
        suggestion_id, reason, attempts,
    )

    instrument_key = str(payload.get("instrument_key", "") or "")
    if suggestion_id:
        await _publish_failed_state(redis, suggestion_id, instrument_key)


async def _republish_retry(
    topic: str,
    payload: dict,
    key: str,
    attempts: int,
    delay_secs: float,
    *,
    not_before: float | None = None,
) -> None:
    """Republish a job to the topic tail with retry headers, for a later attempt."""
    await publish(
        topic,
        payload,
        key=key,
        headers=retry_headers(attempts, delay_secs, not_before=not_before),
    )


async def _honor_not_before(msg: Any, topic: str, key: str) -> bool:
    """
    Enforce the ``not_before`` retry deadline on a delivered message.

    Returns True when the message should be processed now.  A deadline within
    _NOT_BEFORE_SLEEP_CAP_SECS is slept off (aiokafka heartbeats run in a
    background task, so a bounded sleep cannot trigger group eviction); a
    further deadline republishes the message to the tail UNCHANGED (same
    attempts, same deadline) and returns False — the caller commits and moves
    on.  Raises if the republish fails so the caller skips the commit and the
    message is redelivered.
    """
    remaining = get_not_before(msg) - time.time()
    if remaining <= 0:
        return True
    if remaining <= _NOT_BEFORE_SLEEP_CAP_SECS:
        await asyncio.sleep(remaining)
        return True
    await _republish_retry(
        topic, msg.value, key, get_attempts(msg), 0.0, not_before=get_not_before(msg)
    )
    logger.debug(
        "explanation_worker: not_before %.0fs away — republished %s to tail",
        remaining, key,
    )
    return False


async def _queue_depth_ticker(topic: str, group_id: str, stream_label: str) -> None:
    """
    Feed llm_stream_queue_depth{stream=...} from consumer lag every 30s.

    Lag briefly over-counts during retry storms (tail republish inflates end
    offsets) — retried messages are genuinely pending, so this is acceptable.
    """
    while True:
        try:
            depth = await pending_count(topic, group_id)
            llm_stream_queue_depth.labels(stream=stream_label).set(depth)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(
                "explanation_worker: queue depth tick failed for %s: %s",
                stream_label, exc,
            )
        await asyncio.sleep(_QUEUE_DEPTH_TICK_SECS)


# ── Message processors ────────────────────────────────────────────────────────

async def _process_explanation_message(
    redis: Any,
    consumer: Any,
    msg: Any,
    worker_id: int,
) -> None:
    """
    Process one explanation job from the Kafka topic to a terminal outcome,
    then commit its offset.

    Retry policy (attempts header, starts at 0):
      - malformed / missing suggestion_id → commit + skip (poison message)
      - attempts ≥ MAX_ATTEMPTS           → DLQ + failed-state UI + commit
      - GeminiQuotaExhausted              → DLQ + failed-state UI + commit
      - GeminiRateLimitError / other      → republish attempts+1 with a
                                            _RETRY_DELAY_SECS deadline + commit
      - success                           → commit

    Any publish failure inside a handler propagates WITHOUT committing, so the
    message is redelivered after reconnect — no job can be silently dropped.
    """
    payload = msg.value
    if not isinstance(payload, dict) or not payload.get("suggestion_id"):
        logger.warning(
            "explanation_worker[%d]: malformed message at offset %s — skipping",
            worker_id, msg.offset,
        )
        await consumer.commit()
        return

    suggestion_id = str(payload["suggestion_id"])
    try:
        suggestion_db_id = int(payload.get("id", 0))
    except (TypeError, ValueError):
        suggestion_db_id = 0

    # WS7: demand jobs (a user is watching) fail fast; legacy jobs keep the
    # patient 3-attempt/60s policy. The trigger travels in the payload so
    # retries and requeues preserve it.
    trigger = str(payload.get("trigger") or "auto")
    is_demand = trigger == "demand"
    max_attempts = _DEMAND_MAX_ATTEMPTS if is_demand else MAX_ATTEMPTS
    retry_delay = _DEMAND_RETRY_DELAY_SECS if is_demand else _RETRY_DELAY_SECS

    attempts = get_attempts(msg)
    if attempts >= max_attempts:
        await _send_to_dlq(redis, "max_attempts_exceeded", payload, attempts)
        await consumer.commit()
        return

    if not await _honor_not_before(msg, KafkaTopics.EXPLANATION_JOBS, suggestion_id):
        await consumer.commit()
        return

    logger.info(
        "explanation_worker[%d]: processing suggestion %s (trigger=%s attempt=%d/%d)",
        worker_id, suggestion_id, trigger, attempts + 1, max_attempts,
    )

    try:
        await _generate_explanation(suggestion_id, suggestion_db_id, trigger=trigger)
        await consumer.commit()

    except asyncio.CancelledError:
        raise

    except GeminiQuotaExhausted:
        # Daily quota exhausted — straight to DLQ; the quota-reset recovery
        # requeues it after midnight PT.
        await _send_to_dlq(redis, "gemini_quota_exhausted", payload, attempts)
        await consumer.commit()

    except GeminiRateLimitError:
        if is_demand:
            # Backpressure is TERMINAL for demand jobs: the user gets the
            # failed push (PanelFailed + retry button) within the permit
            # timeout instead of a skeleton that outlives their patience.
            await _send_to_dlq(redis, "gemini_rate_limited", payload, attempts)
            await consumer.commit()
            return
        await _republish_retry(
            KafkaTopics.EXPLANATION_JOBS, payload, suggestion_id,
            attempts + 1, retry_delay,
        )
        await consumer.commit()
        logger.warning(
            "explanation_worker[%d]: rate-limited for suggestion %s — "
            "republished for retry in %ds (attempt %d/%d)",
            worker_id, suggestion_id, retry_delay, attempts + 1, max_attempts,
        )

    except Exception as exc:
        await _republish_retry(
            KafkaTopics.EXPLANATION_JOBS, payload, suggestion_id,
            attempts + 1, retry_delay,
        )
        await consumer.commit()
        logger.error(
            "explanation_worker[%d]: attempt %d/%d failed for suggestion %s: %s "
            "— republished for retry in %ds",
            worker_id, attempts + 1, max_attempts, suggestion_id, exc,
            retry_delay,
        )


async def _process_context_message(
    redis: Any,
    consumer: Any,
    msg: Any,
) -> None:
    """Process one instrument context job to a terminal outcome, then commit.

    Retry policy (attempts header, starts at 0; no DLQ — context is best-effort
    and the user gets a fresh generation on the next watchlist open):
      - malformed / missing instrument_key → commit + skip
      - attempts ≥ _MAX_CONTEXT_ATTEMPTS   → abandon + commit
      - recent-success key exists          → duplicate; commit + skip
      - cooldown key exists                → republish SAME attempts with
                                             not_before = now + remaining TTL, commit
      - LLMTransientExhausted              → set cooldown + republish attempts+1
                                             with a cooldown-length deadline, commit
      - GeminiRateLimitError / other       → republish attempts+1, 60s deadline, commit
      - GeminiQuotaExhausted               → abandon + commit
      - success                            → commit + set recent key
    """
    payload = msg.value
    if not isinstance(payload, dict) or not payload.get("instrument_key"):
        logger.warning(
            "context_worker: malformed message at offset %s — skipping", msg.offset
        )
        await consumer.commit()
        return

    instrument_key = str(payload["instrument_key"])
    sym = payload.get("symbol") or None
    if sym == "":
        sym = None
    lock_key   = payload.get("lock_key") or None
    lock_token = payload.get("lock_token") or None
    prediction_data_raw = payload.get("prediction_data", "")

    ml_snapshot: dict | None = None
    if prediction_data_raw:
        try:
            ml_snapshot = json.loads(prediction_data_raw)
        except (json.JSONDecodeError, TypeError):
            pass

    force  = payload.get("force",  "0") == "1"
    source = payload.get("source", "on_demand")

    attempts = get_attempts(msg)

    # ── Attempts cap — abandon to prevent unbounded retries ──────────────────
    if attempts >= _MAX_CONTEXT_ATTEMPTS:
        logger.error(
            "context_worker: abandoning context job for instrument=%s after "
            "%d/%d attempts (user will get fresh context on next watchlist open)",
            instrument_key, attempts, _MAX_CONTEXT_ATTEMPTS,
        )
        await consumer.commit()
        return

    # ── Per-instrument guard keys ──────────────────────────────────────────────
    # Defined before the try block so both guard clauses and exception handlers
    # share the same key names without repeated f-string construction.
    recent_key   = f"cortex:context:recent:{instrument_key}"
    cooldown_key = f"cortex:context:cooldown:{instrument_key}"

    # Post-success dedup: if this instrument was successfully processed within
    # _RECENT_SUCCESS_TTL_SECS, the current message is a duplicate (e.g. scheduler
    # double-enqueue).  Commit and skip.
    if await redis.exists(recent_key):
        logger.info(
            "context_worker: skipping duplicate job for %s "
            "(successfully processed within last %ds) — committing",
            instrument_key, _RECENT_SUCCESS_TTL_SECS,
        )
        await consumer.commit()
        return

    # Transient cooldown: a 5xx / network exhaustion was recorded for this
    # instrument.  Republish with the SAME attempts and a deadline equal to the
    # remaining cooldown so redelivery lands after the endpoint has recovered —
    # this prevents a burst of duplicate messages from hammering an overloaded
    # Gemini endpoint back-to-back.
    if await redis.exists(cooldown_key):
        ttl = max(await redis.ttl(cooldown_key), 1)
        await _republish_retry(
            KafkaTopics.CONTEXT_JOBS, payload, instrument_key, attempts, ttl
        )
        await consumer.commit()
        logger.debug(
            "context_worker: %s in transient cooldown (%ds remaining) — "
            "republished with matching not_before deadline",
            instrument_key, ttl,
        )
        return

    if not await _honor_not_before(msg, KafkaTopics.CONTEXT_JOBS, instrument_key):
        await consumer.commit()
        return

    logger.info(
        "context_worker: processing context for %s "
        "(attempt=%d/%d, force=%s, source=%s)",
        instrument_key, attempts + 1, _MAX_CONTEXT_ATTEMPTS, force, source,
    )

    try:
        await _generate_instrument_context(
            instrument_key, sym, ml_snapshot, lock_key, lock_token, force=force
        )
        await consumer.commit()
        # Record success so duplicate queued jobs are skipped within the dedup window.
        await redis.set(recent_key, "1", ex=_RECENT_SUCCESS_TTL_SECS)

    except asyncio.CancelledError:
        raise

    except LLMTransientExhausted:
        # 5xx / network / timeout — Gemini is temporarily overloaded.  Set a
        # cooldown so duplicate messages for this instrument are deferred until
        # the endpoint recovers, and republish this job with a matching deadline.
        await redis.set(cooldown_key, "1", ex=_TRANSIENT_COOLDOWN_TTL_SECS)
        await _republish_retry(
            KafkaTopics.CONTEXT_JOBS, payload, instrument_key,
            attempts + 1, _TRANSIENT_COOLDOWN_TTL_SECS,
        )
        await consumer.commit()
        logger.warning(
            "context_worker: transient LLM failure for %s (attempt=%d/%d) — "
            "cooldown set, republished for retry in %ds",
            instrument_key, attempts + 1, _MAX_CONTEXT_ATTEMPTS,
            _TRANSIENT_COOLDOWN_TTL_SECS,
        )

    except GeminiRateLimitError:
        await _republish_retry(
            KafkaTopics.CONTEXT_JOBS, payload, instrument_key,
            attempts + 1, _RETRY_DELAY_SECS,
        )
        await consumer.commit()
        logger.warning(
            "context_worker: rate-limited for %s (attempt=%d/%d) — "
            "republished for retry in %ds",
            instrument_key, attempts + 1, _MAX_CONTEXT_ATTEMPTS, _RETRY_DELAY_SECS,
        )

    except GeminiQuotaExhausted:
        await consumer.commit()
        logger.error(
            "context_worker: quota exhausted for %s — committing and abandoning "
            "until quota resets at midnight PT",
            instrument_key,
        )

    except Exception as exc:
        await _republish_retry(
            KafkaTopics.CONTEXT_JOBS, payload, instrument_key,
            attempts + 1, _RETRY_DELAY_SECS,
        )
        await consumer.commit()
        logger.error(
            "context_worker: attempt %d/%d failed for %s: %s — "
            "republished for retry in %ds",
            attempts + 1, _MAX_CONTEXT_ATTEMPTS, instrument_key, exc,
            _RETRY_DELAY_SECS,
        )


# ── DLQ quota recovery ────────────────────────────────────────────────────────

# Redis key prefix for per-suggestion requeue dedup guard (SET NX, 48-hour TTL).
# Prevents the same DLQ entry from being re-added to the jobs topic more than
# once per quota cycle even if the recovery function runs multiple times.
_DLQ_REQUEUE_DEDUP_TTL_SECS = 172_800  # 48 h — spans current + next quota day

# Single-runner guard: the boot scan and the quota-reset listener can fire in
# the same session (restart near midnight PT).  Both triggers are worker-0
# gated, and this lock serializes them within the process so at most one DLQ
# drain runs at a time.
_dlq_requeue_lock = asyncio.Lock()


async def _requeue_quota_dlq_entries(redis: Any, *, trigger: str) -> int:
    """
    Drain the explanation DLQ topic and re-publish ``gemini_quota_exhausted``
    entries from the previous quota day to the explanation jobs topic.

    Called in two scenarios:
      - ``trigger="boot"``        — worker startup; only requeues entries whose
                                    ``moved_at`` timestamp falls before today's
                                    Pacific Time midnight (safe because the Gemini
                                    RPD quota boundary is midnight PT).
      - ``trigger="quota_reset"`` — fired by the pub/sub signal from
                                    GeminiRequestManager after midnight PT reset;
                                    same cutoff applies (reset fires after midnight
                                    so all previous-day entries qualify).

    Mechanics: an on-demand manual-assignment consumer in the
    ``cortex-dlq-requeue`` group drains from its committed offset to a
    SNAPSHOT of the end offset (entries republished during the drain are
    beyond the snapshot and processed on the next trigger).  Per entry:

      - non-quota reason        → commit (terminal; topic retention keeps forensics)
      - not yet eligible        → republish back to the DLQ tail + commit
      - eligible                → Redis SET NX dedup → republish original fields
                                  to the jobs topic with fresh attempts=0 → commit

    A publish failure deletes the dedup key, does NOT commit, and aborts the
    drain — the entry is redelivered on the next trigger.

    De-duplication: a per-suggestion Redis key (SET NX, 48 h TTL) prevents the
    same suggestion from being requeued twice within a single quota cycle, even
    if this function is called multiple times (e.g. boot AND pub/sub signal fire
    in the same session after a restart near midnight).

    Returns the number of entries successfully requeued.
    """
    from aiokafka.structs import TopicPartition

    pt = ZoneInfo("America/Los_Angeles")
    today_midnight_pt = datetime.now(pt).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=pt
    )
    requeued = 0

    async with _dlq_requeue_lock:
        # Manual assignment (the DLQ topic has exactly one partition) — avoids
        # group-join latency and rebalance churn for an on-demand drain.
        tp = TopicPartition(KafkaTopics.EXPLANATION_DLQ, 0)
        consumer = new_consumer(group_id=KafkaGroups.DLQ_REQUEUE)
        consumer.assign([tp])
        try:
            await consumer.start()
        except Exception as exc:
            logger.error(
                "explanation_worker: DLQ quota recovery — consumer start failed: %s",
                exc,
            )
            with contextlib.suppress(Exception):
                await consumer.stop()
            return 0

        try:
            end_snapshot = (await consumer.end_offsets([tp]))[tp]

            while (await consumer.position(tp)) < end_snapshot:
                batches = await consumer.getmany(tp, timeout_ms=2_000, max_records=50)
                records = batches.get(tp, [])
                if not records:
                    break

                for msg in records:
                    if msg.offset >= end_snapshot:
                        break

                    entry = msg.value
                    commit_offsets = {tp: msg.offset + 1}

                    if not isinstance(entry, dict):
                        await consumer.commit(commit_offsets)
                        continue

                    if entry.get("reason") != "gemini_quota_exhausted":
                        # Terminal failure (e.g. max_attempts_exceeded) — commit
                        # past it; topic retention (14d) keeps it for forensics.
                        await consumer.commit(commit_offsets)
                        continue

                    # Only requeue entries from before today's PT midnight —
                    # entries from the current quota day may still be within an
                    # active outage.  Recycle them to the DLQ tail so they stay
                    # queued for the next trigger.
                    moved_at_str = str(entry.get("moved_at", ""))
                    try:
                        moved_at = datetime.fromisoformat(moved_at_str)
                        eligible = moved_at.astimezone(pt) < today_midnight_pt
                    except (ValueError, TypeError, AttributeError):
                        logger.warning(
                            "explanation_worker: DLQ recovery — skipping entry at "
                            "offset %s with unparseable moved_at=%r",
                            msg.offset, moved_at_str,
                        )
                        await consumer.commit(commit_offsets)
                        continue

                    original_fields = entry.get("fields") or {}
                    if isinstance(original_fields, str):
                        # Tolerate cutover-era entries whose fields survived as a
                        # JSON string (the old Redis DLQ stored them serialized).
                        try:
                            original_fields = json.loads(original_fields)
                        except (json.JSONDecodeError, TypeError):
                            original_fields = {}
                    suggestion_id = str(original_fields.get("suggestion_id", "") or "")

                    if not eligible:
                        if suggestion_id:
                            await publish(
                                KafkaTopics.EXPLANATION_DLQ, entry, key=suggestion_id
                            )
                        await consumer.commit(commit_offsets)
                        continue

                    if not suggestion_id:
                        await consumer.commit(commit_offsets)
                        continue

                    # Dedup guard: skip if already requeued in this quota cycle.
                    dedup_key = f"cortex:gemini:dlq:requeued:{suggestion_id}"
                    try:
                        acquired = await redis.set(
                            dedup_key, "1", nx=True, ex=_DLQ_REQUEUE_DEDUP_TTL_SECS
                        )
                        if acquired is None:
                            logger.debug(
                                "explanation_worker: DLQ recovery — suggestion %s "
                                "already requeued this cycle (dedup key exists), "
                                "skipping.",
                                suggestion_id,
                            )
                            await consumer.commit(commit_offsets)
                            continue
                    except Exception as exc:
                        logger.warning(
                            "explanation_worker: DLQ recovery — dedup key check "
                            "failed for suggestion %s: %s — proceeding without "
                            "dedup guard.",
                            suggestion_id, exc,
                        )

                    # Re-publish to the jobs topic with a fresh retry budget.
                    try:
                        await publish(
                            KafkaTopics.EXPLANATION_JOBS,
                            original_fields,
                            key=suggestion_id,
                            headers=retry_headers(0),
                        )
                    except Exception as exc:
                        logger.error(
                            "explanation_worker: DLQ recovery — publish failed for "
                            "suggestion %s: %s — aborting drain (offset NOT "
                            "committed; resumes on next trigger)",
                            suggestion_id, exc,
                        )
                        # Undo the dedup key so the next trigger can retry this entry.
                        with contextlib.suppress(Exception):
                            await redis.delete(dedup_key)
                        return requeued

                    requeued += 1
                    gemini_dlq_requeue_total.labels(trigger=trigger).inc()
                    await consumer.commit(commit_offsets)
                    logger.info(
                        "explanation_worker: DLQ quota recovery — requeued "
                        "suggestion %s (originally DLQ'd %s, trigger=%s)",
                        suggestion_id, moved_at_str, trigger,
                    )

        except Exception as exc:
            logger.error(
                "explanation_worker: DLQ quota recovery failed: %s "
                "(partial recovery may have occurred, requeued=%d so far)",
                exc, requeued,
            )
        finally:
            with contextlib.suppress(Exception):
                await consumer.stop()

    if requeued > 0:
        logger.info(
            "explanation_worker: DLQ quota recovery complete — %d suggestion(s) "
            "requeued to jobs topic (trigger=%s)",
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
    Persistent Kafka consumer for LLM explanation jobs.

    Consumes ``cortex.explanation.jobs`` (2 partitions, keyed by suggestion_id)
    in the ``cortex-explanation-workers`` group with manual commits.  Two
    instances run in parallel (worker_id 0 and 1) spawned by ``main.py``
    lifespan — the group assigns one partition to each.

    Crash recovery is free: offsets are committed only after a terminal
    outcome, so anything in flight at a crash is redelivered from the last
    committed offset on restart (the old PEL-drain, without the machinery).

    Worker 0 additionally owns the single-runner side tasks: boot-time DLQ
    quota recovery, the midnight-PT quota-reset listener, and the queue-depth
    gauge ticker.
    """
    from app.core.redis import get_redis as _get_redis

    _redis = _get_redis()

    # Boot-time DLQ recovery (worker_id=0 only — prevents duplicate requeues when
    # both workers start simultaneously; worker 1 relies on worker 0's listener
    # for realtime recovery).
    if worker_id == 0:
        await _requeue_quota_dlq_entries(_redis, trigger="boot")

    background_tasks: list[asyncio.Task] = []
    if worker_id == 0:
        # Realtime DLQ recovery: listen for midnight PT quota resets from the
        # manager.  Only worker 0 subscribes — one listener per process.
        background_tasks.append(asyncio.create_task(
            _quota_reset_listener(_redis),
            name="gemini_quota_reset_dlq_listener",
        ))
        # Consumer-lag depth gauge — one ticker per topic per process.
        background_tasks.append(asyncio.create_task(
            _queue_depth_ticker(
                KafkaTopics.EXPLANATION_JOBS,
                KafkaGroups.EXPLANATION_WORKERS,
                "explanation",
            ),
            name="explanation_queue_depth_ticker",
        ))

    logger.info("explanation_worker[%d]: ready", worker_id)

    try:
        while True:
            # max_poll_records=1: one message at a time keeps Gemini-latency
            # work (≤120s LLM timeout) plus a bounded not_before sleep (≤60s)
            # far below max_poll_interval_ms (300s).
            consumer = new_consumer(
                KafkaTopics.EXPLANATION_JOBS,
                group_id=KafkaGroups.EXPLANATION_WORKERS,
                max_poll_records=1,
            )
            try:
                await consumer.start()
                async for msg in consumer:
                    await _process_explanation_message(
                        _redis, consumer, msg, worker_id
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "explanation_worker[%d]: consumer error: %s — reconnecting in %ds",
                    worker_id, exc, _RECONNECT_DELAY_SECS,
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECS)
                _redis = _get_redis()
            finally:
                with contextlib.suppress(Exception):
                    await consumer.stop()

    except asyncio.CancelledError:
        logger.info("explanation_worker[%d]: cancelled — shutting down", worker_id)
        raise
    finally:
        for task in background_tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


async def context_worker() -> None:
    """
    Persistent Kafka consumer for instrument context generation jobs.

    Consumes ``cortex.context.jobs`` in the ``cortex-context-workers`` group
    with manual commits.  A single instance is sufficient because context jobs
    are low-frequency and already protected by the distributed lock in
    ai_stream.py Stage 3.

    Crash recovery: offsets are committed only after a terminal outcome, so
    in-flight jobs are redelivered from the committed offset on restart.  The
    DB idempotency check and the recent-success key make redelivery cheap.

    Retry policy lives in _process_context_message (attempts header, republish
    with not_before deadlines, abandon after _MAX_CONTEXT_ATTEMPTS, no DLQ).
    """
    from app.core.redis import get_redis as _get_redis

    _redis = _get_redis()

    depth_ticker_task = asyncio.create_task(
        _queue_depth_ticker(
            KafkaTopics.CONTEXT_JOBS,
            KafkaGroups.CONTEXT_WORKERS,
            "context",
        ),
        name="context_queue_depth_ticker",
    )

    logger.info("context_worker: ready")

    try:
        while True:
            consumer = new_consumer(
                KafkaTopics.CONTEXT_JOBS,
                group_id=KafkaGroups.CONTEXT_WORKERS,
                max_poll_records=1,
            )
            try:
                await consumer.start()
                async for msg in consumer:
                    await _process_context_message(_redis, consumer, msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "context_worker: consumer error: %s — reconnecting in %ds",
                    exc, _RECONNECT_DELAY_SECS,
                    exc_info=True,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECS)
                _redis = _get_redis()
            finally:
                with contextlib.suppress(Exception):
                    await consumer.stop()

    except asyncio.CancelledError:
        logger.info("context_worker: cancelled — shutting down")
        raise
    finally:
        depth_ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await depth_ticker_task
