"""
Cortex AI Worker Process - Background Task Orchestration

Production-grade worker process managing 11 concurrent background loops:
- RSS ingestion (news feeds)
- Event processing (ML predictions)
- Regime detection (market conditions)
- Drift monitoring (model performance)
- Safety monitoring (risk management)
- Data ingestion (OHLCV market data)
- Heartbeat (health check)
- Correlation engine (trade suggestions)
- Suggestion expiry (TTL management)
- Cache invalidation (real-time cache consistency)
- Fundamentals refresh (key-ratios, corp-actions, financials, holdings, nightly universe)

Graceful shutdown with SIGTERM handling and 30s timeout.
"""
import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.ai.correlation.engine import EventCorrelationEngine
from app.ai.fusion.models import AIEventClassification
from app.ai.ingestion.rss_fetcher import rss_ingestion_loop
from app.ai.intelligence.event_processor import event_processing_loop
from app.ai.safety.safety_trigger_engine import safety_monitoring_loop
from app.ai.strategy.regime_detector import regime_detection_loop
from app.core.config import get_settings
from app.core.redis import init_redis, close_redis, get_cache_service
from app.ml.monitoring.drift_scheduler import drift_detection_loop
from app.models.trade_suggestions import TradeSuggestion
from app.services.data_ingestion_worker import data_ingestion_loop
from app.services.fundamentals_refresh import FundamentalsRefreshScheduler
from app.services.upstox_client import UpstoxClient

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
async def worker_lifespan() -> AsyncGenerator[tuple[async_sessionmaker, AsyncSession, dict, UpstoxClient], None]:
    """
    Worker lifespan context manager for resource initialization and cleanup.
    
    Yields:
        Tuple of (session_factory, redis_client, ml_components, upstox_client)
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
    
    # Initialize Upstox client
    upstox_client = UpstoxClient()
    await upstox_client.start()
    
    # Initialize Redis
    await init_redis()
    redis_client = get_cache_service()
    
    # Initialize ML components via registry (same path as the API)
    ml_components = {}
    try:
        from app.ml.inference.registry_loader import RegistryModelLoader
        from app.ml.inference.ensemble_predictor import EnsemblePredictor

        async with session_factory() as session:
            loader   = RegistryModelLoader(session=session, num_threads=4)
            ensemble = await loader.load_production_ensemble()

        predictor = EnsemblePredictor.from_loaded_ensemble(ensemble, cache=redis_client)
        ml_components = {
            "ensemble_predictor":        predictor,
            "ensemble_sequence_length":  ensemble.sequence_length,
            "ensemble_n_features":       ensemble.n_features,
            "ensemble_feature_names":    ensemble.feature_names,  # tuple[str, ...]
        }

        logger.info(
            "ML components initialized: XGBoost v%s (%.0f%%) + GRU v%s (%.0f%%) | "
            "features=%d sequence_len=%d",
            ensemble.xgboost_version, ensemble.xgboost_weight * 100,
            ensemble.gru_version,     ensemble.gru_weight    * 100,
            ensemble.n_features,      ensemble.sequence_length,
        )
    except Exception as exc:
        logger.error("Failed to initialize ML components: %s", exc, exc_info=True)
        logger.warning(
            "Worker will continue WITHOUT ML predictions — "
            "all correlation attempts will be rejected with ML_NEUTRAL"
        )
    
    # ── Startup verification: log exactly which ML components are present ────
    _REQUIRED_ML_KEYS = {
        "ensemble_predictor",
        "ensemble_sequence_length",
        "ensemble_n_features",
        "ensemble_feature_names",
    }
    _missing = _REQUIRED_ML_KEYS - ml_components.keys()
    if _missing:
        logger.warning(
            "Worker starting with INCOMPLETE ML components — missing: %s. "
            "Correlations will be rejected at the ML_NEUTRAL gate until these "
            "are available. Check ML registry and model loader logs above.",
            sorted(_missing),
        )
    else:
        logger.info(
            "ML component verification passed — all %d required keys present",
            len(_REQUIRED_ML_KEYS),
        )

    logger.info("Worker resources initialized successfully")

    try:
        yield session_factory, redis_client, ml_components, upstox_client
    finally:
        logger.info("Cleaning up worker resources...")
        await upstox_client.stop()
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
                from datetime import timezone as _tz
                timestamp = datetime.now(_tz.utc).isoformat()
                await redis_client.set("worker:heartbeat", timestamp, ttl=60)
                logger.debug("Heartbeat: %s", timestamp)
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


async def cache_invalidation_loop(redis_client) -> None:
    """
    Cache invalidation loop - subscribes to SUGGESTIONS_NEW and invalidates list caches.
    
    Production-grade cache invalidation subscriber with:
    - Redis pub/sub subscription to cai:suggestions:new
    - Pattern-based cache invalidation (suggestions:list:*)
    - Prometheus metrics tracking
    - Structured logging with context
    - Graceful shutdown handling
    
    Cadence: Real-time (event-driven via Redis pub/sub)
    Invalidation pattern: suggestions:list:* (all list caches)
    Metrics: api_cache_invalidations_total counter
    
    Best Practices (2026):
    - Event-driven invalidation for real-time consistency
    - Pattern-based deletion for hierarchical cache keys
    - Fire-and-forget pub/sub for low latency
    - Metrics track invalidation rate and deleted key count
    """
    logger.info("Cache invalidation loop started")
    
    from app.core.cache_decorator import invalidate_cache_pattern
    from app.core.metrics import api_cache_invalidations_total
    from app.core.redis import RedisChannels, PubSubClient
    
    pubsub = PubSubClient(redis_client._redis)
    
    try:
        # Subscribe to SUGGESTIONS_NEW channel
        ps = await pubsub.subscribe(RedisChannels.SUGGESTIONS_NEW)
        logger.info(f"Subscribed to {RedisChannels.SUGGESTIONS_NEW} for cache invalidation")
        
        # Listen for new suggestion events
        async for message in pubsub.listen(ps):
            try:
                channel = message["channel"]
                data = message["data"]
                
                suggestion_id = data.get("suggestion_id")
                symbol = data.get("symbol", "UNKNOWN")
                
                logger.debug(
                    f"[Cache Invalidation] Received new suggestion event: {suggestion_id} ({symbol})"
                )
                
                # Invalidate all list caches (pattern: suggestions:list:*)
                deleted_count = await invalidate_cache_pattern("suggestions:list:*")
                
                # Track metrics
                api_cache_invalidations_total.labels(
                    pattern="suggestions:list:*",
                    trigger="new_suggestion",
                ).inc()
                
                logger.info(
                    f"[Cache Invalidation] Invalidated {deleted_count} list cache keys for new suggestion {suggestion_id}",
                    extra={
                        "suggestion_id": suggestion_id,
                        "symbol": symbol,
                        "deleted_count": deleted_count,
                        "pattern": "suggestions:list:*",
                    }
                )
            
            except Exception as e:
                logger.error(
                    f"[Cache Invalidation] Error processing message: {e}",
                    exc_info=True
                )
                continue
    
    except asyncio.CancelledError:
        logger.info("Cache invalidation loop cancelled")
        raise
    finally:
        # Unsubscribe and close
        try:
            await ps.unsubscribe(RedisChannels.SUGGESTIONS_NEW)
            await ps.close()
        except Exception as e:
            logger.warning(f"Error closing pub/sub connection: {e}")
        
        logger.info("Cache invalidation loop stopped")


async def expiry_loop(
    session_factory: async_sessionmaker,
    redis_client,
) -> None:
    """
    Suggestion expiry loop - marks expired trade suggestions.
    
    Production-grade expiry worker with:
    - Batch processing (100 records per cycle)
    - Redis pub/sub notifications for real-time updates
    - Prometheus metrics tracking
    - Structured logging with context
    - Graceful shutdown handling
    
    Cadence: 60s
    Batch size: 100 suggestions per cycle
    Metrics: suggestions_expired_total counter
    Pub/Sub: cai:suggestions:expired channel
    
    Best Practices (2026):
    - Uses RETURNING clause for atomic update + fetch
    - Batch processing prevents table lock contention
    - Fire-and-forget pub/sub for real-time notifications
    - Metrics track expiry rate and direction distribution
    """
    logger.info("Suggestion expiry loop started")
    
    from app.core.metrics import suggestion_expiry_total
    from app.core.redis import RedisChannels, PubSubClient
    
    pubsub = PubSubClient(redis_client._redis)
    loop_iteration = 0
    
    try:
        while not shutdown_event.is_set():
            loop_iteration += 1
            cycle_start = datetime.now(timezone.utc)
            
            try:
                async with session_factory() as session:
                    # Fetch expired suggestions in batch (LIMIT 100)
                    now = datetime.now(timezone.utc)
                    
                    # First, select IDs to update (with LIMIT)
                    select_stmt = (
                        select(TradeSuggestion.id)
                        .where(
                            TradeSuggestion.status == "active",
                            TradeSuggestion.expires_at <= now,
                        )
                        .limit(100)  # Batch size
                    )
                    
                    # Then update those IDs
                    stmt = (
                        update(TradeSuggestion)
                        .where(TradeSuggestion.id.in_(select_stmt))
                        .values(status="expired", updated_at=now)
                        .returning(
                            TradeSuggestion.suggestion_id,
                            TradeSuggestion.symbol,
                            TradeSuggestion.signal_direction,
                            TradeSuggestion.confidence_level,
                            TradeSuggestion.consensus_score,
                            TradeSuggestion.expires_at,
                        )
                    )
                    
                    result = await session.execute(stmt)
                    expired_suggestions = result.fetchall()
                    
                    if expired_suggestions:
                        await session.commit()
                        
                        logger.info(
                            f"[Expiry #{loop_iteration}] Expired {len(expired_suggestions)} suggestions",
                            extra={
                                "loop_iteration": loop_iteration,
                                "expired_count": len(expired_suggestions),
                                "batch_size": 100,
                            }
                        )
                        
                        # Publish expiry events to Redis pub/sub
                        for row in expired_suggestions:
                            suggestion_id, symbol, direction, confidence, score, expired_at = row
                            
                            # Track metrics
                            suggestion_expiry_total.labels(
                                direction=direction,
                                confidence_level=confidence,
                            ).inc()
                            
                            # Publish to Redis pub/sub (fire-and-forget)
                            try:
                                await pubsub.publish_json(
                                    RedisChannels.SUGGESTIONS_EXPIRED,
                                    {
                                        "suggestion_id": str(suggestion_id),
                                        "symbol": symbol,
                                        "signal_direction": direction,
                                        "confidence_level": confidence,
                                        "consensus_score": float(score),
                                        "expired_at": expired_at.isoformat(),
                                        "reason": "TTL_EXPIRED",
                                    }
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[Expiry #{loop_iteration}] Failed to publish expiry event for {suggestion_id}: {e}"
                                )
                        
                        logger.debug(
                            f"[Expiry #{loop_iteration}] Published {len(expired_suggestions)} expiry events to Redis"
                        )
                    else:
                        logger.debug(f"[Expiry #{loop_iteration}] No expired suggestions found")
                
                # Log cycle performance
                cycle_duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                logger.debug(
                    f"[Expiry #{loop_iteration}] Cycle completed in {cycle_duration:.2f}s"
                )
                
                # Sleep 60s or until shutdown
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=60.0
                    )
                except asyncio.TimeoutError:
                    continue
            
            except Exception as e:
                logger.error(
                    f"[Expiry #{loop_iteration}] Unexpected error in expiry loop: {e}",
                    exc_info=True
                )
                # Back off on error (120s)
                await asyncio.sleep(120)
    
    except asyncio.CancelledError:
        logger.info("Suggestion expiry loop cancelled")
        raise
    finally:
        logger.info("Suggestion expiry loop stopped")


async def correlation_loop(
    session_factory: async_sessionmaker,
    redis_client,
    ml_components: dict,
    upstox_client: UpstoxClient,
) -> None:
    """
    Correlation engine loop - monitors scanner anomalies and news events.
    
    Implements bidirectional multi-agent consensus for trade suggestions:
    - Pathway 1: Scanner anomalies → AI + ML validation
    - Pathway 2: News events → Scanner + ML validation
    
    Cadence: 30s during market hours, 5min off-hours
    Filters: score≥5, volume≥2.0 (scanner), impact≥80 (news)
    
    Circuit breaker protected per agent for fault tolerance.
    
    NOTE: Expiry logic moved to dedicated expiry_loop() for separation of concerns.
    """
    logger.info("Starting correlation loop...")
    
    try:
        # Initialize services
        from app.ai.fusion.signal_assembler import SignalAssembler
        from app.ml.inference.feature_loader import FeatureLoader
        from app.services.market_calendar import nse_calendar
        from app.services.market_scanner import MarketScannerService

        _ml_predictor    = ml_components.get("ensemble_predictor")
        _seq_len         = ml_components.get("ensemble_sequence_length", 60)
        _n_features      = ml_components.get("ensemble_n_features", 37)
        _feature_names   = ml_components.get("ensemble_feature_names", ())
        _ml_available    = _ml_predictor is not None

        if not _ml_available:
            logger.warning(
                "Correlation loop starting WITHOUT ML predictor — "
                "all correlations will be rejected at the ML_NEUTRAL gate. "
                "Check worker startup logs for ML initialization errors."
            )

        scanner_svc = MarketScannerService(cache=redis_client)

        # assembler and engine are per-loop singletons so that circuit-breaker
        # state accumulates correctly across cycles.  feature_loader is injected
        # per-cycle (inside the session context) because FeatureLoader holds a
        # reference to the DB session which must not outlive its context manager.
        assembler = SignalAssembler(
            ensemble_predictor=_ml_predictor,
            feature_loader=None,  # injected per-cycle below
            redis=redis_client._redis,  # ML prediction cache — 30 s TTL per symbol/timeframe
        )
        engine = EventCorrelationEngine(
            signal_assembler=assembler,
            redis=redis_client._redis,
            scanner_cache=redis_client,   # CacheService — reads scanner:results:v2:1d
        )

        logger.info(
            "Correlation engine initialized — ml_available=%s features=%d seq_len=%d",
            _ml_available, _n_features, _seq_len,
        )

        loop_iteration = 0

        while not shutdown_event.is_set():
            loop_iteration += 1
            cycle_start = datetime.now(timezone.utc)

            # Determine market state once per cycle — drives pathway guard and sleep cadence.
            try:
                await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=1.0)
            except Exception:
                pass
            market = nse_calendar.get_session(cycle_start)

            try:
                async with session_factory() as session:
                    # Bind a fresh FeatureLoader to this cycle's session so that
                    # gather_ml_signals() has a live connection.  Cleared after
                    # the session exits to prevent use-after-close bugs.
                    if _ml_available:
                        assembler.feature_loader = FeatureLoader(
                            db=session,
                            redis=redis_client._redis,
                            sequence_length=_seq_len,
                            n_features=_n_features,
                            feature_names=_feature_names,
                        )

                    # ── Pathway 1: Scanner Anomalies (market-hours only) ──────
                    if market.is_open_now:
                        try:
                            scan_results, live_prices_available = await scanner_svc.scan_all(
                                session,
                                upstox_client,
                                timeframe="1d",
                                force_refresh=True,
                            )

                            logger.debug(
                                "[Correlation #%d] Scan complete: %d instruments | live_prices=%s",
                                loop_iteration, len(scan_results), live_prices_available,
                            )

                            # Filter high-conviction anomalies
                            anomalies = [
                                r for r in scan_results
                                if abs(r.score) >= 5.0 and (r.volume_ratio or 0.0) >= 2.0
                            ]

                            if anomalies:
                                logger.info(
                                    f"[Correlation #{loop_iteration}] "
                                    f"Processing {len(anomalies)} scanner anomalies"
                                )

                                for result in anomalies:
                                    try:
                                        suggestion = await engine.on_scanner_anomaly(session, result.model_dump(mode="json"))
                                        if suggestion:
                                            logger.info(
                                                f"[Correlation #{loop_iteration}] "
                                                f"Generated {suggestion.confidence_level} "
                                                f"{suggestion.signal_direction} suggestion for "
                                                f"{suggestion.symbol}"
                                            )
                                    except Exception as e:
                                        logger.error(
                                            f"[Correlation #{loop_iteration}] "
                                            f"Error processing anomaly {result.instrument_key}: {e}",
                                            exc_info=True
                                        )
                                        continue

                        except Exception as e:
                            logger.error(
                                f"[Correlation #{loop_iteration}] "
                                f"Pathway 1 error: {e}",
                                exc_info=True
                            )
                    else:
                        logger.debug(
                            "[Correlation #%d] Market closed — skipping scanner pathway",
                            loop_iteration,
                        )
                    
                    # ── Pathway 2: High-Impact News Events ───────────────
                    # Events are fetched with a 5-minute sliding window, but
                    # the loop runs every 30s — each event would be processed
                    # ~10 times within its window without the Redis dedup guard.
                    # We set a key per event_id with a TTL equal to the window
                    # so each event is processed exactly once per window.
                    try:
                        cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
                        stmt = (
                            select(AIEventClassification)
                            .where(
                                AIEventClassification.impact_score >= 80,
                                AIEventClassification.created_at >= cutoff,
                            )
                        )
                        events = (await session.execute(stmt)).scalars().all()

                        if events:
                            unprocessed = []
                            for event in events:
                                dedup_key = f"cortex:correlated:event:{event.id}"
                                already_processed = await redis_client._redis.exists(dedup_key)
                                if not already_processed:
                                    unprocessed.append(event)

                            if unprocessed:
                                logger.info(
                                    "[Correlation #%d] Processing %d/%d high-impact news events "
                                    "(%d already processed this window)",
                                    loop_iteration,
                                    len(unprocessed),
                                    len(events),
                                    len(events) - len(unprocessed),
                                )

                                for event in unprocessed:
                                    try:
                                        suggestions = await engine.on_news_event(session, event)
                                        # Mark as processed regardless of whether suggestions
                                        # were generated — prevents retry storms on events
                                        # that fail consensus (e.g. ML_NEUTRAL, DIRECTION_MISMATCH).
                                        dedup_key = f"cortex:correlated:event:{event.id}"
                                        await redis_client._redis.setex(dedup_key, 300, "1")
                                        if suggestions:
                                            logger.info(
                                                "[Correlation #%d] Generated %d suggestions "
                                                "from news event %s",
                                                loop_iteration, len(suggestions), event.id,
                                            )
                                    except Exception as e:
                                        logger.error(
                                            "[Correlation #%d] Error processing event %s: %s",
                                            loop_iteration, event.id, e,
                                            exc_info=True,
                                        )
                                        continue
                            else:
                                logger.debug(
                                    "[Correlation #%d] All %d high-impact events already "
                                    "processed this window",
                                    loop_iteration, len(events),
                                )

                    except Exception as e:
                        logger.error(
                            f"[Correlation #{loop_iteration}] "
                            f"Pathway 2 error: {e}",
                            exc_info=True
                        )

                # Session is now closed — discard the stale FeatureLoader so
                # any accidental post-cycle access fails fast rather than
                # silently using a closed DB connection.
                assembler.feature_loader = None

                # Log cycle performance
                cycle_duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                logger.debug(
                    f"[Correlation #{loop_iteration}] "
                    f"Cycle completed in {cycle_duration:.2f}s"
                )
                
                # Sleep 30 s (market open) or 5 min (market closed) before next cycle.
                sleep_secs = 30.0 if market.is_open_now else 300.0
                try:
                    await asyncio.wait_for(
                        shutdown_event.wait(),
                        timeout=sleep_secs
                    )
                except asyncio.TimeoutError:
                    continue
            
            except Exception as e:
                logger.error(
                    f"[Correlation #{loop_iteration}] "
                    f"Unexpected error in correlation loop: {e}",
                    exc_info=True
                )
                # Back off on error
                await asyncio.sleep(60)
    
    except asyncio.CancelledError:
        logger.info("Correlation loop cancelled")
        raise
    finally:
        logger.info("Correlation loop stopped")


async def main() -> None:
    """
    Main worker orchestration function.

    Spawns 11 concurrent background tasks and manages graceful shutdown.
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
    
    async with worker_lifespan() as (session_factory, redis_client, ml_components, upstox_client):
        # Extract ML components
        ensemble_predictor = ml_components.get("ensemble_predictor")
        feature_loader = ml_components.get("feature_loader")
        
        # Create 11 background tasks
        fundamentals_scheduler = FundamentalsRefreshScheduler(
            session_factory=session_factory,
            redis=redis_client,
            shutdown=shutdown_event,
        )

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
            asyncio.create_task(data_ingestion_loop(session_factory, upstox_client), name="data_ingestion"),
            asyncio.create_task(heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(
                correlation_loop(session_factory, redis_client, ml_components, upstox_client),
                name="correlation_engine"
            ),
            asyncio.create_task(
                expiry_loop(session_factory, redis_client),
                name="suggestion_expiry"
            ),
            asyncio.create_task(
                cache_invalidation_loop(redis_client),
                name="cache_invalidation"
            ),
            asyncio.create_task(
                fundamentals_scheduler.run(),
                name="fundamentals_refresh"
            ),
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
    # Suppress httpx / httpcore request-level INFO logs — each Upstox batch URL
    # contains 500 URL-encoded instrument keys (~50 KB per line of log noise).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
    except Exception as e:
        logger.error(f"Worker crashed: {e}", exc_info=True)
        raise
