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
from app.api.deps import get_db
from app.core.auth import require_role
from app.core.redis import PubSubClient, RedisChannels

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

    # A8 — admin force-transitions update ai_ml_models directly (with transition-graph
    # validation and a mandatory audit trail) rather than going through
    # UnifiedModelRegistry.promote_model(bypass_gates=True), which is now deprecated.
    # This endpoint is intentionally governance-table-only: it does NOT promote
    # ml_model_metadata — that authority belongs exclusively to ModelPromoter.
    _VALID_TRANSITIONS: dict[str, list[str]] = {
        "shadow":  ["paper"],
        "paper":   ["live", "shadow"],
        "live":    ["shadow"],
        "retired": ["shadow"],
    }
    from_state = model.deployment_state
    if request.new_state not in _VALID_TRANSITIONS.get(from_state, []):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid governance transition: {from_state} → {request.new_state}. "
                f"Valid from '{from_state}': {_VALID_TRANSITIONS.get(from_state, [])}"
            ),
        )

    from app.core.redis import get_redis

    redis  = await get_redis()
    pubsub = PubSubClient(redis)

    try:
        # Persist admin override reason in governance_metadata for the audit trail.
        meta = dict(model.governance_metadata or {})
        meta.setdefault("admin_overrides", []).append({
            "from_state":  from_state,
            "to_state":    request.new_state,
            "reason":      request.reason or "",
            "forced_at":   datetime.now(timezone.utc).isoformat(),
            "forced_by":   getattr(current_user, "email", "admin"),
        })
        model.governance_metadata = meta
        model.deployment_state    = request.new_state
        model.updated_at          = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(model)

        await pubsub.publish_json(RedisChannels.MODELS_STATE_CHANGES, {
            "action":        "model_state_changed",
            "model_name":    model.model_name,
            "from_state":    from_state,
            "to_state":      request.new_state,
            "model_version": model.model_version,
            "forced_by_admin": True,
            "timestamp":     datetime.now(timezone.utc).isoformat(),
        })

        return {
            "model_id":   model.id,
            "model_name": model.model_name,
            "state":      model.deployment_state,
            "updated_at": model.updated_at.isoformat(),
        }
    except Exception as exc:
        await db.rollback()
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
    # A8 — route through ModelPromoter (the single authority) instead of the
    # deprecated UnifiedModelRegistry.promote_model().  The governance model_name
    # (e.g. "cortex_xgboost_1d") maps to an ml_model_metadata record via
    # AIMLModel.model_type == MLModelMetadata.model_name (e.g. "xgboost").
    from app.models.ml_data import MLModelMetadata
    from app.ml.model_registry import ModelPromoter, QualityGateError

    # 1. Resolve governance record → model_type.
    ai_model = (await db.execute(
        select(AIMLModel).where(AIMLModel.model_name == model_name)
    )).scalar_one_or_none()
    if not ai_model:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Governance model '{model_name}' not found",
        )

    # 2. Map target_state (governance vocabulary) → ModelPromoter method.
    #    Only "paper" (staging) and "live" (production) are meaningful upgrade
    #    targets; downgrades must use the /state endpoint.
    target = request.target_state
    if target not in ("paper", "live"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Gated promotion only supports target_state 'paper' or 'live'. "
                f"For downgrades, use POST /governance/models/{{id}}/state."
            ),
        )

    # 3. Find the latest ML model record in the appropriate pre-promotion status.
    #    "live" requires a staging record; "paper" requires a development record.
    _REQUIRED_STATUS = {"live": "staging", "paper": "development"}
    required_status = _REQUIRED_STATUS[target]

    ml_model = (await db.execute(
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == ai_model.model_type,
            MLModelMetadata.status     == required_status,
        )
        .order_by(MLModelMetadata.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()

    if not ml_model:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"No '{required_status}' ML model found for type '{ai_model.model_type}'. "
                f"Run the training pipeline and advance to '{required_status}' first."
            ),
        )

    # 4. Delegate to ModelPromoter — enforces A6 gates + atomically projects
    #    deployment_state back to ai_ml_models in the same DB transaction.
    promoter = ModelPromoter(db)
    try:
        if target == "live":
            promoted = await promoter.promote_to_production(
                model_version=ml_model.model_version,
                model_name=ai_model.model_type,
            )
        else:  # "paper"
            promoted = await promoter.promote_to_staging(
                model_version=ml_model.model_version,
            )

        # Refresh the governance row (updated by the atomic projection).
        await db.refresh(ai_model)
        return {
            "model_name":       ai_model.model_name,
            "version":          ai_model.model_version,
            "state":            ai_model.deployment_state,
            "ml_model_version": promoted.model_version,
        }
    except QualityGateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"message": "Quality gates failed", "failed_checks": exc.failed_checks},
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Model promotion failed: {str(exc)}",
        )


