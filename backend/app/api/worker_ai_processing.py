"""
Cortex AI — Worker Sidecar: Demand-Driven AI Processing Control
==================================================================
FastAPI router mounted on the worker sidecar (:8001), alongside the generic
task control-plane (worker_control.py). Exposes the pending-queue status and
manual dispatch actions for the 3 demand-driven Tier-2 Gemini paths
(sentiment batching, event classification, news-forecast batching).

Routes:
    GET  /ai-processing/status                    Pending counts + auto-flush flags
    POST /ai-processing/sentiment/dispatch         Drain the sentiment queue now
    POST /ai-processing/forecast/dispatch          Drain the forecast queue now
    POST /ai-processing/classification/dispatch    Drain the classification queue now

Auth: same X-Internal-Token dependency as worker_control.py (InternalAuth).

Dispatch failure contract: unlike the read-only status endpoint, the 3
dispatch endpoints never silently swallow a failed drain — a RuntimeError
raised mid-drain (budget-throttled Gemini quota, or a failed classification
call) is mapped to HTTP 502 with a real ``reason`` field, so
WorkerClient.dispatch_ai_processing() (which raises WorkerDispatchError on
any non-2xx) can distinguish a real failure from "0 pending, nothing to do".
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.worker_control import InternalAuth
from app.core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _session_factory(request: Request) -> Any:
    return request.app.state.session_factory


def _redis(request: Request) -> Any:
    return request.app.state.redis_client._redis


@router.get("/ai-processing/status")
async def get_ai_processing_status(_: InternalAuth, request: Request) -> dict[str, Any]:
    """Pending queue depth + auto-dispatch flag for each of the 3 categories."""
    from app.ai.fusion.forecast_batch_worker import pending_forecast_count
    from app.ai.intelligence.event_classifier import EventClassifier
    from app.ai.intelligence.nlp_engine import NLPEngine
    from app.core.metrics import ai_processing_pending_gauge

    settings = get_settings()
    redis = _redis(request)

    sentiment_pending = await NLPEngine.pending_sentiment_count()
    forecast_pending = await pending_forecast_count(redis)
    classification_pending = await EventClassifier(
        use_llm=False
    ).pending_classification_count(redis)

    ai_processing_pending_gauge.labels(category="sentiment").set(sentiment_pending)
    ai_processing_pending_gauge.labels(category="forecast").set(forecast_pending)
    ai_processing_pending_gauge.labels(category="classification").set(classification_pending)

    return {
        "sentiment": {
            "pending": sentiment_pending,
            "auto_flush": settings.SENTIMENT_AUTO_FLUSH,
        },
        "forecast": {
            "pending": forecast_pending,
            "auto_flush": settings.FORECAST_AUTO_DISPATCH,
        },
        "classification": {
            "pending": classification_pending,
            "auto_flush": settings.EVENT_CLASSIFIER_AUTO_DISPATCH,
        },
    }


@router.post("/ai-processing/sentiment/dispatch")
async def dispatch_sentiment(_: InternalAuth) -> dict[str, int]:
    """
    Drain the in-process sentiment batch queue now, regardless of
    SENTIMENT_AUTO_FLUSH. Never fails loudly — NLPEngine's flush path
    resolves every item (falling back to neutral on any per-item LLM
    failure), so there is no partial-drain error state to surface here.
    """
    from app.ai.intelligence.nlp_engine import NLPEngine
    from app.core.metrics import ai_processing_dispatch_total

    result = await NLPEngine.flush_pending_sentiment()
    ai_processing_dispatch_total.labels(
        category="sentiment", trigger_source="manual", outcome="success"
    ).inc()
    logger.info("worker_ai_processing: sentiment dispatch result=%s", result)
    return result


@router.post("/ai-processing/forecast/dispatch")
async def dispatch_forecast(_: InternalAuth, request: Request) -> Any:
    """Drain the Redis forecast batch queue now, regardless of FORECAST_AUTO_DISPATCH."""
    from app.ai.fusion.forecast_batch_worker import flush_pending_forecasts
    from app.core.metrics import ai_processing_dispatch_total

    try:
        result = await flush_pending_forecasts(_redis(request), _session_factory(request))
    except RuntimeError as exc:
        ai_processing_dispatch_total.labels(
            category="forecast", trigger_source="manual", outcome="error"
        ).inc()
        logger.warning("worker_ai_processing: forecast dispatch failed — %s", exc)
        return JSONResponse(
            status_code=502,
            content={"detail": "dispatch_failed", "reason": str(exc)},
        )

    ai_processing_dispatch_total.labels(
        category="forecast", trigger_source="manual", outcome="success"
    ).inc()
    logger.info("worker_ai_processing: forecast dispatch result=%s", result)
    return result


@router.post("/ai-processing/classification/dispatch")
async def dispatch_classification(_: InternalAuth, request: Request) -> Any:
    """Drain the deferred-classification queue now, regardless of EVENT_CLASSIFIER_AUTO_DISPATCH."""
    from app.ai.intelligence.event_classifier import EventClassifier
    from app.core.metrics import ai_processing_dispatch_total

    classifier = EventClassifier(use_llm=True)
    try:
        result = await classifier.flush_pending_classifications(
            _session_factory(request), _redis(request)
        )
    except RuntimeError as exc:
        ai_processing_dispatch_total.labels(
            category="classification", trigger_source="manual", outcome="error"
        ).inc()
        logger.warning("worker_ai_processing: classification dispatch failed — %s", exc)
        return JSONResponse(
            status_code=502,
            content={"detail": "dispatch_failed", "reason": str(exc)},
        )

    ai_processing_dispatch_total.labels(
        category="classification", trigger_source="manual", outcome="success"
    ).inc()
    logger.info("worker_ai_processing: classification dispatch result=%s", result)
    return result
