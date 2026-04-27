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


# ── JSONB normalisers ──────────────────────────────────────────────────────────
# The DB stores contributing_events / ml_predictions / technical_indicators in
# compact internal formats. These helpers translate them to the typed shapes the
# frontend TradingSignal interface expects.

def _normalise_events(raw) -> list[dict]:
    if not raw or not isinstance(raw, list):
        return []
    result = []
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            continue
        result.append({
            "event_id": str(e.get("id", i)),
            "event_type": e.get("type", "unknown"),
            "impact_score": round(float(e.get("impact", 0)) / 100, 4),
            "summary": e.get("summary") or e.get("type") or "—",
        })
    return result


def _normalise_ml_predictions(raw) -> list[dict]:
    if not raw or not isinstance(raw, dict):
        return []
    confidence = float(raw.get("confidence", 0))
    score = float(raw.get("score", 0))
    model = raw.get("model") or "ensemble"
    if confidence == 0.0 and score == 0.0:
        return []
    return [{
        "model_id": str(model),
        "model_name": str(model).capitalize(),
        "prediction": "bullish" if score > 0.5 else "bearish",
        "confidence": confidence,
    }]


def _normalise_technical(raw) -> list[dict]:
    if not raw or not isinstance(raw, dict):
        return []
    indicators = raw.get("indicators")
    if not indicators or not isinstance(indicators, dict):
        return []
    return [
        {
            "indicator": k,
            "value": float(v) if isinstance(v, (int, float)) else 0.0,
            "signal": "neutral",
        }
        for k, v in indicators.items()
    ]


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
                    "events": _normalise_events(s.contributing_events),
                    "ml_predictions": _normalise_ml_predictions(s.ml_predictions),
                    "technical": _normalise_technical(s.technical_indicators),
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


@router.get("/signals/by-id/{signal_id}/audit")
async def get_signal_audit(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Return audit trail entries for a specific signal ID."""
    from sqlalchemy import select
    from app.ai.fusion.models import AITradingSignal

    try:
        signal_id_int = int(signal_id)
    except (ValueError, TypeError):
        return {"audit_log": [], "total": 0}

    stmt = select(AITradingSignal).where(AITradingSignal.id == signal_id_int)
    result = await db.execute(stmt)
    signal = result.scalar_one_or_none()

    if not signal:
        return {"audit_log": [], "total": 0}

    entry = {
        "audit_id": signal.id,
        "signal_id": str(signal.id),
        "symbol": signal.symbol,
        "signal_type": signal.action.lower(),
        "confidence": float(signal.confidence_score),
        "generated_at": signal.signal_timestamp.isoformat(),
        "outcome": None,
    }
    return {"audit_log": [entry], "total": 1}


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
