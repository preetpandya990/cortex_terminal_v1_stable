"""
Fusion API — Trading Signal Generation and Retrieval

Endpoints:
  POST /signals/generate/{symbol}  — on-demand signal generation
  GET  /signals                    — paginated signal list with full filtering
  GET  /signals/by-id/{id}/audit   — per-signal audit trail
  GET  /signals/{symbol}           — recent signals for a symbol (legacy)
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import and_, desc, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.serializers import serialise_signal
from app.ai.fusion.signal_assembler import SignalAssembler
from app.api.deps import get_db
from app.core.auth import get_current_user, require_role
from app.core.limiter import limiter
from app.core.redis import get_pubsub_client, PubSubClient

router = APIRouter()


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.post("/signals/generate/{symbol}")
@limiter.limit("60/minute")
async def generate_signal(
    request: Request,
    symbol: str,
    db: AsyncSession = Depends(get_db),
    pubsub: PubSubClient = Depends(get_pubsub_client),
    current_user=Depends(require_role("trader")),
):
    """On-demand signal generation for a symbol."""
    from app.core.redis import get_redis
    from app.ml.inference.feature_loader import FeatureLoader

    ml_predictor = getattr(request.app.state, "ml_predictor", None)

    feature_loader = (
        FeatureLoader(db=db, redis=get_redis()) if ml_predictor else None
    )

    assembler = SignalAssembler(
        ensemble_predictor=ml_predictor,
        feature_loader=feature_loader,
    )

    try:
        signal = await assembler.assemble_signal(db, pubsub, symbol)
        return serialise_signal(signal)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Signal generation failed: {exc}",
        ) from exc


@router.get("/signals")
async def get_all_signals(
    page: int = 1,
    limit: int = 20,
    symbol: Optional[str] = None,
    signal_type: Optional[str] = None,
    time_horizon: Optional[str] = None,
    min_confidence: Optional[float] = None,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Paginated signal list with filtering.

    Default window: active signals + signals that expired within the last 24 hours.
    This matches professional terminal behaviour — same-day context is always visible.

    Filters:
      symbol         — exact NSE trading symbol (case-sensitive)
      signal_type    — buy / sell / hold
      time_horizon   — intraday / swing / positional
      min_confidence — 0.0–1.0 inclusive threshold on calibrated_confidence
    """
    from app.ai.fusion.models import AITradingSignal

    predicates = [
        or_(
            AITradingSignal.expires_at.is_(None),
            AITradingSignal.expires_at > text("NOW() - INTERVAL '24 hours'"),
        )
    ]

    if symbol:
        predicates.append(AITradingSignal.symbol == symbol)
    if signal_type:
        predicates.append(AITradingSignal.action == signal_type.upper())
    if time_horizon:
        predicates.append(AITradingSignal.time_horizon == time_horizon)
    if min_confidence is not None:
        predicates.append(AITradingSignal.confidence_score >= min_confidence)

    where_clause = and_(*predicates)

    total = await db.scalar(
        select(func.count()).select_from(AITradingSignal).where(where_clause)
    ) or 0

    offset = (page - 1) * limit
    rows = (
        await db.execute(
            select(AITradingSignal)
            .where(where_clause)
            .order_by(desc(AITradingSignal.signal_timestamp))
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()

    return {
        "signals": [serialise_signal(s) for s in rows],
        "total": total,
        "page": page,
        "limit": limit,
    }


@router.get("/signals/by-id/{signal_id}/audit")
async def get_signal_audit(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return the audit trail entry for a specific signal ID."""
    from app.ai.fusion.models import AITradingSignal

    try:
        signal_id_int = int(signal_id)
    except (ValueError, TypeError):
        return {"audit_log": [], "total": 0}

    signal = (
        await db.execute(
            select(AITradingSignal).where(AITradingSignal.id == signal_id_int)
        )
    ).scalar_one_or_none()

    if not signal:
        return {"audit_log": [], "total": 0}

    return {
        "audit_log": [
            {
                "audit_id": signal.id,
                "signal_id": str(signal.id),
                "symbol": signal.symbol,
                "signal_type": signal.action.lower(),
                "confidence": float(signal.confidence_score),
                "generated_at": signal.signal_timestamp.isoformat(),
                "outcome": None,
            }
        ],
        "total": 1,
    }


@router.get("/signals/{symbol}")
async def get_signals(
    symbol: str,
    limit: int = 10,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Recent signals for a symbol (lightweight — no pagination or filters)."""
    from app.ai.fusion.models import AITradingSignal

    rows = (
        await db.execute(
            select(AITradingSignal)
            .where(AITradingSignal.symbol == symbol)
            .order_by(desc(AITradingSignal.signal_timestamp))
            .limit(limit)
        )
    ).scalars().all()

    return [serialise_signal(s) for s in rows]
