"""
Governance API - Model Registry and Drift Detection

Endpoints for model governance, drift monitoring, and state transitions.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDriftReport, AIMLModel
from app.ai.governance.drift_detector import DriftDetector
from app.ai.governance.unified_model_registry import UnifiedModelRegistry
from app.api.deps import get_db
from app.core.auth import require_role
from app.core.redis import PubSubClient

router = APIRouter()


class PromoteModelRequest(BaseModel):
    target_state: str


class TriggerDriftCheckRequest(BaseModel):
    lookback_hours: int = Field(24, ge=1, le=168, description="Hours of prediction history to analyze (1-168)")


class DriftReportResponse(BaseModel):
    model_config = ConfigDict(protected_namespaces=())
    id: int
    model_id: int
    model_name: str
    report_timestamp: datetime
    drift_detected: bool
    drift_score: float | None
    accuracy_drop: float | None
    distribution_metrics: dict | None
    action_taken: str | None
    created_at: datetime


@router.post("/models/{model_name}/promote")
async def promote_model(
    model_name: str,
    request: PromoteModelRequest,
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("admin")),
):
    """Promote model to next state (admin only)."""
    from app.core.redis import get_redis
    
    registry = UnifiedModelRegistry()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    try:
        model = await registry.promote_model(
            db=db,
            pubsub=pubsub,
            model_name=model_name,
            target_state=request.target_state,
        )
        
        return {
            "model_name": model.model_name,
            "version": model.model_version,
            "state": model.deployment_state,
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Model promotion failed: {str(e)}"
        )


@router.get("/models")
async def get_models(
    state: str | None = Query(None, description="Filter by deployment state"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """Get models by state with pagination."""
    registry = UnifiedModelRegistry()
    
    try:
        models = await registry.get_active_models(
            db=db,
            state=state or "live",
        )
        
        return {
            "models": [
                {
                    "model_id": m.id,
                    "model_name": m.model_name,
                    "model_type": m.model_type if hasattr(m, 'model_type') else "unknown",
                    "version": m.model_version,
                    "deployment_state": m.deployment_state,
                    "accuracy_metrics": {"accuracy": float(m.accuracy)} if m.accuracy else {},
                    "registered_at": m.created_at.isoformat() if hasattr(m, 'created_at') else None,
                    "last_prediction_at": m.last_prediction_at.isoformat() if hasattr(m, 'last_prediction_at') and m.last_prediction_at else None,
                }
                for m in models
            ],
            "total": len(models),
            "page": page,
            "limit": limit,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch models: {str(e)}"
        )


@router.post("/drift/check/{model_id}", response_model=DriftReportResponse)
async def trigger_drift_check(
    model_id: int,
    request: TriggerDriftCheckRequest = TriggerDriftCheckRequest(),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("admin")),
):
    """
    Manually trigger drift detection for a specific model (admin only).
    
    Analyzes recent predictions and creates drift report.
    Automatically demotes model if drift exceeds threshold.
    """
    from app.core.redis import get_redis
    
    detector = DriftDetector()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    try:
        report = await detector.check_drift(
            db=db,
            pubsub=pubsub,
            model_id=model_id,
            lookback_hours=request.lookback_hours,
        )
        
        # Get model name
        stmt = select(AIMLModel).where(AIMLModel.id == model_id)
        result = await db.execute(stmt)
        model = result.scalar_one()
        
        return DriftReportResponse(
            id=report.id,
            model_id=report.model_id,
            model_name=model.model_name,
            report_timestamp=report.report_timestamp,
            drift_detected=report.drift_detected,
            drift_score=float(report.drift_score) if report.drift_score else None,
            accuracy_drop=float(report.accuracy_drop) if report.accuracy_drop else None,
            distribution_metrics=report.distribution_metrics,
            action_taken=report.action_taken,
            created_at=report.created_at,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Drift check failed: {str(e)}"
        )


@router.get("/drift/reports")
@router.get("/drift-reports")
async def get_drift_reports(
    model_id: Optional[int] = Query(None, description="Filter by model ID"),
    drift_only: bool = Query(False, description="Only return reports with drift detected"),
    hours: int = Query(24, ge=1, le=720, description="Hours to look back (1-720)"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum reports to return"),
    db: AsyncSession = Depends(get_db),
    current_user = Depends(require_role("trader")),
):
    """
    Get drift detection reports with optional filtering.
    
    Returns historical drift reports for monitoring and analysis.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    stmt = select(AIDriftReport, AIMLModel).join(
        AIMLModel, AIDriftReport.model_id == AIMLModel.id
    ).where(AIDriftReport.report_timestamp >= cutoff)
    
    if model_id:
        stmt = stmt.where(AIDriftReport.model_id == model_id)
    
    if drift_only:
        stmt = stmt.where(AIDriftReport.drift_detected == True)
    
    stmt = stmt.order_by(AIDriftReport.report_timestamp.desc()).limit(limit)
    
    result = await db.execute(stmt)
    rows = result.all()
    
    reports = [
        {
            "id": report.id,
            "model_id": report.model_id,
            "model_name": model.model_name,
            "report_timestamp": report.report_timestamp.isoformat(),
            "drift_detected": report.drift_detected,
            "drift_score": float(report.drift_score) if report.drift_score else None,
            "accuracy_drop": float(report.accuracy_drop) if report.accuracy_drop else None,
            "distribution_metrics": report.distribution_metrics,
            "action_taken": report.action_taken,
            "created_at": report.created_at.isoformat(),
        }
        for report, model in rows
    ]
    
    return {
        "reports": reports,
        "total": len(reports),
    }
