"""
Tests for app.api.worker_ai_processing
=========================================
Validates the worker sidecar's demand-driven AI processing routes:
InternalAuth enforcement on all 4 routes, status aggregation, and the
RuntimeError -> HTTP 502 mapping for forecast/classification dispatch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.worker_ai_processing import router

_TEST_SECRET = "a" * 64


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = MagicMock()
    app.state.redis_client = MagicMock(_redis=AsyncMock())
    return app


@pytest.fixture(autouse=True)
def _patch_secret(monkeypatch):
    monkeypatch.setattr(
        "app.api.worker_control.settings",
        MagicMock(INTERNAL_API_SECRET=_TEST_SECRET),
    )


@pytest.fixture
def client():
    app = _make_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-Internal-Token": _TEST_SECRET}


class TestAuth:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/ai-processing/status"),
            ("post", "/ai-processing/sentiment/dispatch"),
            ("post", "/ai-processing/forecast/dispatch"),
            ("post", "/ai-processing/classification/dispatch"),
        ],
    )
    def test_requires_internal_token(self, client, method, path):
        resp = getattr(client, method)(path)
        assert resp.status_code == 403


class TestStatus:
    def test_status_aggregates_all_3_categories(self, client, auth_headers):
        with patch(
            "app.ai.intelligence.nlp_engine.NLPEngine.pending_sentiment_count",
            AsyncMock(return_value=10),
        ):
            with patch(
                "app.ai.fusion.forecast_batch_worker.pending_forecast_count",
                AsyncMock(return_value=5),
            ):
                with patch(
                    "app.ai.intelligence.event_classifier.EventClassifier.pending_classification_count",
                    AsyncMock(return_value=2),
                ):
                    resp = client.get("/ai-processing/status", headers=auth_headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["sentiment"]["pending"] == 10
        assert body["forecast"]["pending"] == 5
        assert body["classification"]["pending"] == 2


class TestDispatch:
    def test_sentiment_dispatch_success(self, client, auth_headers):
        with patch(
            "app.ai.intelligence.nlp_engine.NLPEngine.flush_pending_sentiment",
            AsyncMock(return_value={"dispatched": 3, "calls_made": 1}),
        ):
            resp = client.post("/ai-processing/sentiment/dispatch", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"dispatched": 3, "calls_made": 1}

    def test_forecast_dispatch_failure_maps_to_502(self, client, auth_headers):
        with patch(
            "app.ai.fusion.forecast_batch_worker.flush_pending_forecasts",
            AsyncMock(side_effect=RuntimeError("forecast dispatch stopped early: budget_throttled")),
        ):
            resp = client.post("/ai-processing/forecast/dispatch", headers=auth_headers)
        assert resp.status_code == 502
        body = resp.json()
        assert body["detail"] == "dispatch_failed"
        assert "budget_throttled" in body["reason"]

    def test_classification_dispatch_failure_maps_to_502(self, client, auth_headers):
        with patch(
            "app.ai.intelligence.event_classifier.EventClassifier.flush_pending_classifications",
            AsyncMock(side_effect=RuntimeError("classification dispatch stopped early: nlp_result_id=5")),
        ):
            resp = client.post("/ai-processing/classification/dispatch", headers=auth_headers)
        assert resp.status_code == 502
        assert resp.json()["detail"] == "dispatch_failed"
