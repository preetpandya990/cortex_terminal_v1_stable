"""
Cortex AI — Worker Task Coroutines
====================================

Pure task-coroutine module.  Contains:

  worker_lifespan()          Context manager that initialises all shared resources
                             (DB engine, Redis, ML models, Upstox client) and tears
                             them down cleanly.  Used by worker_app.py's lifespan.

  heartbeat_loop()           Writes worker:heartbeat to Redis every 30s.
  cache_invalidation_loop()  Event-driven suggestion cache flusher.
  expiry_loop()              Batch-marks expired TradeSuggestions every 60s.
  correlation_loop()         Bidirectional scanner→AI + news→AI consensus engine.
  feature_refresh_loop()     Daily ML feature store refresh at 16:00 IST.

The first four loops accept PauseToken, TriggerToken, and a shutdown Event so
the control-plane (worker_control.py) can pause, resume, and trigger them
without restarting the process.  feature_refresh_loop accepts shutdown only.

Orchestration (TaskGroup, supervisor, signal handling) lives in worker_app.py.
The 9 imported coroutines (rss_ingestion, event_processing, regime_detection,
drift_detection, safety_monitoring, data_ingestion, fundamentals_refresh,
pnl_worker, sl_tp_worker) are registered in workers/registry.py.
"""
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator, Callable

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
from app.workers.supervisor import PauseToken, TriggerToken

logger = logging.getLogger(__name__)
settings = get_settings()


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

    # Initialize Kafka (Redpanda) — durable job topics + process-wide producer.
    # The forecast batch consumer and classifier flush consumers access the
    # app.core.kafka accessors directly (same pattern as get_redis()).
    from app.core.kafka import init_kafka
    await init_kafka()
    logger.info("Kafka topics and producer initialized")

    # Initialize Gemini Request Manager — must run after init_redis() (circuit
    # pre-load reads Redis) and before LLM client / NLPEngine (both call acquire()).
    try:
        from app.ai.intelligence.request_manager import GeminiRequestManager
        from app.ai.intelligence.llm_client import _key_id as _gemini_key_id
        from app.core.redis import get_redis
        _gemini_key_ids = [_gemini_key_id(k) for k in settings.gemini_api_key_pool]
        await GeminiRequestManager.initialize(redis=get_redis(), key_ids=_gemini_key_ids)
        logger.info("Gemini request manager initialized (keys=%d)", len(_gemini_key_ids))
    except Exception as exc:
        logger.error(
            "Gemini request manager initialization failed (quota coordination degraded): %s", exc
        )

    # Initialize LLM Intelligence Client — probes backends, logs active transport.
    try:
        from app.ai.intelligence.llm_client import CortexIntelligenceClient
        await CortexIntelligenceClient.initialize()
        logger.info("LLM intelligence client initialized")
    except Exception as exc:
        logger.error(
            "LLM intelligence client initialization failed (LLM features degraded): %s", exc
        )

    # Initialize NLP engine — requires CortexIntelligenceClient to be initialized first.
    # Without this, NLPEngine._queue is never set and every event_processing_loop
    # iteration crashes with AttributeError on the first Redis cache miss.
    try:
        from app.ai.intelligence.nlp_engine import NLPEngine
        await NLPEngine.initialize()
        logger.info("NLP engine initialized")
    except Exception as exc:
        logger.warning(
            "NLP engine initialization failed (sentiment analysis degraded): %s", exc
        )

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

        # Drain NLP batch queue first — resolves pending futures with a neutral
        # fallback before the Gemini transport is torn down so event-pipeline
        # coroutines receive a clean result rather than a transport error.
        try:
            from app.ai.intelligence.nlp_engine import NLPEngine
            await NLPEngine.aclose()
        except Exception as exc:
            logger.debug("NLP engine close failed (non-fatal): %s", exc)

        # Drain Gemini permit queue — waiting callers get GeminiRateLimitError,
        # not a transport error, which is the correct failure mode at shutdown.
        try:
            from app.ai.intelligence.request_manager import GeminiRequestManager
            if GeminiRequestManager._instance is not None:
                await GeminiRequestManager._instance.aclose()
        except Exception as exc:
            logger.debug("Gemini request manager close failed (non-fatal): %s", exc)

        # Close LLM transport after the permit queue is drained.
        try:
            from app.ai.intelligence.llm_client import close_intelligence_client
            await close_intelligence_client()
        except Exception as exc:
            logger.debug("LLM client close failed (non-fatal): %s", exc)

        await upstox_client.stop()

        # Close Kafka after all task loops have been cancelled by worker_app's
        # TaskGroup teardown and before Redis (consumers write guard keys and
        # result caches through Redis until they stop).
        try:
            from app.core.kafka import close_kafka
            await close_kafka()
        except Exception as exc:
            logger.debug("Kafka close failed (non-fatal): %s", exc)

        await close_redis()
        await engine.dispose()
        logger.info("Worker resources cleaned up")


