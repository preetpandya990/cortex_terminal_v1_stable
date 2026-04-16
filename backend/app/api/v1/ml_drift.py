"""
Cortex AI — ML Drift API Endpoints
====================================
FastAPI endpoints for drift detection and monitoring.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user_id
from app.ml.monitoring.drift_detector import DriftDetector
from app.models.ml_data import MLDriftMetric, MLModelMetadata
from app.schemas.ml_predictions import DriftMetricResponse, DriftDetectionRequest


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ml/drift", tags=["ML Drift Detection"])


@router.get(
    "/metrics",
    response_model=List[DriftMetricResponse],
    summary="Get drift metrics",
)
async def get_drift_metrics(
    model_id: Optional[str] = Query(None, description="Filter by model ID"),
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
    drift_only: bool = Query(False, description="Only return metrics with drift detected"),
    db: AsyncSession = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """
    Retrieve drift detection metrics.
    
    Returns historical drift metrics for monitoring and analysis.
    """
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    query = select(MLDriftMetric).where(MLDriftMetric.timestamp >= cutoff_date)
    
    if model_id:
        query = query.where(MLDriftMetric.model_id == model_id)
    
    if drift_only:
        query = query.where(MLDriftMetric.drift_detected == True)
    
    query = query.order_by(MLDriftMetric.timestamp.desc()).limit(1000)
    
    result = await db.execute(query)
    metrics = list(result.scalars().all())
    
    return [
        DriftMetricResponse(
            id=m.id,
            model_id=m.model_id,
            timestamp=m.timestamp,
            drift_detected=m.drift_detected,
            drift_type=m.drift_type,
            feature_drift_scores=m.feature_drift_scores or {},
            drifted_features=m.drifted_features or [],
            prediction_drift_score=m.prediction_drift_score,
            prediction_z_score=m.prediction_z_score,
            prediction_ks_statistic=m.prediction_ks_statistic,
            prediction_p_value=m.prediction_p_value,
            current_mean=m.current_mean,
            current_std=m.current_std,
            historical_mean=m.historical_mean,
            historical_std=m.historical_std,
            alerts=m.alerts or [],
            alert_sent=m.alert_sent,
            sample_count=m.sample_count,
        )
        for m in metrics
    ]


@router.post(
    "/detect",
    response_model=DriftMetricResponse,
    summary="Run drift detection",
    status_code=status.HTTP_200_OK,
)
async def run_drift_detection(
    request: DriftDetectionRequest,
    db: AsyncSession = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """
    Manually trigger drift detection for a model.
    
    Useful for on-demand drift analysis outside the scheduled runs.
    """
    # Verify model exists
    result = await db.execute(
        select(MLModelMetadata)
        .where(MLModelMetadata.model_id == request.model_id)
        .where(MLModelMetadata.is_active == True)
    )
    model = result.scalar_one_or_none()
    
    if not model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active model {request.model_id} not found"
        )
    
    # Run drift detection
    detector = DriftDetector(
        model_id=request.model_id,
        db_session=db,
        drift_threshold=request.drift_threshold or 2.0,
        lookback_days=request.lookback_days or 7,
    )
    
    try:
        drift_metric = await detector.detect_drift()
        
        if not drift_metric:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Insufficient data for drift detection"
            )
        
        return DriftMetricResponse(
            id=drift_metric.id,
            model_id=drift_metric.model_id,
            timestamp=drift_metric.timestamp,
            drift_detected=drift_metric.drift_detected,
            drift_type=drift_metric.drift_type,
            feature_drift_scores=drift_metric.feature_drift_scores or {},
            drifted_features=drift_metric.drifted_features or [],
            prediction_drift_score=drift_metric.prediction_drift_score,
            prediction_z_score=drift_metric.prediction_z_score,
            prediction_ks_statistic=drift_metric.prediction_ks_statistic,
            prediction_p_value=drift_metric.prediction_p_value,
            current_mean=drift_metric.current_mean,
            current_std=drift_metric.current_std,
            historical_mean=drift_metric.historical_mean,
            historical_std=drift_metric.historical_std,
            alerts=drift_metric.alerts or [],
            alert_sent=drift_metric.alert_sent,
            sample_count=drift_metric.sample_count,
        )
        
    except Exception as e:
        logger.error(f"Error running drift detection: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Drift detection failed: {str(e)}"
        )


@router.get(
    "/summary",
    summary="Get drift summary",
)
async def get_drift_summary(
    days: int = Query(30, ge=1, le=90, description="Number of days to analyze"),
    db: AsyncSession = Depends(get_db),
    current_user_id: int = Depends(get_current_user_id),
):
    """
    Get summary statistics for drift detection.
    
    Returns aggregated drift metrics across all models.
    """
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    result = await db.execute(
        select(MLDriftMetric)
        .where(MLDriftMetric.timestamp >= cutoff_date)
    )
    metrics = list(result.scalars().all())
    
    if not metrics:
        return {
            "total_checks": 0,
            "drift_detected_count": 0,
            "drift_rate": 0.0,
            "models_with_drift": [],
            "most_common_drift_type": None,
        }
    
    drift_detected = [m for m in metrics if m.drift_detected]
    models_with_drift = list(set(m.model_id for m in drift_detected))
    
    drift_types = [m.drift_type for m in drift_detected if m.drift_type]
    most_common_type = max(set(drift_types), key=drift_types.count) if drift_types else None
    
    return {
        "total_checks": len(metrics),
        "drift_detected_count": len(drift_detected),
        "drift_rate": len(drift_detected) / len(metrics) if metrics else 0.0,
        "models_with_drift": models_with_drift,
        "most_common_drift_type": most_common_type,
        "period_days": days,
    }
