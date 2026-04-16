"""
Cortex AI Worker Process - Background Task Orchestration

Production-grade worker process managing 5 concurrent background loops:
- RSS ingestion (news feeds)
- Regime detection (market conditions)
- Drift monitoring (model performance)
- Safety monitoring (risk management)
- Heartbeat (health check)

Graceful shutdown with SIGTERM handling and 30s timeout.
"""
import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.ingestion.rss_fetcher import rss_ingestion_loop
from app.ai.intelligence.event_processor import event_processing_loop
from app.ai.safety.safety_trigger_engine import safety_monitoring_loop
from app.ai.strategy.regime_detector import regime_detection_loop
from app.core.config import get_settings
from app.core.redis import init_redis, close_redis, get_cache_service
from app.ml.monitoring.drift_scheduler import drift_detection_loop

logger = logging.getLogger(__name__)
settings = get_settings()

# Global shutdown event
shutdown_event = asyncio.Event()


def signal_handler(signum: int, frame) -> None:
    """
    Handle SIGTERM/SIGINT signals for graceful shutdown.
    
    Args:
        signum: Signal number
        frame: Current stack frame
    """
    sig_name = signal.Signals(signum).name
    logger.info(f"Received {sig_name}, initiating graceful shutdown...")
    shutdown_event.set()


@asynccontextmanager
async def worker_lifespan() -> AsyncGenerator[tuple[async_sessionmaker, AsyncSession, dict], None]:
    """
    Worker lifespan context manager for resource initialization and cleanup.
    
    Yields:
        Tuple of (session_factory, redis_client, ml_components)
    """
    logger.info("Initializing worker resources...")
    
    # Create worker database engine (separate from API)
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        echo=False,
    )
    
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    # Initialize Redis
    await init_redis()
    redis_client = get_cache_service()
    
    # Initialize ML components (ensemble predictor + feature loader)
    ml_components = {}
    try:
        from pathlib import Path
        from app.ml.inference import create_ensemble_predictor, create_feature_loader
        
        # Check if models exist
        xgb_path = Path("models/production/onnx/xgboost_final.onnx")
        gru_path = Path("models/production/onnx/gru_final.onnx")
        
        if xgb_path.exists() and gru_path.exists():
            logger.info("Initializing ML ensemble predictor...")
            
            # Create ensemble predictor
            ensemble_predictor = create_ensemble_predictor(
                xgboost_path=xgb_path,
                gru_path=gru_path,
                cache=redis_client,
                xgboost_weight=0.6,
                gru_weight=0.4,
                num_threads=4,
                use_gpu=False,  # CPU for worker (GPU for API if needed)
            )
            
            # Create feature loader (needs a session for DB access)
            async with session_factory() as temp_session:
                feature_loader = create_feature_loader(
                    db=temp_session,
                    redis=redis_client._redis,  # Access underlying Redis client
                    sequence_length=60,
                    n_features=47,
                )
            
            ml_components = {
                "ensemble_predictor": ensemble_predictor,
                "feature_loader": feature_loader,
            }
            
            logger.info("✓ ML components initialized successfully")
            logger.info(f"  Models: {xgb_path.name}, {gru_path.name}")
            logger.info(f"  Weights: XGBoost=60%, GRU=40%")
        else:
            logger.warning("ML models not found, predictions will be disabled")
            logger.warning(f"  XGBoost: {xgb_path} (exists: {xgb_path.exists()})")
            logger.warning(f"  GRU: {gru_path} (exists: {gru_path.exists()})")
    
    except Exception as e:
        logger.error(f"Failed to initialize ML components: {e}", exc_info=True)
        logger.warning("Worker will continue without ML predictions")
    
    logger.info("Worker resources initialized successfully")
    
    try:
        yield session_factory, redis_client, ml_components
    finally:
        logger.info("Cleaning up worker resources...")
        await close_redis()
        await engine.dispose()
        logger.info("Worker resources cleaned up")


async def heartbeat_loop() -> None:
    """
    Heartbeat loop - writes timestamp to Redis every 30s.
    
    Allows monitoring systems to detect worker health.
    Key: worker:heartbeat, TTL: 60s
    """
    logger.info("Heartbeat loop started")
    redis_client = get_cache_service()
    
    try:
        while not shutdown_event.is_set():
            try:
                timestamp = datetime.utcnow().isoformat()
                await redis_client.set("worker:heartbeat", timestamp, ttl=60)
                logger.debug(f"Heartbeat: {timestamp}")
            except Exception as e:
                logger.error(f"Heartbeat error: {e}", exc_info=True)
            
            # Sleep 30s or until shutdown
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=30.0
                )
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled")
        raise
    finally:
        logger.info("Heartbeat loop stopped")


async def main() -> None:
    """
    Main worker orchestration function.
    
    Spawns 5 concurrent background tasks and manages graceful shutdown.
    """
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    logger.info("=" * 80)
    logger.info("Cortex AI Worker Process Starting")
    logger.info(f"Environment: {settings.ENVIRONMENT}")
    logger.info(f"Background tasks enabled: {settings.ENABLE_BACKGROUND_TASKS}")
    logger.info(f"Shutdown timeout: {settings.WORKER_SHUTDOWN_TIMEOUT}s")
    logger.info("=" * 80)
    
    if not settings.ENABLE_BACKGROUND_TASKS:
        logger.warning("Background tasks disabled, worker exiting")
        return
    
    async with worker_lifespan() as (session_factory, redis_client, ml_components):
        # Extract ML components
        ensemble_predictor = ml_components.get("ensemble_predictor")
        feature_loader = ml_components.get("feature_loader")
        
        # Create 6 background tasks
        tasks = [
            asyncio.create_task(rss_ingestion_loop(session_factory), name="rss_ingestion"),
            asyncio.create_task(
                event_processing_loop(
                    session_factory,
                    ensemble_predictor=ensemble_predictor,
                    feature_loader=feature_loader,
                ),
                name="event_processing"
            ),
            asyncio.create_task(regime_detection_loop(session_factory), name="regime_detection"),
            asyncio.create_task(drift_detection_loop(session_factory), name="drift_detection"),
            asyncio.create_task(safety_monitoring_loop(session_factory), name="safety_monitoring"),
            asyncio.create_task(heartbeat_loop(), name="heartbeat"),
        ]
        
        logger.info(f"Started {len(tasks)} background tasks")
        for task in tasks:
            logger.info(f"  - {task.get_name()}")
        
        # Wait for shutdown signal
        await shutdown_event.wait()
        
        logger.info("Shutdown signal received, cancelling tasks...")
        
        # Cancel all tasks
        for task in tasks:
            task.cancel()
        
        # Wait for tasks to complete with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=settings.WORKER_SHUTDOWN_TIMEOUT
            )
            logger.info("All tasks completed gracefully")
        except asyncio.TimeoutError:
            logger.error(f"Tasks did not complete within {settings.WORKER_SHUTDOWN_TIMEOUT}s timeout")
            # Force kill remaining tasks
            for task in tasks:
                if not task.done():
                    logger.warning(f"Force killing task: {task.get_name()}")
                    task.cancel()
        
        logger.info("Worker process shutdown complete")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as e:
        logger.error(f"Worker crashed: {e}", exc_info=True)
        raise
