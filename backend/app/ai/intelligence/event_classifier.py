"""
Event Classifier
================
Classifies financial events using a three-level fallback chain:

  1. LLM (NIM primary → Ollama fallback)  — highest accuracy, structured output
  2. GPT-4o  (reserved for future wiring)
  3. Rule-based  (final fallback — deterministic, always succeeds)

Symbol extraction is deliberately decoupled from classification confidence.
The LLM may correctly identify affected companies even when uncertain about
event type or impact score; discarding those symbols on a confidence miss
is the primary reason events arrive at the signal assembler with empty
`affected_symbols`.  The fix is a three-source merge strategy:

  source A — Structured LLM output (validated NSE tickers + company names)
  source B — Content-level extraction  (ALL-CAPS ticker scan + corporate-
             suffix company-name regex, fed through normalize_and_validate_symbols)
  source C — NER entities  (rule-based fallback via spaCy, when available)

All three sources are merged and validated against instrument_master before
the final AIEventClassification record is written.

Each classification also produces two temporal decay half-lives per the
two-component Hawkes-kernel model:
  - decay_half_life_hours      (fast component, intraday)
  - decay_slow_half_life_hours (slow component, multi-day fundamental)

Combined decay: 0.7 · 0.5^(t/fast_hl) + 0.3 · 0.5^(t/slow_hl)
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AINLPResult, AIEventClassification
from app.ai.intelligence.llm_client import get_ollama_client
from app.models.upstox_data import InstrumentMaster
from app.services.symbol_validator import symbol_validator

logger = logging.getLogger(__name__)

# Two-component decay half-lives (fast_hl_hours, slow_hl_hours) per event type.
# Derived from Hawkes-process calibration on NSE equity event data.
# fast component (70%): intraday price reaction
# slow component (30%): multi-day fundamental repricing
_DECAY_HALF_LIVES: dict[str, tuple[int, int]] = {
    "earnings":           (12,  72),
    "fed_announcement":   ( 8,  48),
    "merger_acquisition": (12, 120),
    "regulatory":         (24, 168),
    "geopolitical":       ( 8,  72),
    "market_data":        ( 6,  24),
    "company_news":       (12,  48),
    "sector_news":        (18,  72),
    "general":            (12,  48),
}

_DEFAULT_FAST_HL = 12
_DEFAULT_SLOW_HL = 48

_VALID_EVENT_TYPES = frozenset(_DECAY_HALF_LIVES.keys())


def _half_lives_for(event_type: str) -> tuple[int, int]:
    """Return (fast_hl, slow_hl) for an event type, falling back to 'general'."""
    return _DECAY_HALF_LIVES.get(event_type, (_DEFAULT_FAST_HL, _DEFAULT_SLOW_HL))


_SYMBOL_RE = re.compile(r'^[A-Z0-9&\-]{2,20}$')
# Minimum candidate length for name-based instrument_master lookup.
# 2-char strings are validated by exact-match only (substring ILIKE on a
# 2-char pattern produces too many false positives — e.g. "LT" appears in
# every company name containing "LIMITED").
_MIN_NAME_MATCH_LEN = 4


# ── Content-level symbol extraction ───────────────────────────────────────────

# ALL-CAPS tokens: 3–20 chars (NSE tickers are always upper-case alphanumeric)
_ALLCAPS_RE = re.compile(r'\b([A-Z][A-Z0-9&\-]{2,19})\b')

# Title-Case company names ending with common Indian corporate suffixes.
# Requires every word to begin with an uppercase letter to avoid matching
# mid-sentence phrases like "the bank" or "clean energy".
_CORP_NAME_RE = re.compile(
    r'\b'
    r'([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z0-9]+){0,4}\s+'
    r'(?:Ltd\.?|Limited|Industries|Enterprises|Technologies|Tech|'
    r'Services|Solutions|Corporation|Corp\.?|Bank|Finance|Capital|'
    r'Energy|Power|Holdings|Pharma|Chemicals|Healthcare|'
    r'Infrastructure|Mining|Steel|Cement|Textiles|Media|Telecom|'
    r'Retail|Logistics|Ventures|Realty|Properties|Motors|Auto))'
    r'\b',
)

# Common financial/market acronyms and short tokens that are NOT NSE tickers.
# Checked against ALL-CAPS extraction before passing to instrument_master.
_NON_TICKER_WORDS: frozenset[str] = frozenset({
    # Regulatory bodies
    "NSE", "BSE", "SEBI", "RBI", "IRDAI", "PFRDA", "AMFI", "NPCI",
    # Economic indicators
    "GDP", "CPI", "WPI", "PMI", "IIP", "CAD",
    # Market instruments / categories
    "FII", "DII", "FPI", "MF", "ETF", "IPO", "NFO", "OFS", "SME",
    "SGB", "NCD", "NCB",
    # Indices (not individual stocks)
    "NIFTY", "SENSEX", "NASDAQ", "NYSE", "SGX", "MCX", "NCDEX", "COMEX",
    # Financial metrics
    "SIP", "EMI", "NPA", "GNPA", "NNPA", "PAT", "EBIT", "EBITDA",
    "ROCE", "ROE", "ROA", "EPS", "NAV", "AUM",
    # Valuation multiples
    "PE", "PB", "PEG", "DCF", "NPV",
    # Reporting periods
    "YOY", "QOQ", "FY", "H1", "H2", "Q1", "Q2", "Q3", "Q4",
    # Trading terms
    "LTP", "ATH", "ATL", "SL", "TP",
    # Corporate roles / governance
    "CEO", "MD", "CFO", "CTO", "COO", "AGM", "EGM", "BOD", "QIP",
    # Geographies / currencies (not stocks)
    "US", "UK", "EU", "UAE", "USA", "USD", "INR", "GBP", "EUR",
    # Common two-word fragments that appear in news caps
    "AI", "IT", "IS", "OF", "IN", "ON", "AT", "BY", "TO", "AN",
})


# ── Pydantic schema for structured LLM output ─────────────────────────────────

_EventTypeLiteral = Literal[
    "earnings",
    "fed_announcement",
    "merger_acquisition",
    "regulatory",
    "geopolitical",
    "market_data",
    "company_news",
    "sector_news",
    "general",
]


class _ClassificationSchema(BaseModel):
    """
    Pydantic schema for validated LLM event classification output.

    Used with ``generate_structured`` (Instructor) for type-safe, retryable
    structured output.  Instructor retries up to 2× on validation failure,
    eliminating the JSON-parse errors that previously zeroed out confidence.
    """

    event_type: _EventTypeLiteral = Field(
        default="general",
        description="Category of the financial event.",
    )
    impact_score: float = Field(
        default=50.0,
        ge=0.0,
        le=100.0,
        description="Market impact severity: 0 = no impact, 100 = extreme impact.",
    )
    sentiment: Literal["bullish", "bearish", "neutral"] = Field(
        default="neutral",
        description="Expected directional market sentiment.",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Your confidence in this classification (0.0–1.0).",
    )
    affected_symbols: list[str] = Field(
        default_factory=list,
        description=(
            "NSE trading symbols for every company mentioned or implicated. "
            "Use the exact NSE ticker: RELIANCE, TCS, INFY, HDFCBANK, HINDZINC. "
            "Include all identifiable companies; return [] only for macro/index events."
        ),
    )
    reasoning: str = Field(
        default="",
        description="One-sentence rationale for the classification.",
    )
    decay_hours: int = Field(
        default=12,
        ge=4,
        le=48,
        description="Fast decay half-life in hours (intraday price reaction).",
    )
    decay_slow_hours: int = Field(
        default=48,
        ge=24,
        le=168,
        description="Slow decay half-life in hours (multi-day fundamental repricing).",
    )


# ── Symbol normalisation (shared, module-level) ────────────────────────────────

async def normalize_and_validate_symbols(
    db: AsyncSession,
    raw_symbols: list[str],
    *,
    exchange: str = "NSE",
) -> list[str]:
    """
    Validate and normalize raw LLM/NER/content strings into verified NSE symbols.

    Two-pass strategy:

    Pass 1 — Exact symbol match (Redis-cached via SymbolValidatorService)
        Handles the common case where the LLM or content regex outputs a
        correct NSE ticker ("RELIANCE", "TCS").

    Pass 2 — Company name match (DB ILIKE, for remaining unresolved candidates)
        For candidates ≥ ``_MIN_NAME_MATCH_LEN`` chars that did not match as
        exact tickers, a trigram-accelerated ILIKE query against
        ``instrument_master.name`` maps company-name strings produced by NER
        ("Reliance Industries Limited") or the LLM to validated tickers.

    Only NSE EQ equities are returned; derivatives and indices are excluded.
    Returns a deduplicated, order-preserving list of valid trading_symbol values.
    Fails gracefully — DB or cache errors return an empty list rather than
    propagating upstream.
    """
    if not raw_symbols:
        return []

    candidates: list[str] = [
        s.strip().upper() for s in raw_symbols if s and s.strip()
    ]
    if not candidates:
        return []

    # Ordered-set accumulator preserving first-occurrence order.
    validated: dict[str, None] = {}

    # ── Pass 1: exact symbol validation (cached) ───────────────────────────────
    try:
        exact_hits = await symbol_validator.validate_symbols(candidates, db)
        for sym in exact_hits:
            validated[sym] = None
    except Exception as exc:
        logger.warning(
            "SymbolValidatorService.validate_symbols failed: %s — "
            "falling back to name-only matching", exc,
        )

    # Collect candidates that were not resolved by exact match.
    unresolved = [
        c for c in candidates
        if c not in validated and len(c) >= _MIN_NAME_MATCH_LEN
    ]

    # ── Pass 2: company-name substring match ───────────────────────────────────
    if unresolved:
        try:
            # Build ILIKE conditions — the GIN trigram index on upper(name)
            # makes these fast even without an exact prefix match.
            name_conds = [
                func.upper(InstrumentMaster.name).contains(c)
                for c in unresolved
            ]

            stmt = (
                select(InstrumentMaster.trading_symbol, InstrumentMaster.name)
                .where(
                    InstrumentMaster.exchange == exchange,
                    InstrumentMaster.instrument_type == "EQ",
                    or_(*name_conds),
                )
                # Cap results at (unresolved × 3) to guard against pathological
                # name overlaps while still returning all plausible matches.
                .limit(max(len(unresolved) * 3, 10))
            )
            result = await db.execute(stmt)
            rows = result.all()

            # Map upper(name) → trading_symbol for fast Python-side lookup.
            name_map: dict[str, str] = {
                row.name.upper(): row.trading_symbol
                for row in rows
                if row.name
            }

            for c in unresolved:
                if c in validated:
                    continue
                for known_name, sym in name_map.items():
                    if c in known_name and sym not in validated:
                        validated[sym] = None
                        break

        except Exception as exc:
            logger.warning(
                "Name-based symbol resolution failed: %s — "
                "returning only exact-match results", exc,
            )

    valid_list = list(validated.keys())

    dropped = [c for c in candidates if c not in valid_list]
    if dropped:
        logger.debug(
            "Symbol normalisation: %d/%d candidates resolved; dropped=%s",
            len(valid_list), len(set(candidates)), dropped,
        )

    return valid_list


# ── Classifier ─────────────────────────────────────────────────────────────────

class EventClassifier:
    """Classifies financial events with LLM-primary / rule-based fallback chain."""

    def __init__(self, use_llm: bool = True) -> None:
        self.use_llm = use_llm
        self.ollama_client = get_ollama_client() if use_llm else None

    async def classify(
        self,
        db: AsyncSession,
        nlp_result_id: int,
        content: str,
        entities: dict[str, Any],
    ) -> AIEventClassification:
        """
        Classify event type, impact score, temporal decay, and affected symbols.

        Symbol extraction uses a three-source merge strategy that is deliberately
        decoupled from classification confidence:

          A) LLM symbols — saved *before* any confidence-based fallback so they
             are never discarded even when the model is uncertain about event type.
          B) Content-level symbols — ALL-CAPS tickers and corporate-suffix company
             names extracted directly from the raw text, validated via DB lookup.
          C) NER symbols — spaCy entity output (rule-based fallback, when available).

        Sources A + B + C are merged and deduplicated before the final
        validate-against-instrument_master pass.  Runs the full fallback chain and
        persists the result.  Always returns a valid record.
        """
        llm_result = await self._classify_with_ollama(content, entities)

        # ── Capture LLM symbols before any confidence-based replacement ────────
        # The LLM may correctly identify affected companies even when its
        # classification confidence is low (e.g. ambiguous event type).
        # Discarding these symbols on a confidence miss is the primary reason
        # events reach the signal assembler with empty affected_symbols.
        llm_symbols: list[str] = list(llm_result.get("affected_symbols") or [])

        if llm_result["confidence"] < 0.7:
            logger.info(
                "LLM confidence %.2f < 0.7 for nlp_result_id=%d — trying GPT-4o",
                llm_result["confidence"], nlp_result_id,
            )
            gpt_result = await self._classify_with_gpt4o(content, entities)
            if gpt_result["confidence"] > llm_result["confidence"]:
                llm_result = gpt_result
                # Merge any additional symbols from the GPT pass.
                for sym in (llm_result.get("affected_symbols") or []):
                    if sym not in llm_symbols:
                        llm_symbols.append(sym)

        # Classification result (event_type / impact / confidence).
        # Rule-based is only ever used for the *classification* fields — never
        # as the sole source for symbols, because it has no symbol extraction
        # capability when spaCy NER returns empty entities (the common case).
        if llm_result["confidence"] < 0.5:
            logger.info(
                "LLM confidence %.2f < 0.5 for nlp_result_id=%d — using rule-based",
                llm_result["confidence"], nlp_result_id,
            )
            classification_result = self._classify_rule_based(content, entities)
        else:
            classification_result = llm_result

        # ── Symbol resolution: merge all three sources ─────────────────────────
        # Start with classification result symbols (LLM or rule-based NER).
        raw_symbols: list[str] = list(classification_result.get("affected_symbols") or [])

        # Fold in LLM symbols captured before the confidence fallback.
        for sym in llm_symbols:
            if sym not in raw_symbols:
                raw_symbols.append(sym)

        # Content-level extraction: purely text-driven, no LLM/NER dependency.
        content_symbols = await self._extract_symbols_from_content(db, content)
        for sym in content_symbols:
            if sym not in raw_symbols:
                raw_symbols.append(sym)

        validated_symbols = await normalize_and_validate_symbols(db, raw_symbols)

        fast_hl, slow_hl = _half_lives_for(classification_result["event_type"])
        # LLM may override the fast half-life; slow half-life always comes from
        # the canonical lookup table to ensure consistency across the ensemble.
        fast_hl = classification_result.get("decay_hours", fast_hl)

        classification = AIEventClassification(
            nlp_result_id=nlp_result_id,
            event_type=classification_result["event_type"],
            impact_score=Decimal(str(classification_result["impact_score"])),
            affected_symbols=validated_symbols,
            classification_confidence=Decimal(str(classification_result["confidence"])),
            reasoning=classification_result.get("reasoning", ""),
            decay_half_life_hours=fast_hl,
            decay_slow_half_life_hours=slow_hl,
        )

        db.add(classification)
        await db.commit()
        await db.refresh(classification)

        logger.info(
            "Classified nlp_result_id=%d: type=%s impact=%.1f conf=%.2f "
            "symbols=%s fast_hl=%dh slow_hl=%dh",
            nlp_result_id,
            classification_result["event_type"],
            float(classification_result["impact_score"]),
            float(classification_result["confidence"]),
            validated_symbols,
            fast_hl,
            slow_hl,
        )
        return classification

    # ── Content-level symbol extraction ───────────────────────────────────────

    async def _extract_symbols_from_content(
        self,
        db: AsyncSession,
        content: str,
    ) -> list[str]:
        """
        Extract NSE trading symbol candidates directly from event content text.

        Runs two regex passes over the raw content and feeds the candidates
        into ``normalize_and_validate_symbols`` for DB-backed validation.
        This pass is completely independent of the LLM and spaCy NER — it
        provides reliable symbol coverage even when both AI layers fail.

        Pass 1 — ALL-CAPS tokens (3–20 chars) that could be NSE tickers.
                  Common financial acronyms are excluded via _NON_TICKER_WORDS.

        Pass 2 — Title-Case company names ending with Indian corporate suffixes
                  (Ltd, Limited, Industries, Technologies, Bank, etc.).
                  Requires every component word to start with an uppercase letter
                  to avoid false positives from mid-sentence phrases.
        """
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(token: str) -> None:
            token = token.strip()
            if token and token not in seen:
                seen.add(token)
                candidates.append(token)

        # Pass 1: ALL-CAPS tokens — direct NSE ticker candidates.
        # Strip trailing hyphens/ampersands that the regex can absorb when a
        # hyphenated phrase like "US-Iran" produces "US-" as a match.
        for m in _ALLCAPS_RE.finditer(content):
            token = m.group(1).rstrip("-&")
            if len(token) >= 3 and token not in _NON_TICKER_WORDS:
                _add(token)

        # Pass 2: Title-Case corporate names — resolved via name-based DB lookup
        for m in _CORP_NAME_RE.finditer(content):
            _add(m.group(1).strip())

        if not candidates:
            return []

        try:
            return await normalize_and_validate_symbols(db, candidates)
        except Exception as exc:
            logger.debug("Content-level symbol extraction failed (non-fatal): %s", exc)
            return []

    # ── LLM classifiers ────────────────────────────────────────────────────────

    async def _classify_with_ollama(
        self,
        content: str,
        entities: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Primary LLM classification using generate_structured (Instructor + Pydantic).

        Switching from generate_json to generate_structured eliminates JSON-parse
        errors as a failure mode: Instructor retries up to 2× on validation
        failure before raising, so malformed model output no longer silently
        returns confidence=0.0 and falls through to the rule-based path.
        """
        if not self.use_llm or not self.ollama_client:
            return self._classify_rule_based(content, entities)

        try:
            companies = (entities.get("companies") or [])[:5]
            entity_hint = (
                f"\nExtracted entities — companies: {companies}"
                if companies else ""
            )

            prompt = (
                f"Classify this Indian financial market event.\n\n"
                f"Content: {content[:1500]}"
                f"{entity_hint}\n\n"
                f"Instructions for affected_symbols:\n"
                f"  • List every NSE-listed company mentioned or implicated.\n"
                f"  • Use the exact NSE ticker symbol, not the full company name.\n"
                f"    Examples: RELIANCE (Reliance Industries), TCS (Tata Consultancy),\n"
                f"    HDFCBANK (HDFC Bank), INFY (Infosys), HINDZINC (Hindustan Zinc),\n"
                f"    VEDL (Vedanta), IOC (Indian Oil), ONGC, WIPRO, BAJFINANCE.\n"
                f"  • For sector-wide or macro events with no specific company, return [].\n"
                f"  • For decay_hours: estimate intraday price reaction window (4–48h).\n"
                f"  • For decay_slow_hours: estimate fundamental repricing window (24–168h)."
            )

            schema: _ClassificationSchema = await self.ollama_client.generate_structured(
                prompt=prompt,
                response_model=_ClassificationSchema,
                system=(
                    "You are an expert financial event classifier for Indian equity markets "
                    "(NSE/BSE). Always output exact NSE trading symbols — never full company "
                    "names. If you are not certain of a symbol, omit it rather than guessing."
                ),
                temperature=0.1,
            )

            return {
                "event_type":       schema.event_type,
                "impact_score":     schema.impact_score,
                "confidence":       schema.confidence,
                "affected_symbols": schema.affected_symbols,
                "reasoning":        schema.reasoning,
                "decay_hours":      schema.decay_hours,
                "decay_slow_hours": schema.decay_slow_hours,
            }

        except Exception as exc:
            logger.warning("LLM classification failed: %s — falling back to rule-based", exc)
            return {
                "confidence":       0.0,
                "event_type":       "general",
                "impact_score":     50.0,
                "affected_symbols": [],
                "reasoning":        "",
                "decay_hours":      _DEFAULT_FAST_HL,
                "decay_slow_hours": _DEFAULT_SLOW_HL,
            }

    async def _classify_with_gpt4o(
        self,
        content: str,
        entities: dict[str, Any],
    ) -> dict[str, Any]:
        # GPT-4o integration reserved for future implementation.
        # Delegates to rule-based to preserve the fallback chain contract.
        return self._classify_rule_based(content, entities)

    # ── Rule-based fallback ────────────────────────────────────────────────────

    def _classify_rule_based(
        self,
        content: str,
        entities: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Deterministic keyword-based classification.  Always succeeds.

        Symbol extraction uses NER company entities from spaCy.  Note that
        spaCy ``en_core_web_sm`` has limited recall on Indian company names;
        the content-level extraction pass in ``classify()`` supplements this
        reliably regardless of NER quality.
        """
        content_lower = content.lower()

        event_type = self._detect_event_type(content_lower)
        impact_score = self._score_impact(event_type, content_lower)
        sentiment = self._detect_sentiment(content_lower)
        # NER companies (may be empty for Indian content — supplemented by
        # _extract_symbols_from_content in the parent classify() call).
        affected_symbols = (entities.get("companies") or [])[:3]
        fast_hl, slow_hl = _half_lives_for(event_type)

        return {
            "event_type":       event_type,
            "impact_score":     impact_score,
            "confidence":       0.60,
            "affected_symbols": affected_symbols,
            "sentiment":        sentiment,
            "reasoning":        f"Rule-based: detected keywords for {event_type}",
            "decay_hours":      fast_hl,
            "decay_slow_hours": slow_hl,
        }

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _detect_event_type(content_lower: str) -> str:
        if any(w in content_lower for w in (
            "earnings", "revenue", "profit", "eps", "quarterly result",
        )):
            return "earnings"
        if any(w in content_lower for w in (
            "fed", "federal reserve", "interest rate", "fomc", "rbi", "repo rate",
        )):
            return "fed_announcement"
        if any(w in content_lower for w in (
            "merger", "acquisition", "takeover", "buyout", "demerger",
        )):
            return "merger_acquisition"
        if any(w in content_lower for w in (
            "sebi", "regulatory", "compliance", "investigation", "penalty", "sec",
        )):
            return "regulatory"
        if any(w in content_lower for w in (
            "war", "conflict", "sanctions", "trade war", "geopolitical",
        )):
            return "geopolitical"
        if any(w in content_lower for w in (
            "gdp", "unemployment", "inflation", "cpi", "iip", "pmi",
        )):
            return "market_data"
        if any(w in content_lower for w in (
            "nifty", "sensex", "sector", "fii", "dii", "index",
        )):
            return "sector_news"
        return "general"

    @staticmethod
    def _score_impact(event_type: str, content_lower: str) -> float:
        base: dict[str, float] = {
            "earnings":           70.0,
            "fed_announcement":   85.0,
            "merger_acquisition": 80.0,
            "regulatory":         75.0,
            "geopolitical":       90.0,
            "market_data":        65.0,
            "company_news":       55.0,
            "sector_news":        50.0,
            "general":            45.0,
        }
        score = base.get(event_type, 45.0)
        if any(w in content_lower for w in (
            "surge", "crash", "record", "historic", "crisis",
        )):
            score = min(score + 10.0, 100.0)
        return score

    @staticmethod
    def _detect_sentiment(content_lower: str) -> str:
        if any(w in content_lower for w in (
            "surge", "soar", "rally", "gain", "beat", "positive", "upgrade",
        )):
            return "bullish"
        if any(w in content_lower for w in (
            "plunge", "crash", "fall", "miss", "negative", "loss", "downgrade",
        )):
            return "bearish"
        return "neutral"
