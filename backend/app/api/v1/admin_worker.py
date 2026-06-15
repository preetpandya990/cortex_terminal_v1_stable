"""
Cortex AI — Admin Worker Control Routes
=========================================
Thin proxy from the main API (:8000) to the worker sidecar's control-plane
(:8001).  Protected by the existing admin authentication dependency.

All routes delegate to WorkerClient which implements the fail-open contract:
if the worker is temporarily unreachable, all endpoints return 503 with
``{"detail": "worker_unavailable", "degraded": true}`` rather than raising.

Routes (prefix: /api/v1/admin/worker):
    GET  /health              Worker liveness
    GET  /tasks               All 13 task states
    GET  /tasks/{name}        Single task detail
    POST /tasks/{name}/pause  Pause task at next safepoint
    POST /tasks/{name}/resume Resume a paused task
    POST /tasks/{name}/trigger Force an immediate task cycle
    POST /tasks/{name}/restart Cancel + restart task via supervisor
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.core.auth import AdminUserID

logger = logging.getLogger(__name__)

router = APIRouter()

# ── Sentinel response returned when the worker sidecar is unreachable ─────────
# Using a module-level constant avoids allocating a new dict on every request
# while keeping the response consistent across all routes.
_WORKER_UNAVAILABLE = {"detail": "worker_unavailable", "degraded": True}


def _client(request: Request):
    """Extract the WorkerClient singleton from app state."""
    return request.app.state.worker_client


def _proxy(result: dict[str, Any] | None) -> JSONResponse:
    """Wrap a WorkerClient result (or None) in a JSONResponse."""
    if result is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=_WORKER_UNAVAILABLE,
        )
    return JSONResponse(content=result)


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.get("/health")
async def worker_health(_: AdminUserID, request: Request) -> JSONResponse:
    """Check worker sidecar liveness."""
    return _proxy(await _client(request).get_health())


@router.get("/tasks")
async def list_tasks(_: AdminUserID, request: Request) -> JSONResponse:
    """Return status of all 13 supervised tasks."""
    return _proxy(await _client(request).get_tasks())


@router.get("/tasks/{name}")
async def get_task(_: AdminUserID, request: Request, name: str) -> JSONResponse:
    """Return status of a single task by name."""
    return _proxy(await _client(request).get_task(name))


@router.post("/tasks/{name}/pause")
async def pause_task(_: AdminUserID, request: Request, name: str) -> JSONResponse:
    """Pause a task at its next cooperative safepoint."""
    return _proxy(await _client(request).pause_task(name))


@router.post("/tasks/{name}/resume")
async def resume_task(_: AdminUserID, request: Request, name: str) -> JSONResponse:
    """Resume a paused task."""
    return _proxy(await _client(request).resume_task(name))


@router.post("/tasks/{name}/trigger")
async def trigger_task(_: AdminUserID, request: Request, name: str) -> JSONResponse:
    """Force an immediate task cycle (wake from sleep interval)."""
    return _proxy(await _client(request).trigger_task(name))


@router.post("/tasks/{name}/restart")
async def restart_task(_: AdminUserID, request: Request, name: str) -> JSONResponse:
    """Cancel and restart a task; the supervisor auto-restarts it."""
    return _proxy(await _client(request).restart_task(name))
