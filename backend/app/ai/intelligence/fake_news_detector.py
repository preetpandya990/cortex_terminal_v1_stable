"""
Fake News Detector - Four-Layer Detection with LLM

Detects fake news using:
1. Source credibility (25%)
2. Cross-reference verification (30%)
3. Sentiment consistency (20%)
4. LLM reasoning (25%)
"""
import logging
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
from app.ai.intelligence.llm_client import get_ollama_client

logger = logging.getLogger(__name__)


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
            source: Source URL or identifier
            
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
            db, content, classification.event_type
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
            source: Source identifier
            
        Returns:
            Credibility score (0-1, higher = more credible)
        """
        # Try to find source in credibility database
        stmt = select(AISourceCredibility).where(AISourceCredibility.source_name == source)
        result = await db.execute(stmt)
        credibility = result.scalar_one_or_none()

        if credibility:
            return float(credibility.credibility_score)
        
        # Default score for unknown sources
        return 0.5

    async def _check_cross_reference(
        self,
        db: AsyncSession,
        content: str,
        event_type: str,
    ) -> float:
        """
        Check if event is corroborated by other sources.
        
        Args:
            db: Database session
            content: Event content
            event_type: Event type
            
        Returns:
            Cross-reference score (0-1, higher = more corroborated)
        """
        # Look for similar events in last 24 hours
        cutoff_time = datetime.utcnow() - timedelta(hours=24)
        
        stmt = (
            select(AIEventClassification)
            .where(
                and_(
                    AIEventClassification.event_type == event_type,
                    AIEventClassification.created_at >= cutoff_time,
                )
            )
            .limit(10)
        )
        
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
        content_lower = content.lower()
        
        # Detect sentiment from content
        positive_words = ["surge", "soar", "gain", "up", "positive", "beat", "exceed"]
        negative_words = ["plunge", "crash", "fall", "down", "negative", "miss", "below"]
        
        positive_count = sum(1 for word in positive_words if word in content_lower)
        negative_count = sum(1 for word in negative_words if word in content_lower)
        
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

        try:
            prompt = f"""
Analyze this financial news for credibility:

Content: {content}
Source: {source}
Event Type: {classification.event_type}
Impact Score: {classification.impact_score}

Check for:
- Sensationalist language
- Unrealistic claims
- Missing key details
- Logical inconsistencies
- Source reliability indicators

Return JSON with:
- is_credible: boolean
- credibility_score: float 0-1 (1=highly credible, 0=fake)
- red_flags: list of concerns
- reasoning: explanation
"""

            result = await self.ollama_client.generate_json(
                prompt=prompt,
                system="You are an expert fact-checker analyzing financial news credibility.",
                temperature=0.3,
            )

            return float(result.get("credibility_score", 0.5))

        except Exception as e:
            logger.warning(f"LLM reasoning failed: {e}")
            return 0.5  # Neutral on failure
