"""
Governance API — Model Registry, Drift Detection, and Summary

All state-changing endpoints are admin-only.  Read endpoints require the
trader role so they can surface model status on user-facing dashboards.

Endpoint overview:
  GET  /governance/summary                   — aggregate counts per state + drift
  GET  /governance/models                    — paginated model list (state filter)
  POST /governance/models/{model_id}/state   — admin: force state transition
  POST /governance/models/{model_name}/promote — admin: gate-enforced promotion
  POST /governance/drift/check/{model_id}    — admin: manual drift trigger
  GET  /governance/drift-reports             — historical drift reports
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDriftReport, AIMLModel
from app.ai.governance.drift_detector import DriftDetector
from app.ai.governance.unified_model_registry import UnifiedModelRegistry
from app.api.deps import get_db
from app.core.auth import require_role
from app.core.redis import PubSubClient

router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

class PromoteModelRequest(BaseModel):
    target_state: str


class UpdateModelStateRequest(BaseModel):
    new_state: str
    reason: str = Field(default="", max_length=500)


class TriggerDriftCheckRequest(BaseModel):
    lookback_hours: int = Field(
        default=24, ge=1, le=168,
        description="Hours of prediction history to analyze (1–168)",
    )


# ── Internal helpers ──────────────────────────────────────────────────────────

def _serialize_model(m: AIMLModel) -> dict:
    """Canonical model representation returned by all list/detail endpoints."""
    return {
        "model_id":         m.id,
        "model_name":       m.model_name,
        "model_type":       m.model_type,
        "version":          m.model_version,
        "deployment_state": m.deployment_state,
        "timeframe":        m.timeframe,
        "training_date":    m.training_date.isoformat() if m.training_date else None,
        "metrics": {
            "accuracy":  float(m.accuracy)  if m.accuracy  is not None else None,
            "precision": float(m.precision) if m.precision is not None else None,
            "recall":    float(m.recall)    if m.recall    is not None else None,
            "f1_score":  float(m.f1_score)  if m.f1_score  is not None else None,
        },
        "registered_at": m.created_at.isoformat(),
        "updated_at":    m.updated_at.isoformat(),
    }


def _serialize_drift_report(report: AIDriftReport, model_name: str) -> dict:
    """Canonical drift report representation."""
    return {
        "id":                   report.id,
        "model_id":             report.model_id,
        "model_name":           model_name,
        "report_timestamp":     report.report_timestamp.isoformat(),
        "drift_detected":       report.drift_detected,
        "drift_score":          float(report.drift_score)    if report.drift_score    is not None else None,
        "accuracy_drop":        float(report.accuracy_drop)  if report.accuracy_drop  is not None else None,
        "distribution_metrics": report.distribution_metrics,
        "action_taken":         report.action_taken,
        "created_at":           report.created_at.isoformat(),
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/summary")
async def get_governance_summary(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("trader")),
):
    """
    Aggregate governance health snapshot.

    Returns model counts by deployment state and the number of distinct
    drift alerts fired in the past 24 hours.  Designed to power the
    summary stat cards at the top of the admin governance dashboard.
    """
    state_counts_rows = (await db.execute(
        select(AIMLModel.deployment_state, func.count(AIMLModel.id).label("n"))
        .group_by(AIMLModel.deployment_state)
    )).all()

    counts: dict[str, int] = {row.deployment_state: row.n for row in state_counts_rows}

    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    drift_alerts_24h: int = (await db.execute(
        select(func.count(AIDriftReport.id)).where(
            AIDriftReport.drift_detected == True,
            AIDriftReport.report_timestamp >= cutoff_24h,
        )
    )).scalar_one()

    return {
        "states": {
            "live":    counts.get("live",    0),
            "paper":   counts.get("paper",   0),
            "shadow":  counts.get("shadow",  0),
            "retired": counts.get("retired", 0),
        },
        "drift_alerts_24h": drift_alerts_24h,
        "total_models":     sum(counts.values()),
    }


@router.get("/models")
async def get_models(
    state: str | None = Query(
        None,
        description="Filter by deployment state. Use 'all' or omit to return every state.",
    ),
    page:  int = Query(1,  ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("trader")),
):
    """
    Paginated model registry.

    When *state* is omitted or 'all', every model across all deployment
    states is returned — intended for the admin overview.  Specific state
    values (live, paper, shadow, retired) restrict results accordingly.
    """
    stmt = select(AIMLModel)
    if state and state != "all":
        stmt = stmt.where(AIMLModel.deployment_state == state)
    stmt = stmt.order_by(AIMLModel.updated_at.desc())

    total: int = (await db.execute(
        select(func.count()).select_from(stmt.subquery())
    )).scalar_one()

    offset = (page - 1) * limit
    models = list(
        (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()
    )

    return {
        "models": [_serialize_model(m) for m in models],
        "total":  total,
        "page":   page,
        "limit":  limit,
    }


@router.post("/models/{model_id}/state")
async def update_model_state(
    model_id: int,
    request: UpdateModelStateRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("admin")),
):
    """
    Admin: force a model into any valid deployment state.

    Quality gates are bypassed — this is an administrative override intended
    for incident response and manual governance.  The transition graph is
    still enforced (e.g. live cannot jump directly to paper).  The *reason*
    field is persisted in governance_metadata for the audit trail.
    """
    from app.core.redis import get_redis

    model = (await db.execute(
        select(AIMLModel).where(AIMLModel.id == model_id)
    )).scalar_one_or_none()

    if not model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model {model_id} not found",
        )

    registry = UnifiedModelRegistry()
    redis    = await get_redis()
    pubsub   = PubSubClient(redis)

    try:
        updated = await registry.promote_model(
            db=db,
            pubsub=pubsub,
            model_name=model.model_name,
            target_state=request.new_state,
            evaluation_results={"reason": request.reason, "forced_by_admin": True}
            if request.reason else {"forced_by_admin": True},
            bypass_gates=True,
        )
        return {
            "model_id":   updated.id,
            "model_name": updated.model_name,
            "state":      updated.deployment_state,
            "updated_at": updated.updated_at.isoformat(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@router.post("/models/{model_name}/promote")
async def promote_model(
    model_name: str,
    request: PromoteModelRequest,
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("admin")),
):
    """
    Admin: quality-gate-enforced model promotion.

    Unlike the /state endpoint this path enforces all accuracy, precision,
    and recall gates.  Use this for normal lifecycle progression.
    """
    from app.core.redis import get_redis

    registry = UnifiedModelRegistry()
    redis    = await get_redis()
    pubsub   = PubSubClient(redis)

    try:
        model = await registry.promote_model(
            db=db,
            pubsub=pubsub,
            model_name=model_name,
            target_state=request.target_state,
            bypass_gates=False,
        )
        return {
            "model_name": model.model_name,
            "version":    model.model_version,
            "state":      model.deployment_state,
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Model promotion failed: {str(exc)}",
        )


@router.post("/drift/check/{model_id}")
async def trigger_drift_check(
    model_id: int,
    request: TriggerDriftCheckRequest = TriggerDriftCheckRequest(),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("admin")),
):
    """
    Admin: manually trigger drift detection for a model.

    Analyses recent predictions, writes an AIDriftReport, and demotes the
    model automatically if drift exceeds the configured threshold.
    """
    from app.core.redis import get_redis

    detector = DriftDetector()
    redis    = await get_redis()
    pubsub   = PubSubClient(redis)

    try:
        report = await detector.check_drift(
            db=db,
            pubsub=pubsub,
            model_id=model_id,
            lookback_hours=request.lookback_hours,
        )
        model = (await db.execute(
            select(AIMLModel).where(AIMLModel.id == model_id)
        )).scalar_one()

        return _serialize_drift_report(report, model.model_name)

    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Drift check failed: {str(exc)}",
        )


@router.get("/drift/reports")
@router.get("/drift-reports")
async def get_drift_reports(
    model_id:   Optional[int]  = Query(None, description="Filter to a single model"),
    drift_only: bool           = Query(False, description="Only return reports where drift was detected"),
    hours:      int            = Query(168,   ge=1, le=2160, description="Look-back window in hours (default 7 days)"),
    limit:      int            = Query(500,   ge=1, le=2000, description="Maximum reports to return"),
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("trader")),
):
    """
    Historical drift detection reports.

    Defaults to the last 7 days so the frontend can render per-model drift
    sparklines without a separate per-model fetch.  Use *model_id* and
    *drift_only* to narrow results when needed.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    stmt = (
        select(AIDriftReport, AIMLModel)
        .join(AIMLModel, AIDriftReport.model_id == AIMLModel.id)
        .where(AIDriftReport.report_timestamp >= cutoff)
    )

    if model_id is not None:
        stmt = stmt.where(AIDriftReport.model_id == model_id)

    if drift_only:
        stmt = stmt.where(AIDriftReport.drift_detected == True)

    stmt = stmt.order_by(AIDriftReport.report_timestamp.desc()).limit(limit)

    rows = (await db.execute(stmt)).all()

    return {
        "reports": [_serialize_drift_report(report, model.model_name) for report, model in rows],
        "total":   len(rows),
    }