@router.get("/ensemble/status")
async def get_ensemble_status(
    db: AsyncSession = Depends(get_db),
    current_user=Depends(require_role("trader")),
):
    """
    Current Single-Active-Member Ensemble composition.

    Queries ``ml_model_metadata`` for all production-tier records (both active
    and dormant) across the XGBoost and GRU families.  Returns the ensemble
    mode, per-member metrics, and the activation gate gap for any dormant
    member — so the governance dashboard can surface which models are waiting
    to be activated and exactly what performance gap remains.
    """
    from app.models.ml_data import MLModelMetadata
    from sqlalchemy import case

    _GRU_AUC_PR_GATE = 0.50
    _XGB_AUC_PR_GATE = 0.60
    _SYMBOL_COV_GATE = 0.85

    # Fetch all production-tier records for the two core model types.
    # Order: active first (is_active DESC), then most-recently-deployed first.
    stmt = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name.in_(["xgboost", "gru"]),
            MLModelMetadata.status == "production",
        )
        .order_by(
            case((MLModelMetadata.is_active == True, 0), else_=1),
            MLModelMetadata.deployed_at.desc().nullslast(),
        )
    )
    rows = list((await db.execute(stmt)).scalars().all())

    # ── Pass 1: collect raw member data (one record per model_name) ───────────
    seen: set[str] = set()
    raw: list[dict] = []
    for row in rows:
        if row.model_name in seen:
            continue
        seen.add(row.model_name)

        m                  = row.training_metrics or {}
        auc_pr             = m.get("auc_pr")
        deflated_sharpe    = m.get("deflated_sharpe")
        ece_after          = m.get("ece_after")
        accuracy           = m.get("accuracy")
        symbol_coverage    = m.get("symbol_coverage")
        stored_weight      = m.get("ensemble_weight")        # A5 optimizer recommendation
        ensemble_accretive = m.get("ensemble_accretive")     # False when EnsembleNotAccretiveError was raised

        raw.append({
            "row":                row,
            "auc_pr":             auc_pr,
            "deflated_sharpe":    deflated_sharpe,
            "ece_after":          ece_after,
            "accuracy":           accuracy,
            "symbol_coverage":    symbol_coverage,
            "stored_weight":      stored_weight,
            "ensemble_accretive": ensemble_accretive,
        })

    # ── Pass 2: compute effective_weight from active membership ───────────────
    # This mirrors RegistryModelLoader.load_production_ensemble exactly:
    #   0 active → all 0.0
    #   1 active → that member = 1.0, all others = 0.0
    #   2+ active → stored weights normalised to sum=1.0 across active members
    active_indices = [i for i, r in enumerate(raw) if r["row"].is_active]
    active_count   = len(active_indices)

    # Pre-compute normalisation denominator for the 2+ case.
    if active_count >= 2:
        active_raw_sum = sum(
            float(raw[i]["stored_weight"]) if raw[i]["stored_weight"] is not None else 0.0
            for i in active_indices
        )

    def _effective_weight(idx: int) -> float:
        r = raw[idx]
        if not r["row"].is_active:
            return 0.0
        if active_count == 1:
            return 1.0
        # active_count >= 2: normalise stored weights; uniform fallback if all zero.
        raw_w = float(r["stored_weight"]) if r["stored_weight"] is not None else 0.0
        if active_raw_sum > 0:
            return raw_w / active_raw_sum
        return 1.0 / active_count

    # ── Pass 3: build the final member list ───────────────────────────────────
    members = []
    for idx, r in enumerate(raw):
        row = r["row"]

        # Activation gate info (only meaningful for dormant members).
        gate: dict | None = None
        if not row.is_active:
            auc_pr          = r["auc_pr"]
            symbol_coverage = r["symbol_coverage"]
            auc_gate        = _GRU_AUC_PR_GATE if row.model_name == "gru" else _XGB_AUC_PR_GATE
            cov_gate        = _SYMBOL_COV_GATE
            checks = []
            if auc_pr is not None:
                checks.append({
                    "metric":   "auc_pr",
                    "required": auc_gate,
                    "current":  float(auc_pr),
                    "gap":      round(float(auc_pr) - auc_gate, 4),
                    "pass":     float(auc_pr) >= auc_gate,
                })
            if symbol_coverage is not None:
                checks.append({
                    "metric":   "symbol_coverage",
                    "required": cov_gate,
                    "current":  float(symbol_coverage),
                    "gap":      round(float(symbol_coverage) - cov_gate, 4),
                    "pass":     float(symbol_coverage) >= cov_gate,
                })
            gate = {
                "checks":   checks,
                "all_pass": all(c["pass"] for c in checks),
            }

        members.append({
            "model_name":           row.model_name,
            "model_version":        row.model_version,
            "status":               row.status,
            "is_active":            row.is_active,
            "role":                 "active" if row.is_active else "dormant",
            "effective_weight":     _effective_weight(idx),
            # training_weight is the raw A5 optimizer recommendation — kept for
            # transparency so the UI can distinguish "runtime serving weight" from
            # "what the optimizer recommended at training time".
            "training_weight":      float(r["stored_weight"]) if r["stored_weight"] is not None else None,
            "is_ensemble_accretive": bool(r["ensemble_accretive"]) if r["ensemble_accretive"] is not None else None,
            "deployed_at":          row.deployed_at.isoformat() if row.deployed_at else None,
            "metrics": {
                "auc_pr":          float(r["auc_pr"])          if r["auc_pr"]          is not None else None,
                "deflated_sharpe": float(r["deflated_sharpe"]) if r["deflated_sharpe"] is not None else None,
                "ece_after":       float(r["ece_after"])       if r["ece_after"]       is not None else None,
                "accuracy":        float(r["accuracy"])        if r["accuracy"]        is not None else None,
                "symbol_coverage": float(r["symbol_coverage"]) if r["symbol_coverage"] is not None else None,
            },
            "activation_gate": gate,
        })

    # Sort: active first, then dormant.
    members.sort(key=lambda m: (0 if m["is_active"] else 1, m["model_name"]))

    active_count = sum(1 for m in members if m["is_active"])
    mode = "full_ensemble" if active_count >= 2 else (
        "xgboost_only" if any(m["model_name"] == "xgboost" and m["is_active"] for m in members)
        else "degraded"
    )

    return {
        "mode":    mode,
        "members": members,
    }


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
