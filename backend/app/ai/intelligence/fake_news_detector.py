"""
Fake News Detector - Four-Layer Detection with LLM

Detects fake news using:
1. Source credibility (25%)
2. Cross-reference verification (30%)
3. Sentiment consistency (20%)
4. LLM reasoning (25%)
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AIEventClassification,
    AIFakeNewsFlag,
    AISourceCredibility,
    AIRawEvent,
)
from app.ai.intelligence.credibility_scorer import CredibilityScorer
from app.ai.intelligence.llm_client import get_ollama_client

logger = logging.getLogger(__name__)

# LLM credibility score cache — keyed by SHA-256 of article URL (preferred)
# or content[:500] (fallback when URL is absent/non-unique).
# Article credibility is static within a trading session; 1-hour TTL eliminates
# redundant LLM calls when the same article appears across multiple RSS feeds.
_FAKENEWS_CACHE_TTL_SECS: int = 3600

# Sentiment cue words as explicit word forms with \b anchors — raw substring
# matching produced systematic false positives ("up" inside "group"/"support",
# "down" inside "showdown", "miss" inside "commissioner"). Explicit forms
# instead of stem+\w* so e.g. "mission" can never match "miss".
_POSITIVE_CUES_RE = re.compile(
    r"\b(?:surges?|surged|soars?|soared|gains?|gained|up|positive|beats?|exceed(?:s|ed)?)\b",
    re.IGNORECASE,
)
_NEGATIVE_CUES_RE = re.compile(
    r"\b(?:plunges?|plunged|crash(?:es|ed)?|falls?|fell|fallen|down|negative|miss(?:es|ed)?|below)\b",
    re.IGNORECASE,
)


class FakeNewsDetector:
    """
    Four-layer fake news detection with LLM reasoning.
    
    Layers:
    1. Source credibility (25%): Check source reputation
    2. Cross-reference (30%): Verify with other sources
    3. Sentiment consistency (20%): Check sentiment alignment
    4. LLM reasoning (25%): Deep credibility analysis
    
    Final score: weighted average (0-1, lower = more suspicious)
    """

    def __init__(self, use_llm: bool = True):
        """
        Initialize detector.
        
        Args:
            use_llm: Whether to use LLM for reasoning layer
        """
        self.use_llm = use_llm
        self.ollama_client = get_ollama_client() if use_llm else None
        
        # Weights for each layer
        self.weights = {
            "source_credibility": 0.25,
            "cross_reference": 0.30,
            "sentiment_consistency": 0.20,
            "llm_reasoning": 0.25,
        }

    async def detect(
        self,
        db: AsyncSession,
        classification_id: int,
        content: str,
        source: str,
    ) -> AIFakeNewsFlag:
        """
        Detect fake news using four-layer analysis.
        
        Args:
            db: Database session
            classification_id: Event classification ID
            content: Event content text
            source: Feed display name (canonical source-identity key, e.g.
                    "Economic Times Markets") — used for both the credibility
                    lookup and the outcome feedback. URLs will not match.

        Returns:
            AIFakeNewsFlag record
        """
        # Get classification
        stmt = select(AIEventClassification).where(AIEventClassification.id == classification_id)
        result = await db.execute(stmt)
        classification = result.scalar_one_or_none()

        if not classification:
            raise ValueError(f"Classification {classification_id} not found")

        # Run four-layer detection
        layer_scores = {}
        
        # Layer 1: Source credibility
        layer_scores["source_credibility"] = await self._check_source_credibility(
            db, source
        )
        
        # Layer 2: Cross-reference verification
        layer_scores["cross_reference"] = await self._check_cross_reference(
            db, classification
        )
        
        # Layer 3: Sentiment consistency
        layer_scores["sentiment_consistency"] = self._check_sentiment_consistency(
            content, classification
        )
        
        # Layer 4: LLM reasoning
        layer_scores["llm_reasoning"] = await self._llm_reasoning(
            content, source, classification
        )

        # Calculate weighted final score
        final_score = sum(
            layer_scores[layer] * self.weights[layer]
            for layer in self.weights
        )

        # Determine flag status
        if final_score < 0.4:
            flag_status = "flagged"
            reasoning = "High suspicion of fake news (score < 0.4)"
        elif final_score < 0.6:
            flag_status = "suspected"
            reasoning = "Moderate suspicion of fake news (score < 0.6)"
        else:
            flag_status = "cleared"
            reasoning = "No significant fake news indicators (score >= 0.6)"

        # Create detailed reasoning
        detailed_reasoning = (
            f"{reasoning}\n"
            f"Layer scores: "
            f"source={layer_scores['source_credibility']:.2f}, "
            f"cross_ref={layer_scores['cross_reference']:.2f}, "
            f"sentiment={layer_scores['sentiment_consistency']:.2f}, "
            f"llm={layer_scores['llm_reasoning']:.2f}"
        )

        flag = AIFakeNewsFlag(
            event_classification_id=classification_id,
            flag_status=flag_status,
            detection_layers=layer_scores,
            reasoning=detailed_reasoning,
            flagged_at=datetime.utcnow(),
        )

        db.add(flag)
        await db.commit()
        await db.refresh(flag)

        # Feed the outcome back into source credibility so future
        # _check_source_credibility lookups read earned scores. Decisive
        # outcomes only — "suspected" is ambiguous and counts neither way.
        # Unknown sources are skipped inside update_credibility (sources are
        # registered at ingest, keyed by feed display name).
        if flag_status != "suspected":
            try:
                await CredibilityScorer().update_credibility(
                    db, source, confirmed=(flag_status == "cleared")
                )
            except Exception as exc:
                logger.warning(
                    "Credibility feedback for %r failed (non-fatal): %s", source, exc
                )

        logger.info(
            f"Fake news detection for classification {classification_id}: "
            f"{flag_status} (score: {final_score:.2f})"
        )
        return flag

    async def _check_source_credibility(
        self,
        db: AsyncSession,
        source: str,
    ) -> float:
        """
        Check source credibility from database.

        Args:
            db: Database session
            source: Feed display name (the canonical source-identity key,
                    matching ai_source_credibility.source_name — e.g.
                    "Economic Times Markets"). URLs will not match.

        Returns:
            Credibility score (0-1, higher = more credible)

        The stored score is 0-100 (see credibility_scorer.py); it is rescaled
        here because this layer feeds a 0-1 weighted sum — an unscaled value
        would dominate final_score ~50x.
        """
        stmt = select(AISourceCredibility).where(AISourceCredibility.source_name == source)
        result = await db.execute(stmt)
        credibility = result.scalar_one_or_none()

        if credibility:
            return min(max(float(credibility.credibility_score) / 100.0, 0.0), 1.0)

        # Default score for unknown sources
        return 0.5

    async def _check_cross_reference(
        self,
        db: AsyncSession,
        classification: AIEventClassification,
    ) -> float:
        """
        Check if event is corroborated by other sources.

        Corroboration requires the same event_type within 24h AND an
        affected_symbols overlap — event_type alone let a fabricated earnings
        story about company X score 0.9 purely because 3+ *other* companies
        published genuine earnings news in the same window (guaranteed during
        earnings season, this layer's 30% weight is the highest of the four).
        The classification under test is excluded from its own corroboration.

        Symbol-less classifications (market-wide events: fed announcements,
        geopolitical) fall back to event_type-only matching — for those, other
        reports of the same event type genuinely are corroboration.

        Returns:
            Cross-reference score (0-1, higher = more corroborated)
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=24)

        conditions = [
            AIEventClassification.event_type == classification.event_type,
            AIEventClassification.created_at >= cutoff_time,
            AIEventClassification.id != classification.id,
        ]
        if classification.affected_symbols:
            # Postgres array overlap (&&): at least one symbol in common.
            conditions.append(
                AIEventClassification.affected_symbols.op("&&")(
                    classification.affected_symbols
                )
            )

        stmt = select(AIEventClassification).where(and_(*conditions)).limit(10)
        result = await db.execute(stmt)
        similar_events = result.scalars().all()

        # Score based on number of similar events
        if len(similar_events) >= 3:
            return 0.9  # Well corroborated
        elif len(similar_events) >= 2:
            return 0.7  # Moderately corroborated
        elif len(similar_events) >= 1:
            return 0.5  # Minimally corroborated
        else:
            return 0.3  # Not corroborated

    def _check_sentiment_consistency(
        self,
        content: str,
        classification: AIEventClassification,
    ) -> float:
        """
        Check if sentiment is consistent with event type.
        
        Args:
            content: Event content
            classification: Event classification
            
        Returns:
            Consistency score (0-1, higher = more consistent)
        """
        # Word-boundary-anchored cue matching (see _POSITIVE_CUES_RE /
        # _NEGATIVE_CUES_RE) — raw substring checks flipped this layer on
        # unrelated tokens ("XYZ Group" counted as "up").
        positive_count = len(_POSITIVE_CUES_RE.findall(content))
        negative_count = len(_NEGATIVE_CUES_RE.findall(content))
        
        # Determine detected sentiment
        if positive_count > negative_count:
            detected_sentiment = "bullish"
        elif negative_count > positive_count:
            detected_sentiment = "bearish"
        else:
            detected_sentiment = "neutral"
        
        # Check consistency with event type
        # High-impact negative events should have bearish sentiment
        if classification.event_type in ["regulatory", "geopolitical"]:
            if detected_sentiment == "bearish":
                return 0.9
            elif detected_sentiment == "neutral":
                return 0.6
            else:
                return 0.3  # Inconsistent
        
        # Earnings/M&A can be either way
        elif classification.event_type in ["earnings", "merger_acquisition"]:
            return 0.7  # Neutral consistency
        
        # Default: moderate consistency
        return 0.6

    async def _llm_reasoning(
        self,
        content: str,
        source: str,
        classification: AIEventClassification,
    ) -> float:
        """
        Use LLM to analyze credibility with deep reasoning.
        
        Args:
            content: Event content
            source: Source identifier
            classification: Event classification
            
        Returns:
            LLM credibility score (0-1, higher = more credible)
        """
        if not self.use_llm or not self.ollama_client:
            return 0.5  # Neutral if LLM disabled

        # ── Cache check ────────────────────────────────────────────────────────
        # Prefer URL as the cache key (stable, compact); fall back to content
        # fingerprint when the source is a non-URL identifier or empty.
        from app.core.redis import get_cache_service
        cache_input = source if (source and source.startswith("http")) else content[:500]
        cache_hash = hashlib.sha256(cache_input.encode()).hexdigest()
        cache_key = f"cortex:fakenews:{cache_hash}"
        try:
            cached = await get_cache_service().get(cache_key)
            if cached is not None and isinstance(cached, dict):
                score = float(cached.get("credibility_score", 0.5))
                logger.debug(
                    "fake_news_detector: cache hit hash=%.12s score=%.2f",
                    cache_hash, score,
                )
                return score
        except Exception as _cache_exc:
            logger.debug(
                "fake_news_detector: cache read failed (non-fatal): %s", _cache_exc
            )

        try:
            prompt = (
                f"Analyze this financial news for credibility:\n\n"
                f"Content: {content}\n"
                f"Source: {source}\n"
                f"Event Type: {classification.event_type}\n"
                f"Impact Score: {classification.impact_score}\n\n"
                f"Check for:\n"
                f"- Sensationalist language\n"
                f"- Unrealistic claims\n"
                f"- Missing key details\n"
                f"- Logical inconsistencies\n"
                f"- Source reliability indicators\n\n"
                f"Return JSON with:\n"
                f"- is_credible: boolean\n"
                f"- credibility_score: float 0-1 (1=highly credible, 0=fake)\n"
                f"- red_flags: list of concerns\n"
                f"- reasoning: explanation"
            )

            result = await self.ollama_client.generate_json(
                prompt=prompt,
                system="You are an expert fact-checker analyzing financial news credibility.",
                temperature=0.3,
            )

            score = float(result.get("credibility_score", 0.5))

            # ── Cache write ────────────────────────────────────────────────────
            try:
                await get_cache_service().set(
                    cache_key,
                    {"credibility_score": score},
                    ttl=_FAKENEWS_CACHE_TTL_SECS,
                )
                logger.debug(
                    "fake_news_detector: cached hash=%.12s score=%.2f ttl=%ds",
                    cache_hash, score, _FAKENEWS_CACHE_TTL_SECS,
                )
            except Exception as _cache_exc:
                logger.debug(
                    "fake_news_detector: cache write failed (non-fatal): %s", _cache_exc
                )

            return score

        except Exception as exc:
            logger.warning("LLM reasoning failed: %s", exc)
            return 0.5  # Neutral on failure
