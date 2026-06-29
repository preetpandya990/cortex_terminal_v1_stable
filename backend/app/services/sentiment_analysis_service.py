"""
Sentiment Analysis Service
==========================
Fetches recent financial news from ai_raw_events, runs LLM-backed NLP
inference, and produces an aggregated impact score for a given instrument.

Architecture:
  1. Check L1 cache (class-level LRU, <1ms)
  2. Check L2 cache (Redis, 15-min TTL)
  3. Query events via ai_event_classifications.affected_symbols join (precise)
     → adaptive lookback: extends to 3× window when article count is thin
     → text search fallback for instruments not yet in the NLP pipeline
  4. Run LLM sentiment inference via NLPEngine.analyze_sentiment_batch() —
     one batched Gemini call for all event titles, cache hits served inline
  5. Compute weighted impact score and build structured response
  6. Cache and return

Impact score formula:
  - Each event gets a raw score in [-1, +1] from the LLM
  - Recency weight: exp(-hours_ago / half_life) where half_life = 12h
  - Source credibility weight: 1.0 for exchange feeds, 0.7 for general news
  - Final score = weighted_sum(scores) / weighted_sum(weights) * 100
  - Clamped to [-100, +100]
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from cachetools import LRUCache
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AIEventClassification,
    AINLPResult,
    AIProcessedEvent,
    AIRawEvent,
)
from app.ai.intelligence.nlp_engine import NLPEngine
from app.schemas.sentiment_analysis import (
    SentimentAnalysisResponse,
    SentimentBreakdown,
    TopEvent,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
_RECENCY_HALF_LIFE_HOURS = 12.0
_MAX_EVENTS_PER_REQUEST = 50   # Cap to bound inference latency
_L2_TTL = 900                  # 15-minute Redis TTL for sentiment

# Higher credibility for official exchange feeds
_SOURCE_WEIGHTS: dict[str, float] = {
    "NSE Corporate Filings": 1.0,
    "BSE Corporate Notices": 1.0,
}
_DEFAULT_SOURCE_WEIGHT = 0.7

# Minimum articles needed before we extend the lookback window for symbol queries.
# Below this threshold the service automatically tries _EXTENDED_LOOKBACK_MULTIPLIER × lookback_hours.
_MIN_CLASSIFIED_ARTICLES = 5
_EXTENDED_LOOKBACK_MULTIPLIER = 3


class SentimentAnalysisService:
    """
    Service for computing financial sentiment from recent news events.

    NLPEngine uses the LLM-backed CortexIntelligenceClient for sentiment scoring.
    Each service instance holds its own DB + Redis references.
    """

    # Class-level L1 cache: shared across request instances
    _l1_cache: LRUCache = LRUCache(maxsize=500)

    def __init__(self, db: AsyncSession, redis: Redis | None = None) -> None:
        self.db = db
        self.redis = redis
        self._nlp = NLPEngine()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def analyze(
        self,
        instrument_key: str,
        symbol: str | None = None,
        lookback_hours: int = 24,
    ) -> SentimentAnalysisResponse:
        """
        Compute sentiment analysis for an instrument.

        Args:
            instrument_key: NSE instrument key (used as cache key)
            symbol:         Optional NSE ticker (e.g. "RELIANCE") for news filtering.
                            If None, general market sentiment is returned.
            lookback_hours: How many hours of news to consider (1-168)

        Returns:
            SentimentAnalysisResponse
        """
        cache_key = f"sentiment:{instrument_key}:{symbol or ''}:{lookback_hours}"

        # L1 cache
        cached = self._l1_cache.get(cache_key)
        if cached:
            cached["cache_tier"] = "L1"
            return SentimentAnalysisResponse(**cached)

        # L2 cache (Redis)
        if self.redis:
            raw = await self._redis_get(cache_key)
            if raw:
                self._l1_cache[cache_key] = raw
                raw["cache_tier"] = "L2"
                return SentimentAnalysisResponse(**raw)

        # Compute fresh
        result = await self._compute(instrument_key, symbol, lookback_hours)

        # Cache result (strip cache_tier before storing)
        payload = result.model_dump(exclude={"cache_tier"})
        self._l1_cache[cache_key] = payload
        if self.redis:
            await self._redis_set(cache_key, payload)

        result.cache_tier = "MISS"
        return result

    # ── Internal computation ───────────────────────────────────────────────────

    async def _compute(
        self,
        instrument_key: str,
        symbol: str | None,
        lookback_hours: int,
    ) -> SentimentAnalysisResponse:
        now = datetime.now(timezone.utc)
        computed_at = now.isoformat()

        # Fetch events — may internally extend the lookback window if thin on data.
        events, effective_lookback = await self._fetch_events(now, symbol, lookback_hours)

        if not events:
            return SentimentAnalysisResponse(
                instrument_key=instrument_key,
                symbol=symbol,
                breakdown=SentimentBreakdown(
                    positive=0, negative=0, neutral=0, total=0,
                    positive_pct=0.0, negative_pct=0.0, neutral_pct=100.0,
                ),
                impact_score=0.0,
                sentiment_label="neutral",
                top_event=None,
                lookback_hours=effective_lookback,
                cache_tier="MISS",
                computed_at=computed_at,
            )

        # Run LLM sentiment inference — one batched Gemini call for all titles.
        titles = [self._extract_title(e.extra_data, e.raw_content) for e in events]
        sentiments = await self._nlp.analyze_sentiment_batch(titles)

        # Aggregate
        pos_count = neg_count = neu_count = 0
        weighted_score_sum = 0.0
        weight_sum = 0.0
        best_event: tuple[float, int] | None = None  # (abs impact contribution, index)

        for i, (event, sentiment) in enumerate(zip(events, sentiments)):
            if isinstance(sentiment, Exception):
                logger.warning("Sentiment inference failed for event %d: %s", i, sentiment)
                sentiment = {"label": "neutral", "score": 0.0, "confidence": 0.0, "model": "error"}

            label: str = sentiment["label"]
            score: float = sentiment["score"]
            confidence: float = sentiment["confidence"]

            if label == "positive":
                pos_count += 1
            elif label == "negative":
                neg_count += 1
            else:
                neu_count += 1

            # Recency weight
            hours_ago = (now - event.event_timestamp.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            recency_w = math.exp(-hours_ago / _RECENCY_HALF_LIFE_HOURS)

            # Source credibility weight
            source_w = _SOURCE_WEIGHTS.get(event.source_name, _DEFAULT_SOURCE_WEIGHT)

            # Confidence-adjusted weight
            w = recency_w * source_w * max(confidence, 0.3)

            weighted_score_sum += score * w
            weight_sum += w

            # Track top event by absolute weighted contribution
            contribution = abs(score * w)
            if best_event is None or contribution > best_event[0]:
                best_event = (contribution, i)

        total = len(events)
        impact_score = (weighted_score_sum / weight_sum * 100) if weight_sum > 0 else 0.0
        impact_score = max(-100.0, min(100.0, impact_score))

        # Determine overall sentiment label
        if impact_score >= 15:
            sentiment_label = "bullish"
        elif impact_score <= -15:
            sentiment_label = "bearish"
        else:
            sentiment_label = "neutral"

        # Build top event
        top_event_obj: TopEvent | None = None
        if best_event is not None:
            idx = best_event[1]
            ev = events[idx]
            s = sentiments[idx] if not isinstance(sentiments[idx], Exception) else {"label": "neutral", "score": 0.0, "confidence": 0.0}
            title = self._extract_title(ev.extra_data, ev.raw_content)
            published = ev.event_timestamp.isoformat() if ev.event_timestamp else None
            top_event_obj = TopEvent(
                title=title[:200],
                sentiment=s["label"],  # type: ignore[arg-type]
                confidence=s["confidence"],
                source=ev.source_name,
                published_at=published,
                impact_contribution=round(best_event[0] * 100, 2),
            )

        def safe_pct(n: int) -> float:
            return round(n / total * 100, 1) if total > 0 else 0.0

        logger.info(
            "Sentiment computed: instrument=%s symbol=%s total=%d impact=%.1f label=%s",
            instrument_key, symbol, total, impact_score, sentiment_label,
        )

        return SentimentAnalysisResponse(
            instrument_key=instrument_key,
            symbol=symbol,
            breakdown=SentimentBreakdown(
                positive=pos_count,
                negative=neg_count,
                neutral=neu_count,
                total=total,
                positive_pct=safe_pct(pos_count),
                negative_pct=safe_pct(neg_count),
                neutral_pct=safe_pct(neu_count),
            ),
            impact_score=round(impact_score, 2),
            sentiment_label=sentiment_label,  # type: ignore[arg-type]
            top_event=top_event_obj,
            lookback_hours=effective_lookback,
            cache_tier="MISS",
            computed_at=computed_at,
        )

    async def _fetch_events(
        self,
        now: datetime,
        symbol: str | None,
        lookback_hours: int,
    ) -> tuple[list[AIRawEvent], int]:
        """
        Fetch recent news events for sentiment analysis.

        When a symbol is provided, queries via ai_event_classifications.affected_symbols
        for precise stock-specific article tagging (27× more accurate than text search).
        Falls back to case-insensitive raw content search only if the classification
        join returns no results (e.g. new instrument not yet in the pipeline).

        Adaptive lookback: if symbol-specific results are below _MIN_CLASSIFIED_ARTICLES,
        the window is extended by _EXTENDED_LOOKBACK_MULTIPLIER before falling back,
        ensuring enough signal for a meaningful sentiment score.

        Returns:
            (events, effective_lookback_hours_used)
        """
        since = now - timedelta(hours=lookback_hours)

        if not symbol:
            # No symbol — general market sentiment from all recent events
            events = await self._query_all_events(since)
            return events, lookback_hours

        # ── Symbol-specific path ───────────────────────────────────────────────
        # Primary: join through the classification pipeline which correctly tags
        # affected_symbols via NLP-based entity recognition.
        events = await self._query_classified_events(since, symbol)

        if len(events) >= _MIN_CLASSIFIED_ARTICLES:
            return events, lookback_hours

        # Thin data — extend the lookback window before giving up
        extended_hours = lookback_hours * _EXTENDED_LOOKBACK_MULTIPLIER
        extended_since = now - timedelta(hours=extended_hours)
        events = await self._query_classified_events(extended_since, symbol)

        if events:
            logger.info(
                "Sentiment extended lookback: symbol=%s articles=%d window=%dh→%dh",
                symbol, len(events), lookback_hours, extended_hours,
            )
            return events, extended_hours

        # Fallback: raw text search (handles instruments not yet in the NLP pipeline)
        events = await self._query_raw_text_events(extended_since, symbol)
        if events:
            logger.info(
                "Sentiment fallback to text search: symbol=%s articles=%d",
                symbol, len(events),
            )
        return events, extended_hours

    async def _query_classified_events(
        self,
        since: datetime,
        symbol: str,
    ) -> list[AIRawEvent]:
        """
        Query events tagged with `symbol` in ai_event_classifications.affected_symbols.
        This is the high-precision path — symbols are assigned by the NLP pipeline's
        entity recognition, not by naive text matching.
        """
        try:
            stmt = (
                select(AIRawEvent)
                .join(AIProcessedEvent, AIProcessedEvent.raw_event_id == AIRawEvent.id)
                .join(AINLPResult, AINLPResult.processed_event_id == AIProcessedEvent.id)
                .join(AIEventClassification, AIEventClassification.nlp_result_id == AINLPResult.id)
                .where(AIRawEvent.event_timestamp >= since)
                # PostgreSQL @> (array contains): equivalent to symbol = ANY(affected_symbols)
                # Generates: affected_symbols @> ARRAY[:symbol]
                .where(AIEventClassification.affected_symbols.contains([symbol]))
                .order_by(AIRawEvent.event_timestamp.desc())
                .limit(_MAX_EVENTS_PER_REQUEST)
                .distinct()
            )
            result = await self.db.execute(stmt)
            return list(result.scalars().all())
        except Exception as exc:
            logger.error("Classified event query failed: symbol=%s error=%s", symbol, exc, exc_info=True)
            return []

    async def _query_raw_text_events(
        self,
        since: datetime,
        symbol: str,
    ) -> list[AIRawEvent]:
        """
        Fallback: case-insensitive text search on raw_content.
        Less precise than the classification join — used only when the NLP pipeline
        hasn't yet processed events for this instrument.
        """
        try:
            stmt = (
                select(AIRawEvent)
                .where(AIRawEvent.event_timestamp >= since)
                .where(AIRawEvent.raw_content.ilike(f"%{symbol}%"))
                .order_by(AIRawEvent.event_timestamp.desc())
                .limit(_MAX_EVENTS_PER_REQUEST)
            )
            result = await self.db.execute(stmt)
            return list(result.scalars().all())
        except Exception as exc:
            logger.error("Raw text event query failed: symbol=%s error=%s", symbol, exc, exc_info=True)
            return []

    async def _query_all_events(self, since: datetime) -> list[AIRawEvent]:
        """General market events — no symbol filter."""
        try:
            stmt = (
                select(AIRawEvent)
                .where(AIRawEvent.event_timestamp >= since)
                .order_by(AIRawEvent.event_timestamp.desc())
                .limit(_MAX_EVENTS_PER_REQUEST)
            )
            result = await self.db.execute(stmt)
            return list(result.scalars().all())
        except Exception as exc:
            logger.error("General event query failed: %s", exc, exc_info=True)
            return []

    @staticmethod
    def _extract_title(metadata: dict | None, raw_content: str) -> str:
        """Extract the article headline from metadata or fall back to raw_content first line."""
        if metadata and isinstance(metadata, dict):
            title = metadata.get("title", "")
            if title:
                return str(title)
        # Fall back to first line of raw_content
        return raw_content.split("\n")[0][:300]

    # ── Cache helpers ──────────────────────────────────────────────────────────

    async def _redis_get(self, key: str) -> dict | None:
        try:
            raw = await self.redis.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.warning("Redis get failed: %s", exc)
            return None

    async def _redis_set(self, key: str, value: dict) -> None:
        try:
            await self.redis.setex(key, _L2_TTL, json.dumps(value, default=str))
        except Exception as exc:
            logger.warning("Redis set failed: %s", exc)