async def heartbeat_loop(
    pause: PauseToken,
    trigger: TriggerToken,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Heartbeat loop — writes a UTC timestamp to Redis every 30s.

    Key: worker:heartbeat  TTL: 60s
    Allows the main API's /health/ready probe and Grafana to detect worker health.
    """
    logger.info("Heartbeat loop started")
    redis_client = get_cache_service()

    try:
        while not shutdown.is_set():
            await pause.checkpoint()

            try:
                timestamp = datetime.now(timezone.utc).isoformat()
                await redis_client.set("worker:heartbeat", timestamp, ttl=60)
                logger.debug("Heartbeat: %s", timestamp)
            except Exception as exc:
                logger.error("Heartbeat error: %s", exc, exc_info=True)

            if on_cycle is not None:
                on_cycle()

            await trigger.wait_or_timeout(30.0)

    except asyncio.CancelledError:
        logger.info("Heartbeat loop cancelled")
        raise
    finally:
        logger.info("Heartbeat loop stopped")


async def cache_invalidation_loop(
    redis_client,
    pause: PauseToken,
    trigger: TriggerToken,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Cache invalidation loop — event-driven via Redis pub/sub.

    Subscribes to SUGGESTIONS_NEW and invalidates all suggestions:list:* keys
    whenever a new suggestion is created, ensuring zero stale-cache windows
    on the suggestions list endpoint.

    Cadence: real-time (no sleep; driven by pub/sub message arrival)
    Pattern:  suggestions:list:*
    Metrics:  api_cache_invalidations_total
    """
    logger.info("Cache invalidation loop started")

    from app.core.cache_decorator import invalidate_cache_pattern
    from app.core.metrics import api_cache_invalidations_total
    from app.core.redis import RedisChannels, PubSubClient

    pubsub = PubSubClient(redis_client._redis)
    ps = await pubsub.subscribe(RedisChannels.SUGGESTIONS_NEW)
    logger.info("Subscribed to %s for cache invalidation", RedisChannels.SUGGESTIONS_NEW)

    try:
        async for message in pubsub.listen(ps):
            # Respect shutdown: break the pub/sub loop cleanly.
            if shutdown.is_set():
                break

            # Cooperative pause: block here (holding the pub/sub connection open)
            # until the control plane calls resume().  Messages that arrive while
            # paused accumulate in the Redis client buffer and are processed when
            # the pause lifts — no events are lost.
            await pause.checkpoint()

            try:
                data = message["data"]
                suggestion_id = data.get("suggestion_id")
                symbol = data.get("symbol", "UNKNOWN")

                logger.debug(
                    "[Cache Invalidation] New suggestion event: %s (%s)",
                    suggestion_id, symbol,
                )

                deleted_count = await invalidate_cache_pattern("suggestions:list:*")

                api_cache_invalidations_total.labels(
                    pattern="suggestions:list:*",
                    trigger="new_suggestion",
                ).inc()

                logger.info(
                    "[Cache Invalidation] Invalidated %d list cache keys "
                    "(suggestion=%s symbol=%s)",
                    deleted_count, suggestion_id, symbol,
                    extra={
                        "suggestion_id": suggestion_id,
                        "symbol": symbol,
                        "deleted_count": deleted_count,
                    },
                )

                if on_cycle is not None:
                    on_cycle()

            except Exception as exc:
                logger.error(
                    "[Cache Invalidation] Error processing message: %s", exc,
                    exc_info=True,
                )
                continue

    except asyncio.CancelledError:
        logger.info("Cache invalidation loop cancelled")
        raise
    finally:
        try:
            await ps.unsubscribe(RedisChannels.SUGGESTIONS_NEW)
            await ps.aclose()
        except Exception as exc:
            logger.warning("Error closing pub/sub connection: %s", exc)
        logger.info("Cache invalidation loop stopped")


async def expiry_loop(
    session_factory: async_sessionmaker,
    redis_client,
    pause: PauseToken,
    trigger: TriggerToken,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Suggestion expiry loop — marks expired TradeSuggestions every 60s.

    Cadence:    60s (interruptible via trigger.fire() for immediate sweep)
    Batch size: 100 per cycle (avoids table lock contention)
    Pub/Sub:    cai:suggestions:expired (real-time browser WS updates)
    Metrics:    suggestions_expired_total, suggestions_active, worker_loop_*
    """
    logger.info("Suggestion expiry loop started")

    from app.core.metrics import (
        suggestion_expiry_total,
        suggestions_active,
        worker_loop_iterations_total,
        worker_loop_duration_seconds,
    )
    from app.core.redis import RedisChannels, PubSubClient

    pubsub = PubSubClient(redis_client._redis)
    loop_iteration = 0

    try:
        while not shutdown.is_set():
            await pause.checkpoint()

            loop_iteration += 1
            cycle_start = datetime.now(timezone.utc)
            worker_loop_iterations_total.labels(loop_name="suggestion_expiry").inc()

            try:
                async with session_factory() as session:
                    now = datetime.now(timezone.utc)

                    select_stmt = (
                        select(TradeSuggestion.id)
                        .where(
                            TradeSuggestion.status == "active",
                            TradeSuggestion.expires_at <= now,
                        )
                        .limit(100)
                    )

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
                            "[Expiry #%d] Expired %d suggestions",
                            loop_iteration, len(expired_suggestions),
                            extra={
                                "loop_iteration": loop_iteration,
                                "expired_count": len(expired_suggestions),
                            },
                        )

                        for row in expired_suggestions:
                            suggestion_id, symbol, direction, confidence, score, expired_at = row

                            suggestion_expiry_total.labels(
                                direction=direction,
                                confidence_level=confidence,
                            ).inc()
                            suggestions_active.labels(
                                direction=direction,
                                confidence_level=confidence,
                            ).dec()

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
                                    },
                                )
                            except Exception as exc:
                                logger.warning(
                                    "[Expiry #%d] Failed to publish expiry event %s: %s",
                                    loop_iteration, suggestion_id, exc,
                                )
                    else:
                        logger.debug("[Expiry #%d] No expired suggestions", loop_iteration)

                cycle_duration = (datetime.now(timezone.utc) - cycle_start).total_seconds()
                worker_loop_duration_seconds.labels(loop_name="suggestion_expiry").observe(
                    cycle_duration
                )
                logger.debug(
                    "[Expiry #%d] Cycle completed in %.2fs",
                    loop_iteration, cycle_duration,
                )

                if on_cycle is not None:
                    on_cycle()

                await trigger.wait_or_timeout(60.0)

            except Exception as exc:
                logger.error(
                    "[Expiry #%d] Unexpected error: %s",
                    loop_iteration, exc,
                    exc_info=True,
                )
                await asyncio.sleep(120)

    except asyncio.CancelledError:
        logger.info("Suggestion expiry loop cancelled")
        raise
    finally:
        logger.info("Suggestion expiry loop stopped")


async def feature_refresh_loop(
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
) -> None:
    """
    Daily ML feature store refresh — fires at 16:00 IST (30 min after NSE close).

    Instantiates FeatureComputationPipeline in refresh mode and recomputes
    features for all symbols where ml_features.MAX(timestamp) is older than
    _STALE_DAYS calendar days.  Uses a dedicated DB connection pool (isolated
    from the worker's shared pool) to prevent resource contention during the
    heavy batch operation.

    Sleeps in 60 s chunks so the cooperative shutdown event is honoured within
    one minute of a stop signal — the standard pattern used across this worker.
    """
    from zoneinfo import ZoneInfo

    _IST            = ZoneInfo("Asia/Kolkata")
    _REFRESH_HOUR   = 16
    _REFRESH_MINUTE = 0
    _STALE_DAYS     = 3
    _LOOKBACK_DAYS  = 90

    # Add the scripts directory to sys.path once so FeatureComputationPipeline
    # is importable.  This is idempotent — the module is cached in sys.modules
    # after the first import so subsequent loop iterations pay no I/O cost.
    _scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    from compute_production_features import FeatureComputationPipeline  # noqa: PLC0415

    logger.info(
        "Feature refresh loop started — daily at %02d:%02d IST",
        _REFRESH_HOUR, _REFRESH_MINUTE,
    )

    while not shutdown.is_set():
        now_ist = datetime.now(_IST)
        target  = now_ist.replace(
            hour=_REFRESH_HOUR, minute=_REFRESH_MINUTE, second=0, microsecond=0,
        )
        if now_ist >= target:
            target += timedelta(days=1)

        wait_total = (target - now_ist).total_seconds()
        logger.info(
            "Feature refresh: next run at %s IST (in %.0f min)",
            target.strftime("%Y-%m-%d %H:%M"), wait_total / 60,
        )

        # Sleep in 60 s chunks so shutdown is honoured within one minute.
        slept = 0.0
        while slept < wait_total and not shutdown.is_set():
            chunk  = min(60.0, wait_total - slept)
            await asyncio.sleep(chunk)
            slept += chunk

        if shutdown.is_set():
            break

        logger.info(
            "Feature refresh: starting daily run (mode=refresh, stale_days=%d)",
            _STALE_DAYS,
        )
        try:
            pipeline = FeatureComputationPipeline(
                db_url=str(settings.DATABASE_URL),
                lookback_days=_LOOKBACK_DAYS,
                batch_size=10,
                max_workers=5,
                mode="refresh",
                stale_days=_STALE_DAYS,
            )
            await pipeline.run()
            logger.info("Feature refresh: daily run complete")
        except Exception as exc:
            logger.error(
                "Feature refresh: daily run failed — %s", exc, exc_info=True,
            )
        finally:
            if on_cycle is not None:
                on_cycle()

    logger.info("Feature refresh loop stopped")


async def correlation_loop(
    session_factory: async_sessionmaker,
    redis_client,
    ml_components: dict,
    upstox_client: UpstoxClient,
    pause: PauseToken,
    trigger: TriggerToken,
    shutdown: asyncio.Event,
    *,
    on_cycle: Callable[[], None] | None = None,
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

        while not shutdown.is_set():
            await pause.checkpoint()

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

                if on_cycle is not None:
                    on_cycle()

                # Sleep 30s (market open) or 5min (market closed) before next cycle.
                # trigger.fire() (from the control plane) wakes the loop immediately.
                sleep_secs = 30.0 if market.is_open_now else 300.0
                await trigger.wait_or_timeout(sleep_secs)
            
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


# ── worker.py is now a pure task-coroutines module ────────────────────────────
# Orchestration lives in worker_app.py (FastAPI lifespan + TaskGroup).
# The 4 loops defined here (heartbeat, cache_invalidation, expiry, correlation)
# accept PauseToken/TriggerToken so the control-plane can pause, resume, and
# trigger them remotely.  The 9 imported loops respond to CancelledError only;
# full safepoint support for those is a planned Phase 2 initiative.

