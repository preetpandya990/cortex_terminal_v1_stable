"""
Gemini News Forecaster
======================
The AI/news forecaster — Cortex's *second* forecaster, alongside the ML ensemble.

It reasons over the SAME labeled technical indicators the ML model sees (rsi_14,
macd_*, ema_20/50, atr_14, …, captured verbatim from the ML feature pipeline)
PLUS the recent RSS/news events the ML cannot read, and produces an independent
directional view: ``direction`` + calibrated ``confidence`` + a short rationale.

Because both forecasters share identical indicators, the news is the *only*
differentiator — preserving the decorrelation that makes a two-forecaster
consensus worth more than either alone.

This module holds the pure, side-effect-free pieces:
  - ``NewsForecastOutput``  — the Gemini structured-output schema.
  - prompt construction from indicators + events.
  - ``score_from_direction`` — maps (direction, confidence) → the −100..100 score
    convention the consensus/fusion layer consumes.
  - ``CircuitBreaker`` — trips after consecutive Gemini failures so the consensus
    path falls straight through to its deterministic fallback during an outage
    instead of paying the call timeout on every suggestion.
  - ``generate_news_forecast`` — the timeout-bounded Gemini call.

Orchestration (cache, fallback, audit, metrics) lives in
``SignalAssembler.gather_news_forecast``, which owns the DB/Redis handles.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.ai.intelligence.request_manager import Priority

logger = logging.getLogger(__name__)


# ── Structured output schema ───────────────────────────────────────────────────

class NewsForecastOutput(BaseModel):
    """Gemini's structured news-conditioned forecast for one symbol.

    Field order is deliberate: the model writes ``rationale`` first as a brief
    scratchpad (thinking is disabled for latency), then commits to ``direction``
    conditioned on its own reasoning, then states ``confidence`` in that call.
    """

    rationale: str = Field(
        description=(
            "ONE or TWO short sentences (≤45 words) grounding the call in the "
            "specific indicator values and news provided. No advice, no price "
            "targets, no fabricated figures."
        ),
    )
    direction: Literal["BUY", "SELL", "HOLD"] = Field(
        description=(
            "Net directional lean once the technical indicators and the news are "
            "combined. HOLD when they conflict or signal is weak."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Calibrated confidence in `direction`, 0.0–1.0. Be conservative: "
            "reserve >0.8 for strong agreement between indicators and corroborating "
            "news; use ≤0.5 when evidence is thin, mixed, or news is absent."
        ),
    )


# ── System prompt ──────────────────────────────────────────────────────────────

_FORECAST_SYSTEM_PROMPT = """\
You are the news-aware forecasting module of the Cortex trading platform — the
second forecaster alongside a separate ML ensemble. You are NOT a financial
advisor and must not give investment advice.

You are given the SAME technical indicators the ML model uses, plus recent news
events the ML cannot read. Your job: weigh the indicators and the news together
and output an INDEPENDENT directional view — direction (BUY/SELL/HOLD), a
calibrated confidence (0–1), and a one/two-sentence rationale.

Your edge over the ML is the NEWS: when credible, recent, material news
corroborates or contradicts the technical picture, let it move your direction
and confidence accordingly. With no relevant news, lean on the indicators alone
and keep confidence modest.

Rules:
- GROUND every claim in the specific numbers/news provided. Never invent figures,
  prices, events, or sources.
- No price targets, no guarantees, no advice ("buy now", "you should…").
- HOLD when indicators and news conflict or the signal is weak.
- Confidence is calibrated, not promotional: >0.8 only on strong, corroborated
  agreement; ≤0.5 when evidence is thin, mixed, or news is absent.\
