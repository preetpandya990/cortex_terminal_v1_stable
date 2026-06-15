"""
Cortex AI — Worker Sidecar Control-Plane API
=============================================
FastAPI router mounted on the worker sidecar (:8001).

Routes:
    GET  /health              Liveness — no auth (Docker healthcheck + Prometheus)
    GET  /metrics             Prometheus scrape — no auth; syncs Gauges from TaskState
    GET  /tasks               All 13 task states — requires X-Internal-Token
    GET  /tasks/{name}        Single task detail — requires X-Internal-Token
    POST /tasks/{name}/pause  Cooperative pause at next safepoint
    POST /tasks/{name}/resume Lift pause; task continues at next checkpoint
    POST /tasks/{name}/trigger Wake task immediately from its sleep interval
    POST /tasks/{name}/restart Cancel task handle; supervised() auto-restarts it

Auth:
    X-Internal-Token header — compared with settings.INTERNAL_API_SECRET via
    secrets.compare_digest() to prevent timing-side-channel attacks.
    /health and /metrics are intentionally unauthenticated:
      - /health: Docker healthcheck does not send a token.
      - /metrics: Prometheus scrape agent does not send a token.

Per-task Prometheus metrics (synced at each /metrics scrape):
    worker_task_last_cycle_seconds{task}  — Unix timestamp of last cycle start
    worker_task_crash_count{task}         — Consecutive crash count
    worker_task_status{task}              — 0=starting 1=running 2=paused 3=crashed 4=stopped
"""
from __future__ import annotations

import logging
import secrets
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import Response

from app.core.config import get_settings
from app.workers.supervisor import TaskState, TaskStatus

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter()

# ── Status → int encoding for Prometheus Gauge ────────────────────────────────
_STATUS_INT: dict[TaskStatus, int] = {
    "starting": 0,
    "running":  1,
    "paused":   2,
    "crashed":  3,
    "stopped":  4,
}


# ── Internal token auth ────────────────────────────────────────────────────────

async def _require_internal_token(
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    """
    Validate the X-Internal-Token header using constant-time comparison.

    FastAPI maps the ``x_internal_token`` parameter name to the
    ``X-Internal-Token`` / ``x-internal-token`` HTTP header automatically
    (underscore ↔ hyphen conversion, case-insensitive).

    secrets.compare_digest() prevents timing-side-channel attacks that would
    allow an attacker to infer the token one byte at a time by measuring
    response latency differences.
    """
    if x_internal_token is None or not secrets.compare_digest(
        x_internal_token.encode("utf-8"),
        settings.INTERNAL_API_SECRET.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid_internal_token",
        )


# Annotated type that routes use as a dependency parameter.
# Usage: ``async def route(_: InternalAuth, ...) -> ...``
InternalAuth = Annotated[None, Depends(_require_internal_token)]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _serialize_state(state: TaskState) -> dict[str, Any]:
    """Convert a TaskState to a JSON-serializable dict."""
    return {
        "name":        state.name,
        "status":      state.status,
        "last_run_at": state.last_run_at.isoformat() if state.last_run_at else None,
        "crash_count": state.crash_count,
        "is_paused":   state.pause_token.is_paused,
        "has_handle":  state.task_handle is not None and not state.task_handle.done(),
    }


def _get_states(request: Request) -> dict[str, TaskState]:
    return request.app.state.task_states


def _get_state(request: Request, name: str) -> TaskState:
    states: dict[str, TaskState] = request.app.state.task_states
    if name not in states:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"task '{name}' not found",
        )
    return states[name]


# ── Liveness (no auth — Docker healthcheck) ───────────────────────────────────

@router.get("/health", include_in_schema=False)
async def health() -> dict[str, str]:
    """
    Liveness endpoint.  Always 200 while the process is alive.
    Docker healthcheck: ``curl -f http://localhost:8001/health``
    """
    return {"status": "ok"}


# ── Prometheus metrics (no auth — Prometheus scrape agent) ────────────────────

