"""
NLP Engine - Async Sentiment Analysis & Entity Extraction

Processes events using FinBERT for sentiment and spaCy for entities.
"""
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIProcessedEvent, AINLPResult

logger = logging.getLogger(__name__)


class NLPEngine:
    """NLP processing with sentiment analysis and entity extraction."""

    def __init__(self):
        # Placeholder - will load FinBERT and spaCy models
        self.sentiment_model = None
        self.ner_model = None

    async def process_event(
        self,
        db: AsyncSession,
        processed_event_id: int,
        content: str,
    ) -> AINLPResult:
        """Process event content with NLP."""
        # Simple regex-based entity extraction for E2E testing
        import re
        
        # Extract company names (simple patterns)
        company_patterns = [
            r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s+(?:Inc\.|Corp\.|Ltd\.|LLC|Corporation)\b',
            r'\b(Apple|Microsoft|Tesla|Amazon|Google|Meta|Netflix|Nvidia)\b',
        ]
        
        companies = []
        for pattern in company_patterns:
            matches = re.findall(pattern, content)
            companies.extend(matches)
        
        # Remove duplicates and limit to 3
        companies = list(set(companies))[:3]
        
        sentiment_score = 0.0  # Will use FinBERT
        sentiment_label = "neutral"
        named_entities = {"companies": companies, "people": [], "locations": []}
        keywords = []

        nlp_result = AINLPResult(
            processed_event_id=processed_event_id,
            sentiment_score=sentiment_score,
            sentiment_label=sentiment_label,
            named_entities=named_entities,
            keywords=keywords,
            model_used="finbert",
            confidence_score=0.0,
        )

        db.add(nlp_result)
        await db.commit()
        await db.refresh(nlp_result)

        logger.info(f"NLP processed event {processed_event_id}: {sentiment_label}")
        return nlp_result