"""


# ── Indicator rendering ─────────────────────────────────────────────────────────
# Canonical, human-readable subset of the ML feature set surfaced to the model.
# Keys match app/ml/config.py feature names; missing keys are skipped silently.
_FORECAST_INDICATORS: tuple[tuple[str, str], ...] = (
    ("close",          "Last Close"),
    ("rsi_14",         "RSI-14"),
    ("rsi_21",         "RSI-21"),
    ("macd_line",      "MACD line"),
    ("macd_signal",    "MACD signal"),
    ("macd_histogram", "MACD histogram"),
    ("ema_20",         "EMA-20"),
    ("ema_50",         "EMA-50"),
    ("dema_20",        "DEMA-20"),
    ("atr_14",         "ATR-14"),
    ("adx_14",         "ADX-14"),
    ("obv",            "OBV"),
    ("volume_ratio",   "Volume ratio"),
    ("bb_upper",       "Bollinger upper"),
    ("bb_lower",       "Bollinger lower"),
)


def _render_indicators(indicators: dict[str, Any] | None) -> list[str]:
    """Render the canonical indicator subset present in the snapshot."""
    if not indicators:
        return []
    lines: list[str] = []
    for key, label in _FORECAST_INDICATORS:
        val = indicators.get(key)
        if val is None:
            continue
        try:
            lines.append(f"  {label}: {float(val):.4g}")
        except (TypeError, ValueError):
            continue
    return lines


def _render_events(events: list[dict] | None, limit: int = 6) -> list[str]:
    """Render recent news/events with sentiment/impact/source where available."""
    if not events:
        return []
    lines: list[str] = []
    for ev in events[:limit]:
        title = ev.get("article_title") or ev.get("type") or ev.get("event_type") or "event"
        bits: list[str] = []
        impact = ev.get("impact")
        if impact is not None:
            try:
                bits.append(f"impact {float(impact):+.1f}")
            except (TypeError, ValueError):
                pass
        if ev.get("sentiment") or ev.get("sentiment_label"):
            bits.append(str(ev.get("sentiment") or ev.get("sentiment_label")))
        if ev.get("source_name"):
            bits.append(str(ev["source_name"]))
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"  - {title}{suffix}")
    return lines


def build_forecast_prompt(
    symbol: str,
    indicators: dict[str, Any] | None,
    events: list[dict] | None,
    *,
    regime: str | None = None,
) -> str:
    """Render the forecaster user prompt from indicators + news events."""
    lines = [f"Symbol: {symbol}"]
    if regime:
        lines.append(f"Market regime: {regime}")

    ind_lines = _render_indicators(indicators)
    lines.append("")
    lines.append("Technical indicators (identical to the ML model's inputs):")
    lines.extend(ind_lines or ["  (none available)"])

    ev_lines = _render_events(events)
    lines.append("")
    if ev_lines:
        lines.append("Recent news / events:")
        lines.extend(ev_lines)
    else:
        lines.append(
            "Recent news / events: none in the lookback window — base the call on "
            "the indicators and keep confidence modest."
        )
    return "\n".join(lines)


# ── Score mapping ───────────────────────────────────────────────────────────────

_DIRECTION_SIGN = {"BUY": 1.0, "SELL": -1.0, "HOLD": 0.0}


def score_from_direction(direction: str, confidence: float) -> float:
    """Map (direction, confidence) → the −100..100 score the fusion layer expects.

    BUY → +confidence·100, SELL → −confidence·100, HOLD → 0.  This mirrors the
    {BUY:+100, SELL:−100, HOLD:0} convention used by the ML/event signals, scaled
    by the model's calibrated confidence.
    """
    sign = _DIRECTION_SIGN.get(direction.upper(), 0.0)
    try:
        conf = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        conf = 0.0
    return sign * conf * 100.0


# ── Circuit breaker ─────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Minimal consecutive-failure breaker for the forecaster's Gemini calls.

    A Gemini outage is provider-wide, so one global breaker is correct.  After
    ``threshold`` consecutive failures it opens for ``cooldown`` seconds, during
    which ``allow()`` returns False and callers go straight to the deterministic
    fallback (no wasted call/timeout).  After cooldown it half-opens: the next
    call is allowed; success closes it, failure re-opens it.
    """

    def __init__(self, threshold: int, cooldown: float) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._failures = 0
        self._open_until = 0.0

    def allow(self) -> bool:
        """True if a live call should be attempted now."""
        if self._open_until and time.monotonic() < self._open_until:
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._open_until = 0.0

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            self._open_until = time.monotonic() + self._cooldown
            logger.warning(
                "news_forecaster: circuit breaker OPEN after %d consecutive "
                "failures — using NLP fallback for %.0fs",
                self._failures, self._cooldown,
            )

    @property
    def is_open(self) -> bool:
        return bool(self._open_until and time.monotonic() < self._open_until)


# ── Timed Gemini call ───────────────────────────────────────────────────────────

# ── Batch forecast schemas ──────────────────────────────────────────────────────
# Used by the forecast_batch_worker to fire one Gemini call per group of symbols
# instead of one call per symbol on the signal-assembly hot path.
#
# Reliability constraints (from structured-output bug research):
#   - Batch size ≤ 5 for complex schemas (15 indicators + 6 events per symbol).
#     Community-validated safe ceiling for multi-item structured output.
#   - Each SymbolForecast is validated independently — invalid items are silently
#     dropped; callers receive the NLP fallback for that symbol.
#   - symbol echo-back required for item-mixing detection (confirmed Gemini bug).
#   - max_output_tokens set generously (1 000) to prevent silent None returns.
#   - Never use the Gemini Async Batch API — confirmed ~70% hallucination rate.

