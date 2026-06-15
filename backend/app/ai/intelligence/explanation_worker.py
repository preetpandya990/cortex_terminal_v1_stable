"""
LLM Explanation Worker
======================
Async background task that generates plain-English explanations for two use cases:

  1. Trade suggestion explanations (existing)
       Triggered by LLM_EXPLANATION_PENDING.  Generates a signal-specific
       explanation for a committed TradeSuggestion and writes it back to the
       trade_suggestions table.

  2. Instrument market context (new)
       Triggered by LLM_CONTEXT_PENDING.  Generates an instrument-level market
       context summary for Watchlist items with no active trade suggestion.
       Written to ai_instrument_context (upsert) with a 2-hour TTL.

Pipeline — suggestion explanation
----------------------------------
  1. Receive {suggestion_id, id} from cortex:llm:explanation:pending
  2. Load TradeSuggestion from DB
  3. RAG retrieve — top-k news chunks for the symbol in the last 24 hours
  4. Build a structured prompt from signal data + retrieved context
  5. LLM structured generation → ExplanationOutput (Gemini native structured
     output via response_schema — single call, no streaming)
  6. Apply output guardrails (disclaimer injection, price-prediction filter,
     citation check)
  7. Write llm_summary + llm_explanation to trade_suggestions
  8. Append one row to ai_llm_audit_log
  9. Publish cortex:llm:explanation:ready:{suggestion_id} for the SSE stream

Pipeline — instrument market context
--------------------------------------
  1. Receive {instrument_key, symbol, prediction_data} from cortex:llm:context:pending
  2. RAG retrieve — recent news for the symbol
  3. Build a market-context prompt (news + ML signal snapshot)
  4. LLM structured generation → ExplanationOutput (same schema, different prompt)
  5. Apply same guardrails
  6. Upsert into ai_instrument_context (expires_at = now + 2h)
  7. Append one row to ai_llm_audit_log
  8. Publish cortex:llm:context:ready:{instrument_key} for the SSE stream

Design invariants
-----------------
  - Failed generations never block indefinitely.  MAX_ATTEMPTS applies per
    suggestion/instrument; after exhaustion the frontend shows no explanation.
  - The worker is a single asyncio task (non-concurrent).  At current volume
    this is sufficient; introduce a semaphore-bounded pool when volume scales.
  - Every LLM inference — success or failure — writes one ai_llm_audit_log row.
    This is a non-negotiable governance requirement (SR 11-7).
  - The worker reconnects automatically on Redis errors.

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
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AILLMAuditLog
from app.ai.intelligence.llm_client import Priority, get_intelligence_client
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

    Used by ``_apply_guardrails`` to strip violating sentences from the final
    generated output before it is persisted and shown to the client.
    """
    removed = 0
    clean_lines: list[str] = []
    for line in text.split("\n"):
        if not line.strip():
            clean_lines.append(line)  # preserve blank lines (section breaks)
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

    # 1. Price-prediction filter — remove violating sentences while preserving the
    #    markdown section structure.
    full_explanation, n_removed = _strip_price_predictions(output.full_explanation)
    events.extend(["price_prediction_filter"] * n_removed)

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

def _fmt_pct(value: Any, default: str = "N/A") -> str:
    """Format a 0–1 float as a whole-number percentage; tolerant of None/non-numeric."""
    try:
        return f"{float(value) * 100:.0f}%"
    except (TypeError, ValueError):
        return default


def _format_probabilities(probs: dict | None) -> str | None:
    """Render a buy/sell/hold distribution as 'BUY 71% · SELL 8% · HOLD 21%'."""
    if not probs:
        return None
    parts = [
        f"{label} {_fmt_pct(probs[key])}"
        for key, label in (("buy", "BUY"), ("sell", "SELL"), ("hold", "HOLD"))
        if probs.get(key) is not None
    ]
    return " · ".join(parts) if parts else None


def _render_model_breakdown(models: dict | None) -> list[str]:
    """
    Render the per-model (XGBoost / GRU) breakdown shared by both prompt builders.

    Each model dict follows the serialize_prediction_card / gather_ml_signals shape:
      {direction, confidence, conviction_scale, threshold, probabilities, weight, version}
    Returns an empty list when no per-model data is available.
    """
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


