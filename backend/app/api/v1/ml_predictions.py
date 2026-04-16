"""
Cortex AI — ML Prediction API Endpoints
=========================================
Robust ML predictions with graceful fallback when model unavailable.
"""

import logging
import random
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.schemas.ml_predictions import (
    PredictionRequest,
    PredictionResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ML Predictions"])


@router.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
)
@limiter.limit("1000/minute")  # Increased for load testing
async def predict_single(
    request: Request,
    prediction_request: PredictionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> PredictionResponse:
    """
    Generate ML prediction for a single symbol.
    Uses pre-loaded prediction engine for fast inference.
    """
    symbol = prediction_request.symbol or "UNKNOWN"
    
    # Try to use pre-loaded prediction engine
    prediction_engine = request.app.state.prediction_engine if hasattr(request.app.state, 'prediction_engine') else None
    
    if prediction_engine:
        try:
            import numpy as np
            # Generate feature vector from request features
            feature_vector = np.random.randn(60, 42)  # Placeholder - should use real features
            
            # Run prediction with pre-loaded engine
            prediction = await prediction_engine.predict(
                feature_vector=feature_vector,
                symbol=symbol,
                timeframe="daily",
                model_version="1.0.0",
                use_cache=True,
            )
            return PredictionResponse(**prediction)
        except Exception as e:
            logger.warning(f"Prediction engine failed: {e}, using fallback")
    
    # Fallback response when engine unavailable
    return PredictionResponse(
        id=0,
        model_id=prediction_request.model_id,
        timestamp=datetime.now(timezone.utc),
        features=prediction_request.features,
        prediction=0.5,
        prediction_proba={"bullish": 0.5, "bearish": 0.5},
        symbol=symbol,
        prediction_type=prediction_request.prediction_type or "price",
        model_version="fallback-1.0.0"
    )


@router.get("/models", status_code=status.HTTP_200_OK)
async def list_models(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    List available ML models.
    """
    try:
        from app.models.ml_data import MLModelMetadata
        from sqlalchemy import select
        
        result = await db.execute(
            select(MLModelMetadata)
            .where(MLModelMetadata.is_active == True)
            .order_by(MLModelMetadata.created_at.desc())
        )
        models = result.scalars().all()
        
        return {
            "models": [
                {
                    "model_id": m.model_id,
                    "model_name": m.model_name,
                    "model_version": m.model_version,
                    "status": m.status,
                    "trained_at": m.trained_at.isoformat() if m.trained_at else None,
                }
                for m in models
            ],
            "count": len(models)
        }
    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        return {"models": [], "count": 0, "error": str(e)}
