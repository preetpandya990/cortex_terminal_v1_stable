"""
CAI API - Cortex AI Dashboard

Unified endpoints for Cortex AI dashboard.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AITradingSignal, AIEventClassification, AIKillSwitch
from app.api.deps import get_db
from app.core.auth import get_current_user

router = APIRouter()


@router.get("/dashboard/stats")
async def get_dashboard_stats(
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get dashboard statistics."""
    try:
        # Count signals
        signal_count = await db.scalar(select(func.count()).select_from(AITradingSignal))
        
        # Count events
        event_count = await db.scalar(select(func.count()).select_from(AIEventClassification))
        
        # Check kill switch status
        kill_switch_stmt = (
            select(AIKillSwitch)
            .where(AIKillSwitch.status == "active")
            .order_by(desc(AIKillSwitch.activated_at))
            .limit(1)
        )
        result = await db.execute(kill_switch_stmt)
        kill_switch = result.scalar_one_or_none()
        
        return {
            "total_signals": signal_count or 0,
            "total_events": event_count or 0,
            "kill_switch_active": kill_switch is not None,
        }
    except Exception as e:
        return {
            "total_signals": 0,
            "total_events": 0,
            "kill_switch_active": False,
            "error": str(e),
        }


@router.get("/dashboard/recent-activity")
async def get_recent_activity(
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get recent trading activity."""
    try:
        # Get recent signals
        stmt = (
            select(AITradingSignal)
            .order_by(desc(AITradingSignal.signal_timestamp))
            .limit(limit)
        )
        
        result = await db.execute(stmt)
        signals = result.scalars().all()
        
        return [
            {
                "type": "signal",
                "symbol": s.symbol,
                "action": s.action,
                "confidence": float(s.confidence_score),
                "timestamp": s.signal_timestamp.isoformat(),
            }
            for s in signals
        ]
    except Exception as e:
        return []