# Canonical technical-scanner keys surfaced in the prompt (skips identifiers).
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
    """Render known technical-scanner readings, skipping identifiers and empties."""
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


def _build_explanation_prompt(
    suggestion: TradeSuggestion,
    context: str,
) -> str:
    """
    Render the explanation prompt from suggestion signal data + RAG context.

    Surfaces the full multi-agent evidence the consensus was built from — the ML
    ensemble's per-model (XGBoost / GRU) breakdown, the technical scanner readings,
    and the contributing news events — so the LLM can explain what the models saw
    and how they processed it, not merely restate the news.

    All values are taken from the committed DB row — no inference, no guessing.
    """
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

    # ── ML ensemble detail ────────────────────────────────────────────────────
    if ml.get("available"):
        lines.append("")
        lines.append("## ML Ensemble Output")
        # Ensemble direction lives in the prediction sub-dict; the top-level dict
        # has no 'action' key, so fall back to the score sign rather than N/A.
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

    # ── Technical scanner detail ──────────────────────────────────────────────
    scanner_lines = _render_scanner(scanner)
    if scanner_lines:
        lines.append("")
        lines.append("## Technical Scanner Readings")
        lines.extend(scanner_lines)

    # ── News / event signal detail ────────────────────────────────────────────
    # ai_signal is the Gemini news forecaster's output (Phase 2): its directional
    # lean + rationale over the same indicators the ML saw, plus the contributing
    # news events.  Surfacing it keeps the explanation consistent with the
    # forecaster that actually fed the consensus.
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

    # ── Retrieved news context (RAG) ──────────────────────────────────────────
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
    """
    Render the market-context prompt for an instrument with no active suggestion.

    Surfaces the full ML ensemble snapshot (ensemble + per-model XGBoost / GRU
    breakdown, probabilities, conviction, volatility) so the LLM can describe what
    the model is currently seeing — giving Watchlist users the same signal-level
    insight they'd get from an active suggestion, without fabricating a signal
    that doesn't exist.  ``ml_snapshot`` is the serialize_prediction_card payload.
    """
    lines = [
        "## Instrument Overview",
        f"Instrument Key: {instrument_key}",
        f"Symbol:         {symbol}",
    ]

    # ML snapshot — included only when the SSE stream has a valid prediction
    # loaded (ml_snapshot may be None on first poll).
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

    ``reference_table`` must be provided explicitly by every caller to enforce
    correct audit-trail attribution:
      - "trade_suggestions"     → suggestion explanation
      - "ai_instrument_context" → instrument market context
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

    # ── LLM call (Gemini native structured output, single call) ──────────────
    t0 = time.monotonic()
    error_message: str | None = None
    raw_output: ExplanationOutput | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_provider, _, model_id = client.model_id.partition("/")

    try:
        raw_output, usage_info = await client.generate_structured_with_usage(
            prompt=prompt,
            response_model=ExplanationOutput,
            system=_EXPLANATION_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=1400,
            priority=Priority.HIGH,
        )
        input_tokens  = usage_info.get("input_tokens")
        output_tokens = usage_info.get("output_tokens")
        model_provider = usage_info.get("provider", model_provider)
        model_id       = usage_info.get("model_id", model_id)
    except Exception as exc:
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


# ── Instrument context generation ────────────────────────────────────────────

