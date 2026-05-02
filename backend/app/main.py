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
    auth, cai, fusion, governance, hawk_eye, ingestion, intelligence,
    market_data, ml_drift, ml_predictions, paper_trading, safety, scanner,
    strategy, trade_suggestions, upstox, health, watchlist
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

setup_logging(log_level=settings.LOG_LEVEL, environment=settings.ENVIRONMENT)
logger = logging.getLogger(__name__)


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
        from app.ml.inference.registry_loader import RegistryModelLoader
        from app.ml.inference.ensemble_predictor import EnsemblePredictor
        from app.core.database import AsyncSessionLocal
        from app.core.redis import get_cache_service

        # Session is only needed for the DB query inside load_production_ensemble.
        # LoadedEnsemble stores plain scalars (no ORM objects), so it safely
        # outlives this session.
        async with AsyncSessionLocal() as session:
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

    # Start CAI WebSocket Redis listener — fans out all 4 CAI pub/sub channels
    # to connected browser clients via the in-process CAIConnectionManager.
    from app.api.v1.cai import cai_redis_listener
    cai_listener_task = asyncio.create_task(cai_redis_listener(), name="cai_redis_listener")
    app.state.cai_listener_task = cai_listener_task
    logger.info("CAI Redis listener started")

    # Start Paper Trading P&L recompute worker
    from app.services.paper_trading.pnl_worker import run_pnl_worker
    pnl_worker_task = asyncio.create_task(
        run_pnl_worker(get_redis()), name="paper_trading_pnl_worker"
    )
    app.state.pnl_worker_task = pnl_worker_task
    logger.info("Paper Trading P&L worker started")

    logger.info("All services initialized — ready")
    yield

    logger.info("Cortex AI shutting down")
    
    # Cleanup ML models
    if hasattr(app.state, "ml_predictor") and app.state.ml_predictor:
        logger.info("Cleaning up ML models...")
        app.state.ml_predictor = None
        app.state.ml_ensemble = None
    
    # Stop signal scheduler
    if hasattr(app.state, "signal_scheduler") and app.state.signal_scheduler:
        await app.state.signal_scheduler.stop()

    # Cancel CAI Redis listener
    if hasattr(app.state, "cai_listener_task") and app.state.cai_listener_task:
        app.state.cai_listener_task.cancel()
        try:
            await asyncio.wait_for(app.state.cai_listener_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    # Cancel P&L worker
    if hasattr(app.state, "pnl_worker_task") and app.state.pnl_worker_task:
        app.state.pnl_worker_task.cancel()
        try:
            await asyncio.wait_for(app.state.pnl_worker_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    await market_feed_service.stop()
    await upstox_client.stop()
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
    app.include_router(watchlist.router, prefix=f"{settings.API_V1_PREFIX}/watchlist", tags=["Watchlist"])
    app.include_router(market_data.router, prefix=f"{settings.API_V1_PREFIX}/market-data", tags=["Market Data"])
    app.include_router(scanner.router, prefix=f"{settings.API_V1_PREFIX}/scanner", tags=["Scanner"])
    app.include_router(upstox.router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox"])
    app.include_router(upstox.ws_router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox WebSocket"])
    app.include_router(hawk_eye.router, prefix=f"{settings.API_V1_PREFIX}/hawk-eye", tags=["HawkEye"])
    app.include_router(trade_suggestions.router, prefix=f"{settings.API_V1_PREFIX}/trade-suggestions", tags=["Trade Suggestions"])
    app.include_router(trade_suggestions.ws_router, prefix=f"{settings.API_V1_PREFIX}/trade-suggestions", tags=["Trade Suggestions WebSocket"])
    app.include_router(ml_predictions.router, prefix=f"{settings.API_V1_PREFIX}/ml", tags=["ML Predictions"])
    app.include_router(ml_drift.router, prefix=settings.API_V1_PREFIX, tags=["ML Drift"])
    app.include_router(paper_trading.router, prefix=f"{settings.API_V1_PREFIX}/paper-trading", tags=["Paper Trading"])
    app.include_router(paper_trading.ws_router, prefix=settings.API_V1_PREFIX, tags=["Paper Trading WebSocket"])
    
    # API routes - AI
    app.include_router(ingestion.router, prefix=f"{settings.API_V1_PREFIX}/ingestion", tags=["AI Ingestion"])
    app.include_router(intelligence.router, prefix=f"{settings.API_V1_PREFIX}/intelligence", tags=["AI Intelligence"])
    app.include_router(governance.router, prefix=f"{settings.API_V1_PREFIX}/governance", tags=["AI Governance"])
    app.include_router(strategy.router, prefix=f"{settings.API_V1_PREFIX}/strategy", tags=["AI Strategy"])
    app.include_router(fusion.router, prefix=f"{settings.API_V1_PREFIX}/fusion", tags=["AI Fusion"])
    app.include_router(safety.router, prefix=f"{settings.API_V1_PREFIX}/safety", tags=["AI Safety"])
    app.include_router(cai.router, prefix=f"{settings.API_V1_PREFIX}/cai", tags=["Cortex AI"])
    app.include_router(cai.ws_router, prefix=f"{settings.API_V1_PREFIX}/cai", tags=["Cortex AI WebSocket"])
    
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
        
        Checks if service can handle traffic by verifying critical dependencies.
        
        Returns:
            200: Ready to accept traffic
            503: Not ready (dependencies unavailable)
        """
        is_ready, status_data = await readiness_check()
        
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
