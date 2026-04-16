"""
Event Classifier - LLM-based Event Classification

Classifies events using Ollama LLM with GPT-4o and rule-based fallbacks.
"""
import logging
import re
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AINLPResult, AIEventClassification
from app.ai.intelligence.llm_client import get_ollama_client

logger = logging.getLogger(__name__)


class EventClassifier:
    """
    Classifies events with LLM and fallback chain.
    
    Classification chain:
    1. Ollama LLM (primary)
    2. GPT-4o (fallback if confidence < 0.7)
    3. Rule-based (final fallback)
    """

    def __init__(self, use_llm: bool = True):
        """
        Initialize classifier.
        
        Args:
            use_llm: Whether to use LLM (can disable for testing)
        """
        self.use_llm = use_llm
        self.ollama_client = get_ollama_client() if use_llm else None

    async def classify(
        self,
        db: AsyncSession,
        nlp_result_id: int,
        content: str,
        entities: dict,
    ) -> AIEventClassification:
        """
        Classify event type and score impact with fallback chain.
        
        Args:
            db: Database session
            nlp_result_id: NLP result ID
            content: Event content text
            entities: Extracted entities
            
        Returns:
            AIEventClassification record
        """
        # Try Ollama first
        result = await self._classify_with_ollama(content, entities)
        
        # Fallback to GPT-4o if confidence too low
        if result["confidence"] < 0.7:
            logger.info(f"Ollama confidence {result['confidence']} < 0.7, trying GPT-4o")
            gpt_result = await self._classify_with_gpt4o(content, entities)
            if gpt_result["confidence"] > result["confidence"]:
                result = gpt_result
        
        # Final fallback to rule-based
        if result["confidence"] < 0.5:
            logger.info(f"LLM confidence {result['confidence']} < 0.5, using rule-based")
            result = self._classify_rule_based(content, entities)

        classification = AIEventClassification(
            nlp_result_id=nlp_result_id,
            event_type=result["event_type"],
            impact_score=Decimal(str(result["impact_score"])),
            affected_symbols=result.get("affected_symbols", []),
            classification_confidence=Decimal(str(result["confidence"])),
            reasoning=result.get("reasoning", ""),
            decay_half_life_hours=result.get("decay_hours", 24),
        )

        db.add(classification)
        await db.commit()
        await db.refresh(classification)

        logger.info(
            f"Classified event {nlp_result_id}: {result['event_type']} "
            f"(impact: {result['impact_score']}, confidence: {result['confidence']})"
        )
        return classification

    async def _classify_with_ollama(
        self,
        content: str,
        entities: dict,
    ) -> dict:
        """
        Classify event using Ollama LLM.
        
        Args:
            content: Event content
            entities: Extracted entities
            
        Returns:
            Classification result dict
        """
        if not self.use_llm or not self.ollama_client:
            return self._classify_rule_based(content, entities)

        try:
            prompt = f"""
Classify this financial event:

Content: {content}

Entities: {entities}

Return JSON with:
- event_type: one of [earnings, fed_announcement, merger_acquisition, regulatory, geopolitical, market_data, company_news, sector_news, general]
- impact_score: float 0-100 (0=no impact, 100=extreme impact)
- sentiment: one of [bullish, bearish, neutral]
- confidence: float 0-1
- affected_symbols: list of stock symbols (if identifiable)
- reasoning: brief explanation
- decay_hours: event relevance half-life in hours (4-72)
"""

            result = await self.ollama_client.generate_json(
                prompt=prompt,
                system="You are an expert financial event classifier.",
                temperature=0.3,
            )

            return {
                "event_type": result.get("event_type", "general"),
                "impact_score": float(result.get("impact_score", 50.0)),
                "confidence": float(result.get("confidence", 0.0)),
                "affected_symbols": result.get("affected_symbols", []),
                "reasoning": result.get("reasoning", ""),
                "decay_hours": int(result.get("decay_hours", 24)),
            }

        except Exception as e:
            logger.warning(f"Ollama classification failed: {e}")
            return {"confidence": 0.0, "event_type": "general", "impact_score": 50.0}

    async def _classify_with_gpt4o(
        self,
        content: str,
        entities: dict,
    ) -> dict:
        """
        Classify event using GPT-4o as fallback.
        
        Args:
            content: Event content
            entities: Extracted entities
            
        Returns:
            Classification result dict
            
        Note:
            This is a placeholder for GPT-4o integration.
            In production, would use OpenAI API.
        """
        # Placeholder - would integrate OpenAI API here
        logger.info("GPT-4o fallback not implemented, using rule-based")
        return self._classify_rule_based(content, entities)

    def _classify_rule_based(
        self,
        content: str,
        entities: dict,
    ) -> dict:
        """
        Rule-based classification as final fallback.
        
        Args:
            content: Event content
            entities: Extracted entities
            
        Returns:
            Classification result dict
        """
        content_lower = content.lower()
        
        # Event type rules
        event_type = "general"
        impact_score = 50.0
        decay_hours = 24
        
        if any(word in content_lower for word in ["earnings", "revenue", "profit", "eps"]):
            event_type = "earnings"
            impact_score = 70.0
            decay_hours = 48
        elif any(word in content_lower for word in ["fed", "federal reserve", "interest rate", "fomc"]):
            event_type = "fed_announcement"
            impact_score = 85.0
            decay_hours = 72
        elif any(word in content_lower for word in ["merger", "acquisition", "takeover", "buyout"]):
            event_type = "merger_acquisition"
            impact_score = 80.0
            decay_hours = 48
        elif any(word in content_lower for word in ["sec", "regulatory", "compliance", "investigation"]):
            event_type = "regulatory"
            impact_score = 75.0
            decay_hours = 36
        elif any(word in content_lower for word in ["war", "conflict", "sanctions", "trade war"]):
            event_type = "geopolitical"
            impact_score = 90.0
            decay_hours = 72
        elif any(word in content_lower for word in ["gdp", "unemployment", "inflation", "cpi"]):
            event_type = "market_data"
            impact_score = 65.0
            decay_hours = 24
        
        # Sentiment rules
        sentiment = "neutral"
        if any(word in content_lower for word in ["surge", "soar", "rally", "gain", "up", "positive"]):
            sentiment = "bullish"
        elif any(word in content_lower for word in ["plunge", "crash", "fall", "down", "negative", "loss"]):
            sentiment = "bearish"
        
        # Extract symbols from entities
        affected_symbols = []
        if "companies" in entities:
            # Use company names as symbols
            affected_symbols = entities["companies"][:3]  # Limit to 3
        
        return {
            "event_type": event_type,
            "impact_score": impact_score,
            "confidence": 0.6,  # Rule-based has moderate confidence
            "affected_symbols": affected_symbols,
            "reasoning": f"Rule-based classification: detected keywords for {event_type}",
            "decay_hours": decay_hours,
        }
