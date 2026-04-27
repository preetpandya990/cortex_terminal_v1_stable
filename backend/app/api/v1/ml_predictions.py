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
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.ml.inference.feature_loader import FeatureLoader
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
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    predictor=Depends(get_ml_predictor),
) -> PredictionResponse:
    user_id = current_user.get("user_id", "unknown")
    symbol = body.symbol
    timeframe = body.timeframe

    try:
        redis = await get_redis()
        feature_loader = FeatureLoader(db=db, redis=redis, sequence_length=60, n_features=47)

        try:
            tabular, sequence, current_price, volatility = await feature_loader.load_features(
                symbol=symbol,
                timeframe=timeframe,
            )
        except ValueError as exc:
            # No OHLCV history in the local DB yet — not an error, just no data.
            # Return 200 with available=False so the frontend can display a
            # meaningful message instead of treating this as an HTTP failure.
            logger.info("No features available for %s: %s", symbol, exc)
            return PredictionResponse(
                symbol=symbol,
                available=False,
                unavailable_reason="insufficient_data",
            )

        prediction = await predictor.predict(
            features_tabular=tabular,
            features_sequence=sequence,
            symbol=symbol,
            current_price=current_price,
            volatility=volatility,
            timeframe=timeframe,
            use_cache=True,
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
        user_id, symbol, prediction["direction_label"], prediction["confidence"],
    )

    probs = prediction.get("probabilities", {})
    return PredictionResponse(
        symbol=symbol,
        direction=prediction["direction_label"],
        confidence=float(prediction["confidence"]),
        entry_price=float(prediction["entry_price"]),
        stop_loss=float(prediction["stop_loss"]),
        take_profit_1=float(prediction["tp1"]),
        take_profit_2=float(prediction["tp2"]),
        take_profit_3=float(prediction["tp3"]),
        volatility=float(prediction["volatility"]),
        probabilities={
            "up":   float(probs.get("buy", 0.0)),
            "down": float(probs.get("sell", 0.0)),
            "hold": float(probs.get("hold", 0.0)),
        },
        model_version=prediction.get("metadata", {}).get("model_version", "ensemble_v1.0"),
        predicted_at=datetime.now(timezone.utc),
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
    new_predictor = EnsemblePredictor.from_loaded_ensemble(
        new_ensemble,
        cache=get_cache_service(),
    )

    request.app.state.ml_ensemble = new_ensemble
    request.app.state.ml_predictor = new_predictor

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
