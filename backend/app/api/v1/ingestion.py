"""
Ingestion API - RSS Feed and Event Management

Endpoints for RSS feed management and event ingestion.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIRawEvent, AIProcessedEvent
from app.api.deps import get_db
from app.core.auth import get_current_user, require_role

router = APIRouter()


@router.get("/events/raw")
async def get_raw_events(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get recent raw events."""
    try:
        stmt = (
            select(AIRawEvent)
            .order_by(desc(AIRawEvent.event_timestamp))
            .limit(limit)
        )
        
        result = await db.execute(stmt)
        events = result.scalars().all()
        
        return [
            {
                "event_id": e.id,
                "source_type": e.source_type,
                "source_name": e.source_name,
                "source_url": e.source_url,
                "content_preview": e.raw_content[:200] + "..." if len(e.raw_content) > 200 else e.raw_content,
                "timestamp": e.event_timestamp.isoformat(),
            }
            for e in events
        ]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch events: {str(e)}"
        )


@router.get("/events/processed")
async def get_processed_events(
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get recent processed events."""
    try:
        stmt = (
            select(AIProcessedEvent)
            .order_by(desc(AIProcessedEvent.processed_timestamp))
            .limit(limit)
        )
        
        result = await db.execute(stmt)
        events = result.scalars().all()
        
        return [
            {
                "event_id": e.id,
                "raw_event_id": e.raw_event_id,
                "processing_status": e.processing_status,
                "timestamp": e.processed_timestamp.isoformat(),
            }
            for e in events
        ]
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch processed events: {str(e)}"
        )
