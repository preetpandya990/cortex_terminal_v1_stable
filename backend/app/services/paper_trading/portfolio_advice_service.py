"""
Paper Trading — Portfolio AI Advice Service (B5)
=================================================
On-demand, cached, quota-safe LLM advice for the Portfolio Insight & Advise
layer.  Builds a fully-grounded prompt from the B4 stats panel, calls Gemini's
structured-output mode once (HIGH priority — user-initiated, latency-sensitive),
scrubs the result through the same guardrails as the explanation pipeline, and
caches it under a materiality hash so an unchanged portfolio never re-spends
quota.

Reliability contract
--------------------
This path **never 500s**.  On ``GeminiQuotaExhausted`` / ``GeminiRateLimitError``
/ ``GeminiBudgetThrottled`` (or any unexpected error) it returns the last-cached
advice flagged ``stale=True``; with no cache it returns a graceful
"temporarily unavailable" advice, also ``stale=True``.  The stats panel (B4) is
independent and always renders.

Grounding & compliance
----------------------
Every figure the model may cite is injected into the prompt, so the reused
``_strip_ungrounded_numbers`` guardrail removes any invented number, and
``_strip_guarantee_language`` removes certainty/guarantee claims.  A fixed
regulatory disclaimer (never LLM-generated) is attached server-side.  The advice
is deliberately actionable (the plan's "recommends — the user decides"); the
disclaimer + guardrails + the frontend's verbatim risk warning are the mitigation.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.intelligence.llm_client import LLMFallbackExhausted, get_intelligence_client
from app.ai.intelligence.request_manager import (
    GeminiQuotaExhausted as _RMQuotaExhausted,
    GeminiRateLimitError,
    Priority,
)
from app.core.config import get_settings
from app.schemas.portfolio_insight import (
    PerPositionNote,
    PortfolioAdvice,
    PortfolioAdviceGeneration,
    PortfolioInsightStats,
)
from app.services.paper_trading.portfolio_insight_service import (
    compute_portfolio_insight_stats,
)

logger = logging.getLogger(__name__)

_ADVICE_KEY_PREFIX = "cai:paper:insight:advice:"
_ADVICE_TEMPERATURE = 0.2
_ADVICE_MAX_TOKENS = 1200

# Degradation exceptions that must serve cached/graceful output, never 500.
# llm_client.GeminiQuotaExhausted / LLMTransientExhausted ⊂ LLMFallbackExhausted;
# request_manager.GeminiBudgetThrottled ⊂ GeminiRateLimitError.
_DEGRADE_EXCEPTIONS = (LLMFallbackExhausted, GeminiRateLimitError, _RMQuotaExhausted)

_SYSTEM_PROMPT = (
    "You are a portfolio risk analyst for a paper-trading (simulated) learning "
    "platform. You are given a factual risk snapshot of the user's open paper "
    "portfolio. Write a concise, specific, actionable assessment.\n"
    "Strict rules:\n"
    "1. Use ONLY the figures provided — never invent a number, price, or percentage.\n"
    "2. Frame actions as considerations the user weighs and decides on "
    "(e.g. 'consider trimming…', 'you may want to…'), not as commands.\n"
    "3. Never promise, guarantee, or predict returns or price levels. No certainty language.\n"
    "4. Be concrete: name the specific holdings, sectors, and figures that drive each risk.\n"
    "5. Keep the assessment to 2–4 sentences; risks and considerations to short bullet lines."
)

_ASSESSMENT_FALLBACK = "See the portfolio risk metrics above for the current risk picture."
_UNAVAILABLE_ASSESSMENT = (
    "AI portfolio advice is temporarily unavailable (AI capacity limit). "
    "The risk metrics above remain current."
)


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────────────

async def generate_portfolio_advice(
    session: AsyncSession,
    portfolio,
    redis: Any,
) -> PortfolioAdvice:
    """
    Return AI advice for ``portfolio``, served from cache when the portfolio is
    materially unchanged, regenerated otherwise, and degraded gracefully (never
    raised) on quota/rate-limit exhaustion.
    """
    now = datetime.now(timezone.utc)
    stats = await compute_portfolio_insight_stats(session, portfolio)

    if stats.open_position_count == 0:
        return _empty_advice(now)

    holdings_sig = await _open_holdings_signature(session, portfolio.id)
    materiality = _materiality_hash(holdings_sig, stats)

    cached = await _read_cache(redis, portfolio.id)
    if cached is not None and cached.get("materiality") == materiality:
        # Materially unchanged → serve the cached advice as fresh.
        return _advice_from_cache(cached, stale=False)

    prompt = _build_prompt(stats)
    try:
        client = get_intelligence_client()
        generation, usage = await client.generate_structured_with_usage(
            prompt,
            PortfolioAdviceGeneration,
            system=_SYSTEM_PROMPT,
            temperature=_ADVICE_TEMPERATURE,
            max_tokens=_ADVICE_MAX_TOKENS,
            priority=Priority.HIGH,
        )
    except _DEGRADE_EXCEPTIONS as exc:
        logger.warning("portfolio_advice: LLM degraded (%s) — serving cached/graceful", exc)
        return _degrade(cached)
    except Exception as exc:  # noqa: BLE001 — advice must never 500 the endpoint
        logger.error("portfolio_advice: unexpected LLM error: %s", exc, exc_info=True)
        return _degrade(cached)

    clean = _apply_advice_guardrails(generation, prompt)
    advice = PortfolioAdvice(
        assessment=clean.assessment,
        key_risks=clean.key_risks,
        considerations=clean.considerations,
        per_position=clean.per_position,
        disclaimer=_disclaimer(),
        stale=False,
        generated_at=now,
        model_id=usage.get("model_id") if isinstance(usage, dict) else None,
    )
    await _write_cache(redis, portfolio.id, materiality, advice)
    return advice


# ──────────────────────────────────────────────────────────────────────────────
# Prompt
# ──────────────────────────────────────────────────────────────────────────────

def _build_prompt(stats: PortfolioInsightStats) -> str:
    """Number-rich, fully-grounded snapshot — everything the model may cite is here."""
    car = stats.capital_at_risk
    sn = stats.single_name
    sec = stats.sector
    cor = stats.correlation

    lines: list[str] = [
        f"Portfolio value: ₹{stats.portfolio_value:,.0f}",
        f"Open positions: {stats.open_position_count}",
        (
            f"Capital at risk: ₹{car.capital_at_risk:,.0f} "
            f"({car.capital_at_risk_pct:.1f}% of portfolio); "
            f"{car.positions_with_stop} with a stop, {car.positions_without_stop} without a stop"
        ),
        (
            f"Largest single name: {sn.max_weight_symbol} at {sn.max_weight_pct:.1f}% of portfolio "
            f"(HHI {sn.hhi:.3f}, effective positions {sn.effective_positions:.1f})"
        ),
        "Top holdings by weight: "
        + ", ".join(f"{h.symbol} {h.weight_pct:.1f}%" for h in sn.top_holdings),
    ]
    if sec.max_sector:
        lines.append(f"Most-concentrated sector: {sec.max_sector} at {sec.max_sector_weight_pct:.1f}%")
    if sec.breakdown:
        lines.append(
            "Sector weights: " + ", ".join(f"{b.sector} {b.weight_pct:.1f}%" for b in sec.breakdown)
        )
    if cor.max_pair_correlation is not None and cor.max_pair:
        lines.append(
            f"Highest return correlation: {cor.max_pair[0]} & {cor.max_pair[1]} "
            f"at {cor.max_pair_correlation:.2f}; average pairwise correlation "
            f"{cor.avg_pairwise_correlation:.2f} "
            f"({cor.covered_positions} names covered, {cor.excluded_positions} excluded)"
        )
    if stats.stress.scenarios:
        lines.append(
            "Stress scenarios: "
            + "; ".join(f"{s.label} → {s.delta_pct:.1f}%" for s in stats.stress.scenarios)
        )
    if stats.notes:
        lines.append("Data gaps: " + " ".join(stats.notes))
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Guardrails (reused from the explanation pipeline)
# ──────────────────────────────────────────────────────────────────────────────

def _apply_advice_guardrails(
    generation: PortfolioAdviceGeneration, prompt: str
) -> PortfolioAdviceGeneration:
    """
    Strip guarantee/certainty language and any number not grounded in the prompt,
    per field.  Reuses the exact tested guardrails from ``explanation_worker``
    (lazy-imported to keep this module's import light).
    """
    from app.ai.intelligence.explanation_worker import (  # lazy: avoids heavy import at module load
        _PRICE_TOKEN_RE,
        _strip_guarantee_language,
        _strip_ungrounded_numbers,
    )

    def clean(text: str) -> str:
        filtered, _ = _strip_guarantee_language(text)
        filtered, _ = _strip_ungrounded_numbers(filtered, prompt, extra_token_re=_PRICE_TOKEN_RE)
        return filtered.strip()

    assessment = clean(generation.assessment) or _ASSESSMENT_FALLBACK
    key_risks = [c for c in (clean(x) for x in generation.key_risks) if c]
    considerations = [c for c in (clean(x) for x in generation.considerations) if c]
    per_position = [
        PerPositionNote(symbol=p.symbol, note=cleaned)
        for p in generation.per_position
        if (cleaned := clean(p.note))
    ]
    return PortfolioAdviceGeneration(
        assessment=assessment,
        key_risks=key_risks,
        considerations=considerations,
        per_position=per_position,
    )


def _disclaimer() -> str:
    """The fixed regulatory notice (reused verbatim from the explanation pipeline)."""
    from app.ai.intelligence.explanation_worker import _REGULATORY_DISCLAIMER
    return _REGULATORY_DISCLAIMER.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Materiality + cache
# ──────────────────────────────────────────────────────────────────────────────

async def _open_holdings_signature(session: AsyncSession, portfolio_id: UUID) -> list[list]:
    """Sorted (instrument_key, qty, side) of open positions — the structural fingerprint."""
    from app.models.paper_trading import PaperPosition

    rows = (await session.execute(
        select(PaperPosition.instrument_key, PaperPosition.quantity, PaperPosition.side)
        .where(and_(PaperPosition.portfolio_id == portfolio_id, PaperPosition.status == "OPEN"))
    )).all()
    return sorted([ik, int(qty), side] for ik, qty, side in rows)


def _materiality_hash(holdings_sig: list[list], stats: PortfolioInsightStats) -> str:
    """
    Stable hash of what makes advice materially different: the holdings set/size,
    plus coarsely-bucketed stats.  Small P&L drift keeps the same hash (cache
    hit); a structural change or a stat crossing a bucket boundary changes it.
    """
    def bucket(value: float | None, step: float) -> float | None:
        return None if value is None else round(value / step) * step

    buckets = {
        "n": stats.open_position_count,
        "car_pct": bucket(stats.capital_at_risk.capital_at_risk_pct, 1.0),
        "max_weight": bucket(stats.single_name.max_weight_pct, 5.0),
        "eff_pos": bucket(stats.single_name.effective_positions, 1.0),
        "top_sector": stats.sector.max_sector,
        "sector_pct": bucket(stats.sector.max_sector_weight_pct, 5.0),
        "max_corr": bucket(stats.correlation.max_pair_correlation, 0.1),
    }
    blob = json.dumps({"h": holdings_sig, "b": buckets}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _advice_key(portfolio_id: UUID) -> str:
    return f"{_ADVICE_KEY_PREFIX}{portfolio_id}"


async def _read_cache(redis: Any, portfolio_id: UUID) -> dict[str, Any] | None:
    raw = await redis.get(_advice_key(portfolio_id))
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and "advice" in data else None


async def _write_cache(
    redis: Any, portfolio_id: UUID, materiality: str, advice: PortfolioAdvice
) -> None:
    payload = json.dumps({
        "materiality": materiality,
        "advice": advice.model_dump(mode="json"),
    })
    ttl = get_settings().INSIGHT_ADVICE_CACHE_TTL_SECONDS
    await redis.set(_advice_key(portfolio_id), payload, ex=ttl)


def _advice_from_cache(cached: dict[str, Any], *, stale: bool) -> PortfolioAdvice:
    advice = PortfolioAdvice.model_validate(cached["advice"])
    advice.stale = stale
    return advice


def _degrade(cached: dict[str, Any] | None) -> PortfolioAdvice:
    """Serve last-cached advice (stale) or a graceful unavailable advice — never raise."""
    if cached is not None:
        try:
            return _advice_from_cache(cached, stale=True)
        except Exception:  # noqa: BLE001 — corrupt cache must not break the fallback
            logger.warning("portfolio_advice: cached advice unparseable during degrade")
    return _unavailable_advice()


# ──────────────────────────────────────────────────────────────────────────────
# Canned advices
# ──────────────────────────────────────────────────────────────────────────────

def _empty_advice(now: datetime) -> PortfolioAdvice:
    return PortfolioAdvice(
        assessment="You have no open positions to analyze.",
        key_risks=[],
        considerations=[],
        per_position=[],
        disclaimer=_disclaimer(),
        stale=False,
        generated_at=now,
        model_id=None,
    )


def _unavailable_advice() -> PortfolioAdvice:
    return PortfolioAdvice(
        assessment=_UNAVAILABLE_ASSESSMENT,
        key_risks=[],
        considerations=[],
        per_position=[],
        disclaimer=_disclaimer(),
        stale=True,
        generated_at=datetime.now(timezone.utc),
        model_id=None,
    )
