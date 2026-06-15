"""
Tests for app.api.worker_control
==================================
Validates the worker sidecar control-plane routes: liveness, task listing,
pause/resume/trigger/restart, and auth enforcement.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.worker_control import router
from app.workers.supervisor import PauseToken, TaskState, TriggerToken, create_task_states

# ── Dummy INTERNAL_API_SECRET for tests ───────────────────────────────────────
_TEST_SECRET = "a" * 64   # 64 chars, passes the min_length=64 validator


# ── Test app fixture ───────────────────────────────────────────────────────────

def _make_app(task_names: list[str] | None = None) -> FastAPI:
    """Build a minimal FastAPI app with the worker control router attached."""
    app = FastAPI()
    app.include_router(router)

    names = task_names or ["heartbeat", "cache_invalidation"]
    states = create_task_states(names)
    app.state.task_states = states
    app.state.shutdown_event = asyncio.Event()
    return app


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-Internal-Token": _TEST_SECRET}


# Patch settings.INTERNAL_API_SECRET for all tests in this module
@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(
        "app.api.worker_control.settings",
        MagicMock(INTERNAL_API_SECRET=_TEST_SECRET),
    )


# ── Liveness ───────────────────────────────────────────────────────────────────

class TestHealth:
    def test_health_returns_200_without_auth(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_health_returns_200_with_extra_headers(self, client, auth_headers):
        resp = client.get("/health", headers=auth_headers)
        assert resp.status_code == 200


# ── Auth enforcement ───────────────────────────────────────────────────────────

class TestAuth:
    def test_tasks_requires_auth(self, client):
        resp = client.get("/tasks")
        assert resp.status_code == 403

    def test_tasks_with_wrong_token_is_403(self, client):
        resp = client.get("/tasks", headers={"X-Internal-Token": "wrongtoken"})
        assert resp.status_code == 403

    def test_tasks_with_valid_token_is_200(self, client, auth_headers):
        resp = client.get("/tasks", headers=auth_headers)
        assert resp.status_code == 200

    def test_pause_without_auth_is_403(self, client):
        resp = client.post("/tasks/heartbeat/pause")
        assert resp.status_code == 403


# ── Task list ──────────────────────────────────────────────────────────────────

class TestTaskList:
    def test_returns_all_task_names(self, client, auth_headers):
        resp = client.get("/tasks", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "tasks" in body
        assert set(body["tasks"].keys()) == {"heartbeat", "cache_invalidation"}

    def test_task_state_shape(self, client, auth_headers):
        resp = client.get("/tasks", headers=auth_headers)
        task = resp.json()["tasks"]["heartbeat"]
        assert "name" in task
        assert "status" in task
        assert "last_run_at" in task
        assert "crash_count" in task
        assert "is_paused" in task

    def test_get_single_task(self, client, auth_headers):
        resp = client.get("/tasks/heartbeat", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["name"] == "heartbeat"

    def test_unknown_task_is_404(self, client, auth_headers):
        resp = client.get("/tasks/nonexistent", headers=auth_headers)
        assert resp.status_code == 404


# ── Pause / resume ─────────────────────────────────────────────────────────────

class TestPauseResume:
    def test_pause_sets_status_paused(self, client, auth_headers):
        resp = client.post("/tasks/heartbeat/pause", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "paused"

    def test_pause_reflects_in_state(self, client, auth_headers):
        client.post("/tasks/heartbeat/pause", headers=auth_headers)
        resp = client.get("/tasks/heartbeat", headers=auth_headers)
        assert resp.json()["is_paused"] is True

    def test_resume_clears_pause(self, client, auth_headers):
        client.post("/tasks/heartbeat/pause", headers=auth_headers)
        client.post("/tasks/heartbeat/resume", headers=auth_headers)
        resp = client.get("/tasks/heartbeat", headers=auth_headers)
        assert resp.json()["is_paused"] is False
        assert resp.json()["status"] == "running"


# ── Trigger ────────────────────────────────────────────────────────────────────

class TestTrigger:
    def test_trigger_returns_triggered(self, client, auth_headers):
        resp = client.post("/tasks/heartbeat/trigger", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "triggered"


# ── Restart ────────────────────────────────────────────────────────────────────

class TestRestart:
    def test_restart_with_no_handle_returns_200(self, client, auth_headers):
        # task_handle is None by default — restart is a no-op but still 200
        resp = client.post("/tasks/heartbeat/restart", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "restart_initiated"