class SymbolForecast(BaseModel):
    """Single symbol result within a batch forecast response."""

    symbol: str = Field(
        description="Exact symbol string from the request — echo back verbatim.",
    )
    rationale: str = Field(
        description=(
            "ONE or TWO short sentences (≤45 words) grounding the call in the "
            "specific indicator values and news provided. No advice, no price "
            "targets, no fabricated figures."
        ),
    )
    direction: Literal["BUY", "SELL", "HOLD"] = Field(
        description=(
            "Net directional lean once the technical indicators and the news are "
            "combined. HOLD when they conflict or signal is weak."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Calibrated confidence in `direction`, 0.0–1.0. Be conservative: "
            "reserve >0.8 for strong agreement; use ≤0.5 when evidence is thin."
        ),
    )


class NewsForecastBatchOutput(BaseModel):
    """Batch forecast response — one SymbolForecast entry per requested symbol."""

    forecasts: list[SymbolForecast] = Field(
        description=(
            "One forecast per symbol, in the same order as the input. "
            "Echo the symbol name exactly as given. If evidence is thin for a "
            "symbol, use HOLD with confidence ≤ 0.4 rather than omitting the entry."
        ),
    )


# System prompt for batch calls (multi-symbol context).
_BATCH_FORECAST_SYSTEM_PROMPT = """\
You are the news-aware forecasting module of the Cortex trading platform.
You are forecasting for MULTIPLE symbols in a single call.

For EACH symbol provided:
- Reason over its technical indicators and news events INDEPENDENTLY.
- Output direction (BUY/SELL/HOLD), calibrated confidence (0–1), and a
  1–2 sentence rationale (≤45 words) grounded in the provided data.
- Echo the symbol name EXACTLY as given in the request (verbatim).
- If evidence is thin, use HOLD with confidence ≤ 0.4 rather than omitting.

Rules:
- Never fabricate figures, prices, events, or sources.
- No price targets, no investment advice ("buy now", "you should…").
- Confidence >0.8 only on strong, corroborated agreement.
- Treat each symbol INDEPENDENTLY — do not cross-contaminate signals between symbols.\
"""

# Fields extracted from raw event dicts for the batch queue payload.
# Limits payload size while retaining all data needed by _render_events().
_BATCH_EVENT_FIELDS: frozenset[str] = frozenset({
    "id", "article_title", "type", "event_type",
    "impact", "sentiment", "sentiment_label", "source_name",
})


def build_batch_forecast_prompt(
    payloads: list[dict[str, Any]],
) -> tuple[str, list[str]]:
    """Build a multi-symbol forecast prompt from a list of queue payloads.

    Returns ``(prompt_text, valid_symbols)`` where ``valid_symbols`` mirrors
    the order of symbols written into the prompt.  Payloads without a
    ``symbol`` field are skipped silently.
    """
    sections: list[str] = []
    valid_symbols: list[str] = []

    for payload in payloads:
        symbol = payload.get("symbol")
        if not symbol:
            continue
        indicators = payload.get("indicators") or {}
        events     = payload.get("events") or []
        regime     = payload.get("regime")

        per_symbol = build_forecast_prompt(symbol, indicators, events, regime=regime)
        sections.append(f"## {symbol}\n\n{per_symbol}")
        valid_symbols.append(symbol)

    if not sections:
        return "", []

    header = (
        f"Forecast for {len(valid_symbols)} symbol(s): "
        f"{', '.join(valid_symbols)}\n\n"
        "Provide one SymbolForecast entry per symbol.\n\n"
        "---\n\n"
    )
    return header + "\n\n---\n\n".join(sections), valid_symbols


async def generate_news_forecast(
    client: Any,
    *,
    symbol: str,
    indicators: dict[str, Any] | None,
    events: list[dict] | None,
    regime: str | None,
    timeout: float,
    max_tokens: int,
    priority: Priority = Priority.LOW,
) -> tuple[NewsForecastOutput, dict[str, Any]]:
    """Run the timeout-bounded Gemini structured forecast.

    Returns ``(output, usage)``.  Raises ``asyncio.TimeoutError`` on timeout or
    propagates the client's ``LLMFallbackExhausted`` — the caller maps either to
    the deterministic NLP fallback.
    """
    prompt = build_forecast_prompt(symbol, indicators, events, regime=regime)
    return await asyncio.wait_for(
        client.generate_structured_with_usage(
            prompt=prompt,
            response_model=NewsForecastOutput,
            system=_FORECAST_SYSTEM_PROMPT,
            temperature=0.2,
            max_tokens=max_tokens,
            priority=priority,
        ),
        timeout=timeout,
    )
