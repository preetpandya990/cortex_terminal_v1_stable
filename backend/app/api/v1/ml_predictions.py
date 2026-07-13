"""
Cortex AI — ML Prediction API Endpoints
"""
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_ml_predictor
from app.core.auth import require_admin_role
from app.core.database import AsyncSessionLocal
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.services.prediction_snapshot import get_prediction_snapshot
from app.schemas.ml_predictions import PredictionRequest, PredictionResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ML Predictions"])


@router.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate ensemble prediction for an NSE instrument",
)
@limiter.limit("100/minute")
async def predict(
    request: Request,
    body: PredictionRequest,
    current_user: dict = Depends(get_current_user),
    predictor=Depends(get_ml_predictor),
) -> PredictionResponse:
    user_id = current_user.get("user_id", "unknown")
    symbol = body.symbol
    timeframe = body.timeframe

    try:
        redis = await get_redis()
        snapshot = await get_prediction_snapshot(
            instrument_key=symbol,
            timeframe=timeframe,
            predictor=predictor,
            session_factory=AsyncSessionLocal,
            redis=redis,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Prediction failed: user=%s symbol=%s error=%s", user_id, symbol, exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "prediction_failed", "message": str(exc)},
        )

    logger.info(
        "Prediction: user=%s symbol=%s direction=%s confidence=%.4f",
        user_id,
        symbol,
        snapshot.get("direction"),
        float(snapshot.get("confidence", 0.0)),
    )

    if not snapshot.get("available"):
        return PredictionResponse(
            symbol=symbol,
            available=False,
            unavailable_reason=snapshot.get("unavailable_reason"),
        )

    probs = snapshot.get("probabilities", {})
    predicted_at_raw = snapshot.get("predicted_at")
    predicted_at = (
        datetime.fromisoformat(predicted_at_raw)
        if isinstance(predicted_at_raw, str) and predicted_at_raw
        else datetime.now(timezone.utc)
    )

    return PredictionResponse(
        symbol=symbol,
        available=True,
        direction=snapshot.get("direction"),
        confidence=float(snapshot.get("confidence", 0.0)),
        entry_price=float(snapshot.get("entry_price", 0.0)),
        stop_loss=float(snapshot.get("stop_loss", 0.0)),
        take_profit_1=float(snapshot.get("tp1", 0.0)),
        take_profit_2=float(snapshot.get("tp2", 0.0)),
        take_profit_3=float(snapshot.get("tp3", 0.0)),
        volatility=float(snapshot.get("volatility", 0.0)),
        probabilities={
            "up": float(probs.get("buy", 0.0)),
            "down": float(probs.get("sell", 0.0)),
            "hold": float(probs.get("hold", 0.0)),
        },
        model_version=snapshot.get("xgboost_version") or "ensemble_v1.0",
        predicted_at=predicted_at,
    )


