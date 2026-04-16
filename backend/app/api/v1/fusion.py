"""
Fusion API - Trading Signal Generation

Endpoints for signal assembly and fusion.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.signal_assembler import SignalAssembler
from app.api.deps import get_db
from app.core.auth import get_current_user, require_role
from app.core.limiter import limiter
from app.core.redis import PubSubClient

router = APIRouter()


@router.post("/signals/generate/{symbol}")
@limiter.limit("10000/minute")  # Increased for load testing
async def generate_signal(
    request: Request,
    symbol: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """Generate trading signal for a symbol."""
    assembler = SignalAssembler()
    pubsub = PubSubClient()
    
    try:
        signal = await assembler.assemble_signal(db, pubsub, symbol)
        return {
            "signal_id": signal.id,
            "symbol": signal.symbol,
            "action": signal.action,
            "confidence": float(signal.confidence_score),
            "timestamp": signal.signal_timestamp.isoformat(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signal generation failed: {str(e)}"
        )


@router.get("/signals")
async def get_all_signals(
    page: int = 1,
    limit: int = 20,
    symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get recent signals with pagination."""
    from sqlalchemy import select, desc, func
    from app.ai.fusion.models import AITradingSignal
    
    stmt = select(AITradingSignal).order_by(desc(AITradingSignal.signal_timestamp))
    
    if symbol:
        stmt = stmt.where(AITradingSignal.symbol == symbol)
    
    # Get total count
    count_stmt = select(func.count()).select_from(AITradingSignal)
    if symbol:
        count_stmt = count_stmt.where(AITradingSignal.symbol == symbol)
    total = await db.scalar(count_stmt) or 0
    
    # Apply pagination
    offset = (page - 1) * limit
    stmt = stmt.offset(offset).limit(limit)
    
    result = await db.execute(stmt)
    signals = result.scalars().all()
    
    return {
        "signals": [
            {
                "signal_id": str(s.id),
                "symbol": s.symbol,
                "signal_type": s.action.lower(),  # Map action to signal_type
                "confidence": float(s.confidence_score),
                "calibrated_confidence": float(s.confidence_score),
                "time_horizon": "intraday",  # Default, should be in DB
                "reasoning": s.reasoning or "",
                "contributing_factors": {
                    "events": s.contributing_events or [],
                    "ml_predictions": s.ml_predictions or [],
                    "technical": s.technical_indicators or [],
                },
                "regime_type": s.regime_type,
                "generated_at": s.signal_timestamp.isoformat(),
                "expires_at": s.signal_timestamp.isoformat(),  # Should calculate expiry
            }
            for s in signals
        ],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/signals/{symbol}")
async def get_signals(
    symbol: str,
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get recent signals for a symbol."""
    from sqlalchemy import select, desc
    from app.ai.fusion.models import AITradingSignal
    
    stmt = (
        select(AITradingSignal)
        .where(AITradingSignal.symbol == symbol)
        .order_by(desc(AITradingSignal.signal_timestamp))
        .limit(limit)
    )
    
    result = await db.execute(stmt)
    signals = result.scalars().all()
    
    return [
        {
            "signal_id": s.id,
            "symbol": s.symbol,
            "action": s.action,
            "confidence": float(s.confidence_score),
            "timestamp": s.signal_timestamp.isoformat(),
        }
        for s in signals
    ]
