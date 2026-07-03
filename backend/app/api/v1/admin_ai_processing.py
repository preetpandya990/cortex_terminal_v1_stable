# PATH: backend/app/api/v1/admin_ai_processing.py
"""
Admin — Demand-Driven AI Processing Queue Routes
===================================================
Thin proxy from the main API (:8000) to the worker sidecar's demand-driven
AI processing control-plane (:8001, worker_ai_processing.py). Lets an admin
view pending queue depth and manually dispatch each of the 3 Tier-2 Gemini
paths (sentiment batching, event classification, news-forecast batching)
from the Worker Control Panel, without waiting for the daily safety net.

Routes (prefix: /api/v1/admin/ai-processing)
-----------------------------------------------
  GET  /status                    Pending counts + auto-dispatch flags, all 3 categories
  POST /sentiment/dispatch        Drain the sentiment queue now
  POST /forecast/dispatch         Drain the forecast queue now
  POST /classification/dispatch   Drain the classification queue now

There is deliberately no POST /dispatch-all endpoint — "Dispatch All" is a
frontend-side Promise.allSettled over the 3 mutations above, so one
category's failure never blocks the other two without needing new backend
transaction semantics.

NOTE: do NOT add `from __future__ import annotations` here — FastAPI needs
to resolve Annotated dependency aliases (AdminUserID) eagerly at import time.
PEP 563 lazy strings break TypeAdapter resolution when @limiter.limit is
stacked. Python 3.11 supports all modern type syntax natively; the import
is unnecessary. (Same gotcha documented in admin_strategies.py.)
"""
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.auth import AdminUserID
from app.core.limiter import limiter
from app.core.worker_client import WorkerDispatchError

logger = logging.getLogger(__name__)

router = APIRouter()

_WORKER_UNAVAILABLE = {"detail": "worker_unavailable", "degraded": True}


class CategoryStatus(BaseModel):
    pending: int
    auto_flush: bool


class AIProcessingStatusResponse(BaseModel):
    sentiment: CategoryStatus
    forecast: CategoryStatus
    classification: CategoryStatus


class DispatchResult(BaseModel):
    dispatched: int
    calls_made: int


def _client(request: Request):
    """Extract the WorkerClient singleton from app state."""
    return request.app.state.worker_client


@router.get("/status")
@limiter.limit("30/minute")
async def get_ai_processing_status(request: Request, admin_user_id: AdminUserID) -> JSONResponse:
    """Pending queue depth + auto-dispatch flag for each of the 3 categories."""
    result = await _client(request).get_ai_processing_status()
    if result is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_WORKER_UNAVAILABLE,
        )
    # Validate the sidecar's payload against the documented response shape
    # before returning it — a schema drift there should surface as a 502,
    # not silently pass through malformed data to the frontend.
    validated = AIProcessingStatusResponse.model_validate(result)
    return JSONResponse(content=validated.model_dump())


@router.post("/sentiment/dispatch")
@limiter.limit("10/minute")
async def dispatch_sentiment(request: Request, admin_user_id: AdminUserID) -> JSONResponse:
    return await _dispatch(request, "sentiment")


@router.post("/forecast/dispatch")
@limiter.limit("10/minute")
async def dispatch_forecast(request: Request, admin_user_id: AdminUserID) -> JSONResponse:
    return await _dispatch(request, "forecast")


@router.post("/classification/dispatch")
@limiter.limit("10/minute")
async def dispatch_classification(request: Request, admin_user_id: AdminUserID) -> JSONResponse:
    return await _dispatch(request, "classification")


async def _dispatch(request: Request, category: str) -> JSONResponse:
    """Shared dispatch body for all 3 category endpoints."""
    try:
        result = await _client(request).dispatch_ai_processing(category)
    except WorkerDispatchError as exc:
        logger.warning("admin_ai_processing: %s dispatch failed — %s", category, exc)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    validated = DispatchResult.model_validate(result)
    return JSONResponse(content=validated.model_dump())
