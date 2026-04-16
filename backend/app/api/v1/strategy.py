"""
Strategy API - Regime Detection and Strategy Management

Endpoints for market regime detection and strategy orchestration.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIRegimeDetection
from app.api.deps import get_db
from app.core.auth import get_current_user

router = APIRouter()


@router.get("/regime")
async def get_current_regime(
    symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get current market regime."""
    try:
        stmt = (
            select(AIRegimeDetection)
            .order_by(desc(AIRegimeDetection.detection_timestamp))
            .limit(1)
        )
        
        result = await db.execute(stmt)
        regime = result.scalar_one_or_none()
        
        if not regime:
            # Return mock data for development
            import random
            from datetime import datetime, timezone
            
            regime_types = ["bull_trending", "bear_trending", "sideways_range", "high_volatility"]
            mock_regime = random.choice(regime_types)
            
            return {
                "regime_type": mock_regime,
                "confidence": round(random.uniform(0.75, 0.92), 2),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "indicators": {
                    "adx": round(random.uniform(25, 45), 2),
                    "atr": round(random.uniform(1.2, 2.8), 2),
                    "bollinger_width": round(random.uniform(0.03, 0.07), 4),
                    "rsi": round(random.uniform(35, 65), 2),
                },
                "regime_duration_minutes": random.randint(30, 240),
                "previous_regime": random.choice(["bull_trending", "sideways_range", None]),
            }
        
        return {
            "regime_type": regime.regime_type,
            "confidence": float(regime.confidence_score),
            "timestamp": regime.detection_timestamp.isoformat(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch regime: {str(e)}"
        )


@router.get("/regime/latest")
async def get_latest_regime(
    symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get latest detected market regime."""
    try:
        stmt = (
            select(AIRegimeDetection)
            .order_by(desc(AIRegimeDetection.detection_timestamp))
            .limit(1)
        )
        
        result = await db.execute(stmt)
        regime = result.scalar_one_or_none()
        
        if not regime:
            return {"regime_type": None, "message": "No regime detected yet"}
        
        return {
            "regime_type": regime.regime_type,
            "confidence": float(regime.confidence_score),
            "timestamp": regime.detection_timestamp.isoformat(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch regime: {str(e)}"
        )


@router.get("/regime/history")
async def get_regime_history(
    symbol: str | None = None,
    days: int = 1,
    limit: int = 20,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get regime detection history."""
    try:
        stmt = (
            select(AIRegimeDetection)
            .order_by(desc(AIRegimeDetection.detection_timestamp))
            .limit(limit)
        )
        
        result = await db.execute(stmt)
        regimes = result.scalars().all()
        
        if not regimes:
            # Return mock history for development
            import random
            from datetime import datetime, timezone, timedelta
            
            regime_types = ["bull_trending", "bear_trending", "sideways_range", "high_volatility"]
            mock_history = []
            now = datetime.now(timezone.utc)
            
            for i in range(min(10, limit)):
                hours_ago = i * 2
                mock_history.append({
                    "regime_type": random.choice(regime_types),
                    "confidence": round(random.uniform(0.70, 0.90), 2),
                    "detected_at": (now - timedelta(hours=hours_ago)).isoformat(),
                    "duration_minutes": random.randint(60, 180),
                })
            
            return {
                "history": mock_history,
                "total": len(mock_history),
            }
        
        return {
            "history": [
                {
                    "regime_type": r.regime_type,
                    "confidence": float(r.confidence_score),
                    "timestamp": r.detection_timestamp.isoformat(),
                }
                for r in regimes
            ],
            "total": len(regimes),
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch regime history: {str(e)}"
        )


@router.get("/strategies")
async def get_active_strategies(
    symbol: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Get active trading strategies."""
    from app.ai.fusion.models import AIActiveStrategy
    
    try:
        stmt = select(AIActiveStrategy).where(AIActiveStrategy.is_active == True)
        result = await db.execute(stmt)
        strategies = result.scalars().all()
        
        return {
            "strategies": [
                {
                    "id": s.id,
                    "strategy_name": s.strategy_name,
                    "regime_type": s.regime_type,
                    "is_active": s.is_active,
                }
                for s in strategies
            ],
            "total": len(strategies),
        }
    except Exception as e:
        return {"strategies": [], "total": 0}