@router.get(
    "/models",
    status_code=status.HTTP_200_OK,
    summary="List registered models and their lifecycle status",
)
async def list_models(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        from app.models.ml_data import MLModelMetadata
        from sqlalchemy import select

        rows = (await db.execute(
            select(MLModelMetadata).order_by(MLModelMetadata.created_at.desc())
        )).scalars().all()

        return {
            "models": [
                {
                    "model_id":      m.model_id,
                    "model_name":    m.model_name,
                    "model_version": m.model_version,
                    "status":        m.status,
                    "is_active":     m.is_active,
                    "accuracy":      (m.training_metrics or {}).get("accuracy"),
                    "trained_at":    m.trained_at.isoformat() if m.trained_at else None,
                    "deployed_at":   m.deployed_at.isoformat() if m.deployed_at else None,
                }
                for m in rows
            ],
            "count": len(rows),
        }
    except Exception as exc:
        logger.error("Failed to list models: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "list_models_failed", "message": str(exc)},
        )


@router.get(
    "/health",
    status_code=status.HTTP_200_OK,
    summary="ML inference health — no auth required",
)
async def ml_health(request: Request) -> dict[str, Any]:
    ensemble = getattr(request.app.state, "ml_ensemble", None)
    predictor = getattr(request.app.state, "ml_predictor", None)

    if ensemble is None or predictor is None:
        return {
            "status": "unavailable",
            "detail": "No production model loaded. Promote a model first.",
        }

    return {
        "status":           "healthy",
        "xgboost_version":  ensemble.xgboost_version,
        "gru_version":      ensemble.gru_version,
        "xgboost_weight":   ensemble.xgboost_weight,
        "gru_weight":       ensemble.gru_weight,
        "n_features":       ensemble.n_features,
        "sequence_length":  ensemble.sequence_length,
        "loaded_at":        ensemble.loaded_at.isoformat(),
    }


@router.post(
    "/admin/reload",
    status_code=status.HTTP_200_OK,
    summary="Hot-reload production models without server restart",
    include_in_schema=False,
)
async def admin_reload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user_id: str = Depends(require_admin_role),
) -> dict[str, Any]:
    from app.ml.inference.registry_loader import RegistryModelLoader
    from app.ml.inference.ensemble_predictor import EnsemblePredictor
    from app.core.redis import get_cache_service

    loader = RegistryModelLoader(session=db, num_threads=4)
    new_ensemble = await loader.load_production_ensemble(force_reload=True)

    # In-place swap: SignalScheduler (and any other long-lived holder) was
    # handed this exact object at startup and keeps its own reference, so
    # reassigning app.state.ml_predictor alone would leave those consumers
    # silently stuck on the old model. Mutate the existing instance instead.
    existing_predictor = getattr(request.app.state, "ml_predictor", None)
    if existing_predictor is not None:
        await existing_predictor.reload_from(new_ensemble)
    else:
        request.app.state.ml_predictor = EnsemblePredictor.from_loaded_ensemble(
            new_ensemble,
            cache=get_cache_service(),
        )

    request.app.state.ml_ensemble = new_ensemble

    logger.info(
        "Hot-reload complete: XGB=%s GRU=%s user=%s",
        new_ensemble.xgboost_version, new_ensemble.gru_version, admin_user_id,
    )

    return {
        "status":          "reloaded",
        "xgboost_version": new_ensemble.xgboost_version,
        "gru_version":     new_ensemble.gru_version,
        "loaded_at":       new_ensemble.loaded_at.isoformat(),
    }


# ── Prediction Card endpoint ───────────────────────────────────────────────────

@router.get(
    "/prediction-card",
    status_code=status.HTTP_200_OK,
    summary="Full ensemble prediction payload for the ML Analysis Card",
    description="""
    Returns the complete ensemble prediction in the ML Analysis Card format,
    including per-model breakdown (XGBoost / GRU), conviction scale, volatility
    regime, and price levels.

    When no production model is deployed or when there is insufficient price
    history, returns `{"available": false, "unavailable_reason": "..."}` with
    HTTP 200 so the frontend can render a graceful empty state rather than
    treating this as an error.

    Rate limit: 30 requests/minute per user (predictions are Redis-cached).
    """,
)
@limiter.limit("30/minute")
async def get_prediction_card(
    request: Request,
    instrument_key: str,
    timeframe: str = "1d",
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    user_id = current_user.get("user_id", "unknown")

    predictor = getattr(request.app.state, "ml_predictor", None)
    try:
        redis = get_redis()
        snapshot = await get_prediction_snapshot(
            instrument_key=instrument_key,
            timeframe=timeframe,
            predictor=predictor,
            session_factory=AsyncSessionLocal,
            redis=redis,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Prediction card failed: user=%s instrument=%s error=%s",
            user_id, instrument_key, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "prediction_failed", "message": "Ensemble prediction failed"},
        )

    if not snapshot.get("available"):
        logger.info(
            "Prediction card unavailable: user=%s instrument=%s reason=%s",
            user_id, instrument_key, snapshot.get("unavailable_reason"),
        )
        return snapshot

    logger.info(
        "Prediction card: user=%s instrument=%s direction=%s confidence=%.4f",
        user_id, instrument_key, snapshot.get("direction"), snapshot.get("confidence"),
    )
    return snapshot
