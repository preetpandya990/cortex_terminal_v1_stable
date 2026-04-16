"""
Intelligence API - Event Classification and Analysis

Endpoints for NLP, event classification, and fake news detection.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.intelligence.event_classifier import EventClassifier
from app.ai.intelligence.fake_news_detector import FakeNewsDetector
from app.api.deps import get_db
from app.core.auth import get_current_user, require_role

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/events")
async def get_events(
    page: int = 1,
    limit: int = 10,
    event_type: str | None = None,
    symbol: str | None = None,
    min_impact: float | None = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """Get processed events with classifications and impact scores."""
    from sqlalchemy import select, desc, func, and_
    from app.ai.fusion.models import (
        AIRawEvent,
        AIProcessedEvent,
        AINLPResult,
        AIEventClassification,
    )
    
    try:
        # Build query joining all related tables
        stmt = (
            select(
                AIRawEvent.id.label("raw_id"),
                AIRawEvent.event_timestamp,
                AIRawEvent.source_name,
                AIRawEvent.raw_content,
                AIProcessedEvent.translated_content,
                AIEventClassification.event_type,
                AIEventClassification.impact_score,
                AIEventClassification.affected_symbols,
                AIEventClassification.reasoning,
                AINLPResult.sentiment_score,
                AINLPResult.sentiment_label,
            )
            .select_from(AIRawEvent)
            .join(AIProcessedEvent, AIProcessedEvent.raw_event_id == AIRawEvent.id)
            .join(AINLPResult, AINLPResult.processed_event_id == AIProcessedEvent.id)
            .join(AIEventClassification, AIEventClassification.nlp_result_id == AINLPResult.id)
            .order_by(desc(AIRawEvent.event_timestamp))
        )
        
        # Apply filters
        filters = []
        if event_type:
            filters.append(AIEventClassification.event_type == event_type)
        if symbol:
            filters.append(AIEventClassification.affected_symbols.contains([symbol]))
        if min_impact is not None:
            filters.append(AIEventClassification.impact_score >= min_impact)
        
        if filters:
            stmt = stmt.where(and_(*filters))
        
        # Get total count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = await db.scalar(count_stmt) or 0
        
        # Apply pagination
        offset = (page - 1) * limit
        stmt = stmt.offset(offset).limit(limit)
        
        result = await db.execute(stmt)
        rows = result.all()
        
        return {
            "events": [
                {
                    "id": row.raw_id,
                    "event_type": row.event_type,
                    "title": row.source_name,
                    "content": row.translated_content or row.raw_content[:500],
                    "timestamp": row.event_timestamp.isoformat(),
                    "impact_score": float(row.impact_score) if row.impact_score else 0.0,
                    "affected_symbols": row.affected_symbols or [],
                    "sentiment": row.sentiment_label,
                    "sentiment_score": float(row.sentiment_score) if row.sentiment_score else None,
                    "reasoning": row.reasoning,
                }
                for row in rows
            ],
            "total": total,
            "page": page,
            "limit": limit,
        }
    except Exception as e:
        logger.error(f"Failed to fetch events: {str(e)}")
        return {"events": [], "total": 0, "page": page, "limit": limit}


class ClassifyEventRequest(BaseModel):
    content: str
    entities: dict = {}


class DetectFakeNewsRequest(BaseModel):
    classification_id: int
    content: str
    source: str


@router.post("/classify")
async def classify_event(
    request: ClassifyEventRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """Classify an event."""
    classifier = EventClassifier(use_llm=True)
    
    try:
        # Create a placeholder NLP result
        from app.ai.fusion.models import AINLPResult
        nlp_result = AINLPResult(
            processed_event_id=0,
            model_used='rule_based',
            sentiment_score=0.0,
        )
        db.add(nlp_result)
        await db.commit()
        await db.refresh(nlp_result)
        
        classification = await classifier.classify(
            db=db,
            nlp_result_id=nlp_result.id,
            content=request.content,
            entities=request.entities,
        )
        
        return {
            "classification_id": classification.id,
            "event_type": classification.event_type,
            "impact_score": float(classification.impact_score),
            "confidence": float(classification.classification_confidence),
            "reasoning": classification.reasoning,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Classification failed: {str(e)}"
        )


@router.post("/fake-news/detect")
async def detect_fake_news(
    request: DetectFakeNewsRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """Detect fake news in an event."""
    detector = FakeNewsDetector(use_llm=True)
    
    try:
        flag = await detector.detect(
            db=db,
            classification_id=request.classification_id,
            content=request.content,
            source=request.source,
        )
        
        return {
            "flag_id": flag.id,
            "status": flag.flag_status,
            "reasoning": flag.reasoning,
            "detection_layers": flag.detection_layers,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fake news detection failed: {str(e)}"
        )