async def _generate_instrument_context(
    instrument_key: str,
    symbol: str | None,
    ml_snapshot: dict | None,
    db: AsyncSession,
) -> None:
    """
    Generate a market context summary for an instrument with no active signal.

    Used for Watchlist items — gives the user a news-grounded, ML-annotated
    market overview instead of a blank explanation panel.

    On success:
      - Upserts ai_instrument_context (expires_at = now + 2 h)
      - Appends one ai_llm_audit_log row
      - Publishes cortex:llm:context:ready:{instrument_key}

    Raises RuntimeError on LLM failure so the worker can track retry count.
    """
    from app.ai.fusion.models import AIInstrumentContext
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    client = get_intelligence_client()
    invocation_id = uuid4()

    # Derive a usable symbol for RAG when only the instrument key is available.
    eff_symbol: str = symbol or (
        instrument_key.split("|")[-1] if "|" in instrument_key else instrument_key
    )

    # ── RAG retrieval ─────────────────────────────────────────────────────────
    query = f"{eff_symbol} market analysis news"
    try:
        chunks = await retrieve(db=db, query=query, symbol=eff_symbol)
    except Exception as exc:
        logger.warning(
            "explanation_worker: RAG retrieval failed for instrument context %s "
            "(continuing with no context): %s",
            instrument_key, exc,
        )
        chunks = []

    context    = format_context(chunks)
    source_refs = build_retrieval_source_refs(chunks)
    prompt     = _build_context_prompt(instrument_key, eff_symbol, ml_snapshot, context)
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()

    # ── LLM call (Gemini native structured output, single call) ──────────────
    t0 = time.monotonic()
    error_message:  str | None               = None
    raw_output:     ExplanationOutput | None  = None
    input_tokens:   int | None                = None
    output_tokens:  int | None                = None
    model_provider, _, model_id = client.model_id.partition("/")

    try:
        raw_output, usage_info = await client.generate_structured_with_usage(
            prompt=prompt,
            response_model=ExplanationOutput,
            system=_CONTEXT_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=1400,
            priority=Priority.LOW,
        )
        input_tokens  = usage_info.get("input_tokens")
        output_tokens = usage_info.get("output_tokens")
        model_provider = usage_info.get("provider", model_provider)
        model_id       = usage_info.get("model_id", model_id)
    except Exception as exc:
        error_message = f"{type(exc).__name__}: {exc}"
        logger.error(
            "explanation_worker: LLM context generation failed for %s: %s",
            instrument_key, exc,
        )

    latency_ms = int((time.monotonic() - t0) * 1000)

    if error_message is None:
        llm_explanations_total.labels(status="success", provider=model_provider).inc()
        llm_explanation_duration_seconds.labels(provider=model_provider).observe(latency_ms / 1000.0)
    else:
        llm_explanations_total.labels(status="failure", provider=model_provider).inc()

    # ── Guardrails ────────────────────────────────────────────────────────────
    guardrail_events: list[str]          = []
    final_output:     ExplanationOutput | None = None

    if raw_output is not None:
        final_output, guardrail_events = _apply_guardrails(
            raw_output, has_context=bool(chunks)
        )
        for event in guardrail_events:
            llm_guardrail_events_total.labels(guardrail=event).inc()

    # ── Persist to ai_instrument_context (upsert) ─────────────────────────────
    sources_payload: list[dict] = []
    if final_output is not None:
        now_utc    = datetime.now(timezone.utc)
        expires_at = now_utc + timedelta(hours=2)
        model_str  = f"{model_provider}/{model_id}"

        sources_payload = [
            {
                "source_name": chunk.source_name,
                "as_of":       chunk.as_of_timestamp.isoformat(),
                "source_url":  chunk.source_url,
            }
            for chunk in chunks
        ]

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

    # ── Audit log ─────────────────────────────────────────────────────────────
    output_preview: str | None = None
    if final_output is not None:
        output_preview = final_output.summary[:500]

    await _write_audit_entry(
        db,
        invocation_id=invocation_id,
        invocation_type="instrument_context",
        reference_table="ai_instrument_context",
        reference_id=None,  # upsert — no stable row PK to reference
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
            ready_channel = RedisChannels.LLM_CONTEXT_READY.format(
                instrument_key=instrument_key
            )
            payload = json.dumps({
                "instrument_key":  instrument_key,
                "context_summary": final_output.summary,
                "context_full":    final_output.full_explanation,
                "model":           f"{model_provider}/{model_id}",
                "generated_at":    datetime.now(timezone.utc).isoformat(),
                "sources":         sources_payload,
            }, default=str)
            await get_redis().publish(ready_channel, payload)
        except Exception as exc:
            logger.warning(
                "explanation_worker: failed to publish context ready for %s "
                "(non-fatal): %s",
                instrument_key, exc,
            )

    if error_message is not None:
        raise RuntimeError(error_message)


# ── Worker task ───────────────────────────────────────────────────────────────

async def explanation_worker() -> None:
    """
    Persistent application-level background task.

    Subscribes to:
      - RedisChannels.LLM_EXPLANATION_PENDING  → _generate_explanation
      - RedisChannels.LLM_CONTEXT_PENDING      → _generate_instrument_context

    Processes requests sequentially.  Registered in main.py lifespan —
    one instance for the lifetime of the process.

    Retry policy: each key (suggestion_id or instrument_key) is attempted at
    most MAX_ATTEMPTS times.  After exhaustion the failure is recorded in
    ai_llm_audit_log and the frontend shows no explanation rather than a
    broken skeleton.

    Reconnect policy: on Redis errors, waits _RECONNECT_DELAY_SECS then
    re-subscribes (identical to cai_redis_listener and suggestions_redis_listener).
    """
    from app.core.database import AsyncSessionLocal
    from app.core.redis import get_redis as _get_redis

    # Unified retry counter keyed by:
    #   suggestion_id (str UUID) for explanation requests
    #   f"ctx:{instrument_key}"  for instrument context requests
    attempt_counts: dict[str, int] = {}

    while True:
        redis = _get_redis()
        pubsub = redis.pubsub()
        try:
            await pubsub.subscribe(
                RedisChannels.LLM_EXPLANATION_PENDING,
                RedisChannels.LLM_CONTEXT_PENDING,
            )
            logger.info(
                "explanation_worker: subscribed to %s and %s",
                RedisChannels.LLM_EXPLANATION_PENDING,
                RedisChannels.LLM_CONTEXT_PENDING,
            )

            while True:
                raw = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.1
                )
                if raw is None:
                    await asyncio.sleep(0)
                    continue

                channel: str = raw.get("channel", "")

                # ── Route: suggestion explanation ──────────────────────────
                if channel == RedisChannels.LLM_EXPLANATION_PENDING:
                    try:
                        data = json.loads(raw["data"])
                        suggestion_id:    str = data["suggestion_id"]
                        suggestion_db_id: int = data["id"]
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        logger.warning(
                            "explanation_worker: malformed message on explanation "
                            "pending channel: %s",
                            exc,
                        )
                        continue

                    attempts = attempt_counts.get(suggestion_id, 0)
                    if attempts >= MAX_ATTEMPTS:
                        logger.error(
                            "explanation_worker: suggestion %s exhausted %d/%d "
                            "attempts — abandoning (llm_summary will remain NULL)",
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
                            await _generate_explanation(
                                suggestion_id, suggestion_db_id, db
                            )
                        attempt_counts.pop(suggestion_id, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "explanation_worker: attempt %d/%d failed for "
                            "suggestion %s: %s",
                            attempts + 1, MAX_ATTEMPTS, suggestion_id, exc,
                        )

                # ── Route: instrument market context ───────────────────────
                elif channel == RedisChannels.LLM_CONTEXT_PENDING:
                    try:
                        data = json.loads(raw["data"])
                        instrument_key: str       = data["instrument_key"]
                        sym: str | None           = data.get("symbol")
                        ml_snapshot: dict | None  = data.get("prediction_data")
                    except (json.JSONDecodeError, KeyError, TypeError) as exc:
                        logger.warning(
                            "explanation_worker: malformed message on context "
                            "pending channel: %s",
                            exc,
                        )
                        continue

                    retry_key = f"ctx:{instrument_key}"
                    attempts  = attempt_counts.get(retry_key, 0)
                    if attempts >= MAX_ATTEMPTS:
                        logger.error(
                            "explanation_worker: instrument context for %s "
                            "exhausted %d/%d attempts — abandoning",
                            instrument_key, attempts, MAX_ATTEMPTS,
                        )
                        attempt_counts.pop(retry_key, None)
                        continue

                    attempt_counts[retry_key] = attempts + 1
                    logger.info(
                        "explanation_worker: generating context for %s "
                        "(attempt %d/%d)",
                        instrument_key, attempts + 1, MAX_ATTEMPTS,
                    )

                    try:
                        async with AsyncSessionLocal() as db:
                            await _generate_instrument_context(
                                instrument_key, sym, ml_snapshot, db
                            )
                        attempt_counts.pop(retry_key, None)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.error(
                            "explanation_worker: attempt %d/%d failed for "
                            "instrument context %s: %s",
                            attempts + 1, MAX_ATTEMPTS, instrument_key, exc,
                        )

                else:
                    # Unexpected channel — ignore silently
                    logger.debug(
                        "explanation_worker: message on unexpected channel %s — skipping",
                        channel,
                    )

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
                await pubsub.unsubscribe(
                    RedisChannels.LLM_EXPLANATION_PENDING,
                    RedisChannels.LLM_CONTEXT_PENDING,
                )
                await pubsub.aclose()
            except Exception:
                pass
