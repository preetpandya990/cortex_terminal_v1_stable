"""
Cortex AI — Unified Application Entry Point
============================================
Production-grade FastAPI with lifespan management, CORS, rate limiting, and middleware.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, Response
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1 import (
    admin_ai_processing, admin_strategies, admin_training, admin_users, admin_worker, auth,
    cai, fusion, fundamentals,
    governance, hawk_eye, ingestion, intelligence, market_data, ml_drift, ml_patterns,
    ml_predictions, paper_trading, safety, scanner, strategies, strategy, trade_suggestions,
    upstox, health, users, watchlist, ai_sentiment, ai_stream,
)
from app.core.config import get_settings
from app.core.database import engine, worker_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.limiter import limiter
from app.core.redis import close_redis, init_redis
from app.services.data_ingestion import DataIngestionService
from app.services.market_feed import MarketFeedService
from app.services.upstox_client import UpstoxClient

# Import circuit breaker to initialize metrics
from app.core import circuit_breaker

settings = get_settings()

# ── Logging ────────────────────────────────────────────────────────────────────
from app.core.logging_config import setup_logging

setup_logging(
    log_level=settings.LOG_LEVEL,
    environment=settings.ENVIRONMENT,
    explanation_log_enabled=settings.EXPLANATION_PIPELINE_LOG_ENABLED,
    explanation_log_path=settings.EXPLANATION_PIPELINE_LOG_PATH,
)
logger = logging.getLogger(__name__)


# ── ML hot-reload watcher ────────────────────────────────────────────────────
async def _model_reload_watcher(app: FastAPI) -> None:
    """
    Hot-reloads app.state.ml_predictor in place after a model promotion.

    Mirrors app.worker.model_reload_watcher: a fast pub/sub path reacts to
    ML_MODEL_PROMOTED immediately, and a 5-minute poll fallback covers the
    at-most-once nature of Redis pub/sub (a message during a restart/
    reconnect window would otherwise be lost forever). No task-supervisor
    framework exists on the API side, so this is a single asyncio.create_task
    loop cancelled at shutdown, same as cai_listener_task below.
    """
    from app.core.database import AsyncSessionLocal
    from app.core.redis import PubSubClient, RedisChannels, get_redis
    from app.ml.inference.registry_loader import RegistryModelLoader
    from app.models.ml_data import MLModelMetadata
    from sqlalchemy import select

    _POLL_INTERVAL_SECONDS = 300.0

    async def _production_fingerprint() -> dict[str, tuple[str, str | None]]:
        async with AsyncSessionLocal() as session:
            stmt = select(
                MLModelMetadata.model_name,
                MLModelMetadata.model_id,
                MLModelMetadata.checksum,
            ).where(
                MLModelMetadata.status == "production",
                MLModelMetadata.is_active == True,  # noqa: E712
            )
            rows = (await session.execute(stmt)).all()
        return {name: (model_id, checksum) for name, model_id, checksum in rows}

    async def _reload(reason: str) -> None:
        predictor = getattr(app.state, "ml_predictor", None)
        if predictor is None:
            return
        try:
            async with AsyncSessionLocal() as session:
                loader = RegistryModelLoader(session=session, num_threads=4)
                new_ensemble = await loader.load_production_ensemble(force_reload=True)
            await predictor.reload_from(new_ensemble)
            app.state.ml_ensemble = new_ensemble
            logger.info(
                "API model reload watcher: reloaded (%s) — XGBoost v%s + GRU v%s",
                reason, new_ensemble.xgboost_version, new_ensemble.gru_version,
            )
        except Exception as exc:
            logger.error(
                "API model reload watcher: reload failed (%s): %s", reason, exc, exc_info=True
            )

    async def _pubsub_path() -> None:
        pubsub = PubSubClient(get_redis())
        ps = await pubsub.subscribe(RedisChannels.ML_MODEL_PROMOTED)
        logger.info("API model reload watcher subscribed to %s", RedisChannels.ML_MODEL_PROMOTED)
        try:
            async for _message in pubsub.listen(ps):
                await _reload("pubsub")
        finally:
            try:
                await ps.unsubscribe(RedisChannels.ML_MODEL_PROMOTED)
                await ps.aclose()
            except Exception as exc:
                logger.warning("Error closing API model reload pub/sub connection: %s", exc)

    async def _poll_path() -> None:
        last_fingerprint: dict[str, tuple[str, str | None]] | None = None
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
            try:
                fingerprint = await _production_fingerprint()
                if last_fingerprint is not None and fingerprint != last_fingerprint:
                    await _reload("poll-drift")
                last_fingerprint = fingerprint
            except Exception as exc:
                logger.error("API model reload watcher: poll check failed: %s", exc, exc_info=True)

    try:
        await asyncio.gather(_pubsub_path(), _poll_path())
    except asyncio.CancelledError:
        logger.info("API model reload watcher cancelled")
        raise


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup: initialize services. Shutdown: graceful cleanup."""
    logger.info("Cortex AI starting [env=%s]", settings.ENVIRONMENT)

    # Initialize Prometheus metrics
    from app.core.metrics import init_metrics
    init_metrics(settings.APP_VERSION, settings.ENVIRONMENT)
    logger.info("Prometheus metrics initialized")

    await init_redis()

    # Initialize Kafka (Redpanda) — ensures the durable job topics exist and
    # starts the process-wide idempotent producer.  Idempotent across restarts
    # (TopicAlreadyExistsError is swallowed, mirroring the old BUSYGROUP path).
    from app.core.kafka import init_kafka
    await init_kafka()
    logger.info("Kafka topics and producer initialized")

    # Initialize Gemini Request Manager — central coordinator for all 6 Gemini callers.
    # Pre-loads quota circuit state from Redis so a post-exhaustion restart fast-fails
    # immediately rather than wasting an API round-trip.  Must run AFTER init_redis()
    # (circuit pre-load needs Redis) and BEFORE the LLM client (client calls acquire()).
    try:
        from app.ai.intelligence.request_manager import GeminiRequestManager
        from app.ai.intelligence.llm_client import _key_id as _gemini_key_id
        from app.core.redis import get_redis
        _gemini_key_ids = [_gemini_key_id(k) for k in get_settings().gemini_api_key_pool]
        await GeminiRequestManager.initialize(redis=get_redis(), key_ids=_gemini_key_ids)
        logger.info("Gemini request manager initialized (keys=%d)", len(_gemini_key_ids))
    except Exception as exc:
        logger.error(
            "Gemini request manager initialization failed (quota coordination degraded): %s", exc
        )

    # Initialize LLM Intelligence Client — probes NIM then Ollama, logs active backend
    try:
        from app.ai.intelligence.llm_client import CortexIntelligenceClient
        await CortexIntelligenceClient.initialize()
    except Exception as exc:
        logger.error(
            "Intelligence client initialization failed (LLM features degraded): %s", exc
        )

    # Initialize NLP engine — LLM-backed sentiment analysis via CortexIntelligenceClient
    try:
        from app.ai.intelligence.nlp_engine import NLPEngine
        await NLPEngine.initialize()
        logger.info("NLP engine initialized")
    except Exception as exc:
        logger.warning("NLP engine initialization failed (sentiment analysis degraded): %s", exc)

    upstox_client = UpstoxClient()
    await upstox_client.start()
    app.state.upstox_client = upstox_client
    if settings.UPSTOX_ACCESS_TOKEN:
        upstox_client.set_access_token(settings.UPSTOX_ACCESS_TOKEN)
        logger.info("Loaded Upstox access token")

    from app.core.redis import get_redis
    market_feed_service = MarketFeedService(
        upstox_client=upstox_client,
        redis=get_redis(),
        throttle_ms=settings.MARKET_FEED_THROTTLE_MS,
    )
    await market_feed_service.start()
    app.state.market_feed_service = market_feed_service

    ingestion_service = DataIngestionService(upstox_client=upstox_client)
    app.state.ingestion_service = ingestion_service

    # Load production ML models from registry
    app.state.ml_ensemble  = None
    app.state.ml_predictor = None
    try:
        from app.ml.inference.registry_loader import RegistryModelLoader, bootstrap_production_models
        from app.ml.inference.ensemble_predictor import EnsemblePredictor
        from app.core.database import AsyncSessionLocal
        from app.core.redis import get_cache_service

        # Session is only needed for DB queries; LoadedEnsemble stores plain
        # scalars so it safely outlives this session.
        async with AsyncSessionLocal() as session:
            await bootstrap_production_models(session)
            loader   = RegistryModelLoader(session=session, num_threads=4, use_gpu=False)
            ensemble = await loader.load_production_ensemble()

        predictor = EnsemblePredictor.from_loaded_ensemble(
            ensemble,
            cache=get_cache_service(),
        )
        app.state.ml_ensemble  = ensemble
        app.state.ml_predictor = predictor

        logger.info(
            "Production ML models loaded: XGBoost v%s + GRU v%s",
            ensemble.xgboost_version, ensemble.gru_version,
        )
    except Exception as exc:
        logger.error("Failed to load production ML models: %s", exc, exc_info=True)
        if settings.is_production:
            raise RuntimeError(f"Critical: failed to load production ML models: {exc}") from exc
        logger.warning("Continuing without ML models (non-production environment)")

    # Model hot-reload watcher — picks up promotions without a restart.
    app.state.model_reload_watcher_task = None
    if app.state.ml_predictor is not None:
        app.state.model_reload_watcher_task = asyncio.create_task(
            _model_reload_watcher(app), name="model_reload_watcher"
        )
        logger.info("API model reload watcher started")

    # Signal Scheduler — automated generation for Nifty 50 + Next 50 every 15 min
    from app.services.signal_scheduler import SignalScheduler
    from app.core.redis import get_pubsub_client, get_redis

    signal_scheduler = SignalScheduler(
        db_factory=AsyncSessionLocal,
        ml_predictor=app.state.ml_predictor,
        pubsub=get_pubsub_client(),
        redis=get_redis(),
        scheduled_universe=settings.SIGNAL_SCHEDULED_UNIVERSE,
    )
    await signal_scheduler.start()
    app.state.signal_scheduler = signal_scheduler

    # Register SignalPipeline with the scheduler injected so that the
    # event-driven on-demand trigger fires for high-impact classifications.
    from app.ai.fusion.signal_pipeline import SignalPipeline
    app.state.signal_pipeline = SignalPipeline(scheduler=signal_scheduler)

    # Instrument Sync Service — daily reconcile of instrument_master against the
    # Upstox BOD file (soft-deletes delistings) + startup catch-up if stale.
    from app.services.instrument_sync_service import InstrumentSyncService

    instrument_sync_service = InstrumentSyncService(
        db_factory=AsyncSessionLocal,
        redis=get_redis(),
    )
    await instrument_sync_service.start()
    app.state.instrument_sync_service = instrument_sync_service

    # Start CAI WebSocket Redis listener — fans out all 4 CAI pub/sub channels
    # to connected browser clients via the in-process CAIConnectionManager.
    from app.api.v1.cai import cai_redis_listener
    cai_listener_task = asyncio.create_task(cai_redis_listener(), name="cai_redis_listener")
    app.state.cai_listener_task = cai_listener_task
    logger.info("CAI Redis listener started")

    # Start Trade Suggestions WebSocket Redis listener — persistent singleton that
    # bridges Redis pub/sub to connected suggestion WebSocket clients.
    from app.api.v1.trade_suggestions import suggestions_redis_listener
    suggestions_listener_task = asyncio.create_task(
        suggestions_redis_listener(), name="suggestions_redis_listener"
    )
    app.state.suggestions_listener_task = suggestions_listener_task
    logger.info("Trade Suggestions Redis listener started")

    # Initialise WorkerClient — fail-open HTTP client for the worker sidecar
    # control-plane.  The pnl_worker and sl_tp_worker tasks now run inside the
    # worker sidecar; this client enables pause/resume/trigger/restart of all
    # 13 background tasks from the main API without coupling the processes.
    from app.core.worker_client import WorkerClient
    worker_client = WorkerClient()
    app.state.worker_client = worker_client
    logger.info("Worker sidecar client initialized (base_url=%s)", settings.WORKER_BASE_URL)

    # Start LLM Explanation Workers — two parallel consumers on the explanation
    # jobs topic (2 partitions) + one context worker, all using manual-commit
    # Kafka consumer groups for at-least-once delivery.  Two explanation workers
    # run in parallel so a slow Gemini call for one suggestion does not block
    # all subsequent jobs.
    from app.ai.intelligence.explanation_worker import context_worker, explanation_worker
    explanation_worker_tasks = [
        asyncio.create_task(
            explanation_worker(worker_id=0), name="llm_explanation_worker_0"
        ),
        asyncio.create_task(
            explanation_worker(worker_id=1), name="llm_explanation_worker_1"
        ),
    ]
    context_worker_task = asyncio.create_task(
        context_worker(), name="llm_context_worker"
    )
    app.state.explanation_worker_tasks = explanation_worker_tasks
    app.state.context_worker_task = context_worker_task
    logger.info("LLM explanation workers started (2 explanation + 1 context)")

    # Start RAG corpus refresh worker — incrementally embeds new news events
    # (ai_raw_events → ai_document_embeddings) on startup and every
    # RAG_REFRESH_INTERVAL_HOURS hours.  Paced, resumable, and Redis-locked so
    # at most one backfill runs at a time across all processes.
    from app.ai.rag.backfill_service import run_rag_refresh_worker
    rag_refresh_task = asyncio.create_task(
        run_rag_refresh_worker(get_redis()), name="rag_corpus_refresh"
    )
    app.state.rag_refresh_task = rag_refresh_task
    logger.info(
        "RAG corpus refresh worker started (interval=%dh)",
        settings.RAG_REFRESH_INTERVAL_HOURS,
    )

    logger.info("All services initialized — ready")
    yield

    logger.info("Cortex AI shutting down")

    # Cancel model reload watcher
    if hasattr(app.state, "model_reload_watcher_task") and app.state.model_reload_watcher_task:
        app.state.model_reload_watcher_task.cancel()
        try:
            await asyncio.wait_for(app.state.model_reload_watcher_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Cleanup ML models
    if hasattr(app.state, "ml_predictor") and app.state.ml_predictor:
        logger.info("Cleaning up ML models...")
        app.state.ml_predictor = None
        app.state.ml_ensemble = None
    
    # Stop signal scheduler
    if hasattr(app.state, "signal_scheduler") and app.state.signal_scheduler:
        await app.state.signal_scheduler.stop()

    # Stop instrument sync service
    if hasattr(app.state, "instrument_sync_service") and app.state.instrument_sync_service:
        await app.state.instrument_sync_service.stop()

    # Cancel CAI Redis listener
    if hasattr(app.state, "cai_listener_task") and app.state.cai_listener_task:
        app.state.cai_listener_task.cancel()
        try:
            await asyncio.wait_for(app.state.cai_listener_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Cancel Trade Suggestions Redis listener
    if hasattr(app.state, "suggestions_listener_task") and app.state.suggestions_listener_task:
        app.state.suggestions_listener_task.cancel()
        try:
            await asyncio.wait_for(app.state.suggestions_listener_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Close the WorkerClient connection pool
    if hasattr(app.state, "worker_client") and app.state.worker_client:
        await app.state.worker_client.aclose()

    # Cancel LLM explanation workers (2 explanation + 1 context)
    for _task in getattr(app.state, "explanation_worker_tasks", []):
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    if hasattr(app.state, "context_worker_task") and app.state.context_worker_task:
        app.state.context_worker_task.cancel()
        try:
            await asyncio.wait_for(app.state.context_worker_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Cancel RAG corpus refresh worker — 10s timeout to allow an in-flight embed
    # API call to be interrupted and the lock/metrics to be cleaned up cleanly.
    if hasattr(app.state, "rag_refresh_task") and app.state.rag_refresh_task:
        app.state.rag_refresh_task.cancel()
        try:
            await asyncio.wait_for(app.state.rag_refresh_task, timeout=10.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Drain NLP sentiment batch queue — resolves all pending futures with neutral
    # fallback before the Gemini transport is torn down.  Must run BEFORE
    # GeminiRequestManager.aclose() so event-pipeline coroutines receive a clean
    # fallback dict rather than a transport error from a closed LLM client.
    try:
        from app.ai.intelligence.nlp_engine import NLPEngine
        await NLPEngine.aclose()
    except Exception as exc:
        logger.debug("NLP engine close failed (non-fatal): %s", exc)

    # Shut down Gemini Request Manager — drains the permit queue and cancels all waiting
    # callers before the LLM transport is closed.  Order matters: callers must receive
    # GeminiRateLimitError (not a transport error) if they are still queued at shutdown.
    try:
        from app.ai.intelligence.request_manager import GeminiRequestManager
        if GeminiRequestManager._instance is not None:
            await GeminiRequestManager._instance.aclose()
    except Exception as exc:
        logger.debug("Gemini request manager close failed (non-fatal): %s", exc)

    # Close the Gemini LLM client transport
    try:
        from app.ai.intelligence.llm_client import close_intelligence_client
        await close_intelligence_client()
    except Exception as exc:
        logger.debug("Gemini client close failed (non-fatal): %s", exc)

    await market_feed_service.stop()
    await upstox_client.stop()

    # Close Kafka AFTER all consumer tasks above are cancelled (a live consumer
    # must never outlive the shared clients) and BEFORE Redis — consumer
    # processing paths write SSE/pub-sub state through Redis until they stop.
    from app.core.kafka import close_kafka
    await close_kafka()

    await close_redis()
    await engine.dispose()
    await worker_engine.dispose()
    logger.info("Shutdown complete")


# ── Application factory ────────────────────────────────────────────────────────
def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url=settings.docs_url,
        redoc_url=settings.redoc_url,
        lifespan=lifespan,
    )

    # Middleware (outermost first)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["*"] if not settings.is_production else ["api.cortex.in", "*.cortex.in"],
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_str,
        allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"] if not settings.is_production else ["Authorization", "Content-Type", "X-Request-ID", "X-Correlation-ID"],
    )
    
    # Add request ID middleware (for structured logging and tracing)
    from app.middleware.request_id import RequestIDMiddleware
    app.add_middleware(RequestIDMiddleware)
    
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SlowAPIMiddleware)
    
    # Add metrics middleware
    from app.middleware.metrics import MetricsMiddleware
    app.add_middleware(MetricsMiddleware)

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    register_exception_handlers(app)

    # API routes - ML
    app.include_router(auth.router, prefix=f"{settings.API_V1_PREFIX}/auth", tags=["Authentication"])
    app.include_router(admin_users.router, prefix=f"{settings.API_V1_PREFIX}/admin", tags=["Admin — User Management"])
    app.include_router(admin_strategies.router, prefix=f"{settings.API_V1_PREFIX}/admin/strategies", tags=["Admin — Strategy Governance"])
    app.include_router(admin_training.router,    prefix=f"{settings.API_V1_PREFIX}/admin/training",   tags=["Admin — ML Training Console"])
    app.include_router(admin_worker.router,      prefix=f"{settings.API_V1_PREFIX}/admin/worker",     tags=["Admin — Worker Control"])
    app.include_router(admin_ai_processing.router, prefix=f"{settings.API_V1_PREFIX}/admin/ai-processing", tags=["Admin — AI Processing Queue"])
    app.include_router(admin_training.ws_router, prefix=f"{settings.API_V1_PREFIX}/admin/training",   tags=["Admin — ML Training WebSocket"])
    app.include_router(watchlist.router, prefix=f"{settings.API_V1_PREFIX}/watchlist", tags=["Watchlist"])
    app.include_router(market_data.router, prefix=f"{settings.API_V1_PREFIX}/market-data", tags=["Market Data"])
    app.include_router(scanner.router, prefix=f"{settings.API_V1_PREFIX}/scanner", tags=["Scanner"])
    app.include_router(upstox.router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox"])
    app.include_router(upstox.ws_router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox WebSocket"])
    app.include_router(hawk_eye.router, prefix=f"{settings.API_V1_PREFIX}/hawk-eye", tags=["HawkEye"])
    app.include_router(trade_suggestions.router, prefix=f"{settings.API_V1_PREFIX}/trade-suggestions", tags=["Trade Suggestions"])
    app.include_router(trade_suggestions.ws_router, prefix=f"{settings.API_V1_PREFIX}/trade-suggestions", tags=["Trade Suggestions WebSocket"])
    app.include_router(ml_predictions.router, prefix=f"{settings.API_V1_PREFIX}/ml", tags=["ML Predictions"])
    app.include_router(ml_patterns.router, prefix=settings.API_V1_PREFIX, tags=["ML Pattern Analysis"])
    app.include_router(ml_drift.router, prefix=settings.API_V1_PREFIX, tags=["ML Drift"])
    app.include_router(paper_trading.router, prefix=f"{settings.API_V1_PREFIX}/paper-trading", tags=["Paper Trading"])
    app.include_router(paper_trading.ws_router, prefix=settings.API_V1_PREFIX, tags=["Paper Trading WebSocket"])
    
    # API routes - AI Analysis Cards
    app.include_router(ai_sentiment.router, prefix=settings.API_V1_PREFIX, tags=["AI Sentiment Analysis"])
    app.include_router(ai_stream.router, prefix=settings.API_V1_PREFIX, tags=["AI Analysis Stream"])

    # API routes - AI
    app.include_router(ingestion.router, prefix=f"{settings.API_V1_PREFIX}/ingestion", tags=["AI Ingestion"])
    app.include_router(intelligence.router, prefix=f"{settings.API_V1_PREFIX}/intelligence", tags=["AI Intelligence"])
    app.include_router(governance.router, prefix=f"{settings.API_V1_PREFIX}/governance", tags=["AI Governance"])
    app.include_router(strategy.router, prefix=f"{settings.API_V1_PREFIX}/strategy", tags=["AI Strategy"])
    app.include_router(strategies.router, prefix=f"{settings.API_V1_PREFIX}/strategies", tags=["Trading Strategies"])
    app.include_router(users.router, prefix=f"{settings.API_V1_PREFIX}/users", tags=["User Profile"])
    app.include_router(fusion.router, prefix=f"{settings.API_V1_PREFIX}/fusion", tags=["AI Fusion"])
    app.include_router(safety.router, prefix=f"{settings.API_V1_PREFIX}/safety", tags=["AI Safety"])
    app.include_router(cai.router, prefix=f"{settings.API_V1_PREFIX}/cai", tags=["Cortex AI"])
    app.include_router(cai.ws_router, prefix=f"{settings.API_V1_PREFIX}/cai", tags=["Cortex AI WebSocket"])
    
    # Company Fundamentals
    app.include_router(
        fundamentals.router,
        prefix=f"{settings.API_V1_PREFIX}/fundamentals",
        tags=["Company Fundamentals"],
    )

    # Health
    app.include_router(health.router, prefix=settings.API_V1_PREFIX, tags=["Health"])

    # Production health check endpoints (Kubernetes-compatible)
    from app.core.health_checks import liveness_check, readiness_check, detailed_check
    from fastapi import status as http_status

    @app.get("/health", include_in_schema=False)
    async def health_liveness() -> dict[str, str]:
        """
        Liveness probe for Kubernetes.
        
        Checks if the process is alive and responsive.
        Does NOT check external dependencies (DB, Redis).
        
        Returns:
            Always 200 unless process is broken
        """
        return await liveness_check()

    @app.get("/health/ready", include_in_schema=False)
    async def health_readiness() -> JSONResponse:
        """
        Readiness probe for Kubernetes.

        Checks critical dependencies (DB + Redis).  The worker sidecar is
        checked as a non-critical component: if unreachable the API stays
        ready (fail-open) but the response body reflects the degraded state.

        Returns:
            200: Ready to accept traffic
            503: Not ready (DB or Redis unavailable)
        """
        from app.core.health_checks import check_worker
        is_ready, status_data = await readiness_check()

        # Worker health is non-critical: a restarting worker must not take the
        # API out of service rotation.  Add the check for visibility only.
        if hasattr(app.state, "worker_client") and app.state.worker_client:
            from app.core.health_checks import HealthStatus
            # Reconstruct a HealthStatus so we can add the worker check with
            # the correct critical=False flag, then re-derive is_ready.
            result = HealthStatus()
            result.status = status_data.get("status", HealthStatus.HEALTHY)
            result.checks = status_data.get("checks", {})
            result.timestamp = status_data.get("timestamp", "")
            worker_healthy, worker_details = await check_worker(
                app.state.worker_client, timeout=1.0
            )
            result.add_check("worker", worker_healthy, worker_details, critical=False)
            # Ready if status is "healthy" or "degraded" (worker non-critical).
            # "unhealthy" only when a critical check (DB/Redis) failed above.
            is_ready = result.status != HealthStatus.UNHEALTHY
            status_data = result.to_dict()

        return JSONResponse(
            status_code=http_status.HTTP_200_OK if is_ready else http_status.HTTP_503_SERVICE_UNAVAILABLE,
            content=status_data
        )

    @app.get("/health/detailed", include_in_schema=False)
    async def health_detailed() -> dict[str, Any]:
        """
        Detailed health check for monitoring and debugging.
        
        Provides comprehensive status of all components.
        
        Returns:
            Detailed status including all checks
        """
        return await detailed_check()

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        """Prometheus metrics endpoint."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()
