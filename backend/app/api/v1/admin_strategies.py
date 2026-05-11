# PATH: backend/app/api/v1/admin_strategies.py
"""
Admin — Strategy Governance Routes
====================================
Cross-user visibility and promote/demote lifecycle management for the
global strategy library.  All endpoints require the admin role (verified
directly from the JWT claim via AdminUserID — zero extra DB round-trip).

Routes (prefix: /api/v1/admin/strategies)
------------------------------------------
  GET    /                       List all strategies (paginated, filterable)
  POST   /{id}/promote           Promote strategy to global library (scope → shared)
  DELETE /{id}/promote           Demote strategy from global library (scope → private)
  GET    /{id}/audit-log         List audit trail for a strategy

Rate limits reflect admin usage patterns — higher than regular endpoints
but still capped to prevent abuse.

NOTE: do NOT add `from __future__ import annotations` here — FastAPI needs
to resolve Annotated dependency aliases (AdminUserID) eagerly at import time.
PEP 563 lazy strings break TypeAdapter resolution when @limiter.limit is
stacked.  Python 3.11 supports all modern type syntax natively; the import
is unnecessary.
"""
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.auth import AdminUserID
from app.core.limiter import limiter
from app.exceptions import StrategyNotFoundError
from app.schemas.strategies import (
    AdminStrategiesListResponse,
    AdminStrategyItemResponse,
    CreateStrategyRequest,
    PromoteStrategyRequest,
    StrategyAuditLogListResponse,
    StrategyAuditLogResponse,
    StrategyResponse,
)
from app.services import strategy_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post(
    "",
    response_model=StrategyResponse,
    status_code=201,
    summary="Create a new global library strategy (admin)",
)
@limiter.limit("20/minute")
async def admin_create_library_strategy(
    request: Request,
    body: CreateStrategyRequest,
    admin_user_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    """
    Create a strategy directly as scope=shared / status=active in a single atomic
    transaction.  An audit log entry (action=promote) is written automatically so
    every library entry has a full governance chain of custody.
    """
    strategy = await strategy_service.admin_create_library_strategy(
        admin_user_id=int(admin_user_id),
        req=body,
        db=db,
    )
    await db.commit()
    return StrategyResponse.model_validate(strategy)


@router.get(
    "",
    response_model=AdminStrategiesListResponse,
    summary="List all strategies (admin)",
)
@limiter.limit("120/minute")
async def admin_list_strategies(
    request: Request,
    admin_user_id: AdminUserID,
    scope: str | None = Query(None, description="Filter by scope: 'private' | 'shared'"),
    status: str | None = Query(None, description="Filter by status: 'draft' | 'active' | 'paused' | 'archived'"),
    search: str | None = Query(None, max_length=100, description="Substring search on name and description"),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> AdminStrategiesListResponse:
    items, total = await strategy_service.admin_list_strategies(
        db,
        scope=scope,
        status=status,
        search=search,
        page=page,
        page_size=page_size,
    )
    return AdminStrategiesListResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/{strategy_id}/promote",
    response_model=StrategyResponse,
    status_code=200,
    summary="Promote strategy to global library",
)
@limiter.limit("30/minute")
async def promote_strategy(
    request: Request,
    body: PromoteStrategyRequest,
    admin_user_id: AdminUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    strategy = await strategy_service.promote_strategy(
        strategy_id=strategy_id,
        admin_user_id=int(admin_user_id),
        req=body,
        db=db,
    )
    await db.commit()
    return StrategyResponse.model_validate(strategy)


@router.delete(
    "/{strategy_id}/promote",
    response_model=StrategyResponse,
    status_code=200,
    summary="Demote strategy from global library",
)
@limiter.limit("30/minute")
async def demote_strategy(
    request: Request,
    body: PromoteStrategyRequest,
    admin_user_id: AdminUserID,
    strategy_id: UUID = Path(...),
    db: AsyncSession = Depends(get_db),
) -> StrategyResponse:
    strategy = await strategy_service.demote_strategy(
        strategy_id=strategy_id,
        admin_user_id=int(admin_user_id),
        req=body,
        db=db,
    )
    await db.commit()
    return StrategyResponse.model_validate(strategy)


@router.get(
    "/{strategy_id}/audit-log",
    response_model=StrategyAuditLogListResponse,
    summary="Get promote/demote audit trail for a strategy",
)
@limiter.limit("120/minute")
async def get_strategy_audit_log(
    request: Request,
    admin_user_id: AdminUserID,
    strategy_id: UUID = Path(...),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
) -> StrategyAuditLogListResponse:
    items, total = await strategy_service.get_strategy_audit_log(
        strategy_id=strategy_id,
        db=db,
        page=page,
        page_size=page_size,
    )
    return StrategyAuditLogListResponse(items=items, total=total)
