"""
Cortex AI — Unified Application Entry Point
============================================
Production-grade FastAPI with lifespan management, CORS, rate limiting, and middleware.
"""
from __future__ import annotations

import logging
import logging.config
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.v1 import (
    auth, cai, fusion, governance, hawk_eye, ingestion, intelligence,
    market_data, ml_drift, ml_predictions, safety, scanner, strategy, upstox, health
)
from app.core.config import get_settings
from app.core.database import engine, worker_engine
from app.core.exception_handlers import register_exception_handlers
from app.core.limiter import limiter
from app.core.redis import close_redis, init_redis
from app.services.data_ingestion import DataIngestionService
from app.services.tick_stream import TickStreamService
from app.services.upstox_client import UpstoxClient

settings = get_settings()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.config.dictConfig(
    {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(name)s %(levelname)s %(message)s",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
            }
        },
        "root": {"level": settings.LOG_LEVEL, "handlers": ["console"]},
    }
)

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

    tick_service = TickStreamService(upstox_client=upstox_client)
    app.state.tick_service = tick_service

    ingestion_service = DataIngestionService(upstox_client=upstox_client)
    app.state.ingestion_service = ingestion_service

    # Pre-load ML prediction engine for faster inference
    try:
        from app.ml.inference import create_prediction_engine
        from app.core.redis import get_cache_service
        cache = get_cache_service()
        prediction_engine = create_prediction_engine(
            model_path="models/production/model_quantized.onnx",
            cache=cache,
            num_threads=4,
        )
        app.state.prediction_engine = prediction_engine
        logger.info("ML prediction engine pre-loaded")
    except Exception as e:
        logger.warning(f"Could not pre-load prediction engine: {e}")
        app.state.prediction_engine = None

    logger.info("All services initialized — ready")
    yield

    logger.info("Cortex AI shutting down")
    await tick_service.disconnect()
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
        allow_headers=["*"] if not settings.is_production else ["Authorization", "Content-Type", "X-Request-ID"],
    )
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
    app.include_router(market_data.router, prefix=f"{settings.API_V1_PREFIX}/market-data", tags=["Market Data"])
    app.include_router(scanner.router, prefix=f"{settings.API_V1_PREFIX}/scanner", tags=["Scanner"])
    app.include_router(upstox.router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox"])
    app.include_router(upstox.ws_router, prefix=f"{settings.API_V1_PREFIX}/upstox", tags=["Upstox WebSocket"])
    app.include_router(hawk_eye.router, prefix=f"{settings.API_V1_PREFIX}/hawk-eye", tags=["HawkEye"])
    app.include_router(ml_predictions.router, prefix=f"{settings.API_V1_PREFIX}/ml", tags=["ML Predictions"])
    app.include_router(ml_drift.router, prefix=settings.API_V1_PREFIX, tags=["ML Drift"])
    
    # API routes - AI
    app.include_router(ingestion.router, prefix=f"{settings.API_V1_PREFIX}/ingestion", tags=["AI Ingestion"])
    app.include_router(intelligence.router, prefix=f"{settings.API_V1_PREFIX}/intelligence", tags=["AI Intelligence"])
    app.include_router(governance.router, prefix=f"{settings.API_V1_PREFIX}/governance", tags=["AI Governance"])
    app.include_router(strategy.router, prefix=f"{settings.API_V1_PREFIX}/strategy", tags=["AI Strategy"])
    app.include_router(fusion.router, prefix=f"{settings.API_V1_PREFIX}/fusion", tags=["AI Fusion"])
    app.include_router(safety.router, prefix=f"{settings.API_V1_PREFIX}/safety", tags=["AI Safety"])
    app.include_router(cai.router, prefix=f"{settings.API_V1_PREFIX}/cai", tags=["Cortex AI"])
    
    # Health
    app.include_router(health.router, prefix=settings.API_V1_PREFIX, tags=["Health"])

    @app.get("/health", include_in_schema=False)
    async def basic_health() -> dict[str, str]:
        return {"status": "ok", "version": settings.APP_VERSION}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        """Prometheus metrics endpoint."""
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        from fastapi.responses import Response
        
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()