@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """
    Prometheus scrape endpoint.

    Syncs per-task Gauges from live TaskState immediately before generating
    the scrape payload — ensures /metrics reflects real-time state at scrape
    time without a separate background sync loop.
    """
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    from app.core.metrics import (
        worker_task_crash_count_gauge,
        worker_task_last_cycle_seconds,
        worker_task_status_gauge,
    )

    states: dict[str, TaskState] = _get_states(request)
    for name, state in states.items():
        worker_task_status_gauge.labels(task=name).set(
            _STATUS_INT.get(state.status, 0)
        )
        worker_task_crash_count_gauge.labels(task=name).set(state.crash_count)
        if state.last_run_at is not None:
            worker_task_last_cycle_seconds.labels(task=name).set(
                state.last_run_at.timestamp()
            )

    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── Task list ──────────────────────────────────────────────────────────────────

@router.get("/tasks")
async def list_tasks(_: InternalAuth, request: Request) -> dict[str, Any]:
    """Return status of all 13 supervised tasks."""
    states = _get_states(request)
    return {
        "count": len(states),
        "tasks": {name: _serialize_state(state) for name, state in states.items()},
    }


@router.get("/tasks/{name}")
async def get_task(_: InternalAuth, request: Request, name: str) -> dict[str, Any]:
    """Return status of a single task by name."""
    return _serialize_state(_get_state(request, name))


# ── Task control ───────────────────────────────────────────────────────────────

@router.post("/tasks/{name}/pause", status_code=status.HTTP_200_OK)
async def pause_task(_: InternalAuth, request: Request, name: str) -> dict[str, str]:
    """
    Pause the task at its next cooperative safepoint (pause.checkpoint() call).

    The task is NOT cancelled — it blocks at the checkpoint until resumed.
    Pub/sub listeners hold their connection open while paused; messages queue
    in the Redis client buffer and are processed when the pause lifts.
    """
    state = _get_state(request, name)
    state.pause_token.pause()
    state.status = "paused"
    logger.info("Task %s paused via control plane", name)
    return {"task": name, "status": "paused"}


@router.post("/tasks/{name}/resume", status_code=status.HTTP_200_OK)
async def resume_task(_: InternalAuth, request: Request, name: str) -> dict[str, str]:
    """Resume a paused task — it continues from its checkpoint immediately."""
    state = _get_state(request, name)
    state.pause_token.resume()
    if state.status == "paused":
        state.status = "running"
    logger.info("Task %s resumed via control plane", name)
    return {"task": name, "status": "running"}


@router.post("/tasks/{name}/trigger", status_code=status.HTTP_200_OK)
async def trigger_task(_: InternalAuth, request: Request, name: str) -> dict[str, str]:
    """
    Wake the task immediately from its sleep interval.

    The task's trigger.wait_or_timeout(N) returns True right away instead of
    sleeping the remaining seconds.  Useful for forcing an on-demand cycle
    without waiting for the next natural interval.
    """
    state = _get_state(request, name)
    state.trigger_token.fire()
    logger.info("Task %s triggered via control plane", name)
    return {"task": name, "status": "triggered"}


@router.post("/tasks/{name}/restart", status_code=status.HTTP_200_OK)
async def restart_task(_: InternalAuth, request: Request, name: str) -> dict[str, str]:
    """
    Cancel the task's asyncio.Task handle.

    The supervised() wrapper catches CancelledError, resets status, and
    immediately starts a fresh coroutine from the factory — the task is back
    running within milliseconds.  crash_count is NOT incremented for a manual
    restart (only unhandled exceptions count as crashes).
    """
    state = _get_state(request, name)
    if state.task_handle is not None and not state.task_handle.done():
        state.task_handle.cancel()
        logger.info("Task %s restart initiated via control plane", name)
    else:
        logger.warning(
            "Task %s restart requested but task handle is absent or already done", name
        )
    return {"task": name, "status": "restart_initiated"}
