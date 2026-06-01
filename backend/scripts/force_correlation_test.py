"""
Force-trigger one real correlation cycle for ML Activity Card testing.
Uses actual DB/Redis data — no mocks.

Strategy:
  1. Try cached scanner results first (Redis), then run a fresh scan from DB.
  2. Lower thresholds to abs(score)>=1.0 & volume_ratio>=0.5 to guarantee hits.
  3. If still nothing, fall back to last 24h high-impact news events.
  4. Run the real correlation engine — emits correlation_started WS events
     and writes to Redis in-flight store so the seed endpoint can surface them.
"""
import asyncio
import logging
import sys
import os

# ── path bootstrap ─────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("force_correlation")

# Suppress noisy library loggers
for lib in ("httpx", "httpcore", "hpack", "sqlalchemy.engine", "upstox_client"):
    logging.getLogger(lib).setLevel(logging.WARNING)


async def main() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from sqlalchemy import select

    from app.core.config import get_settings
    from app.core.redis import init_redis, close_redis, get_redis, CacheService
    from app.ai.correlation.engine import EventCorrelationEngine
    from app.ai.fusion.signal_assembler import SignalAssembler
    from app.schemas.scanner import ScanResult
    from app.services.upstox_client import UpstoxClient

    settings = get_settings()

    # ── DB + Redis setup ───────────────────────────────────────────────────────
    engine = create_async_engine(str(settings.DATABASE_URL), echo=False, pool_pre_ping=True)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    upstox_client = UpstoxClient()
    await upstox_client.start()

    await init_redis()
    redis_raw   = get_redis()
    cache_svc   = CacheService(redis_raw)

    # ── ML components ──────────────────────────────────────────────────────────
    ml_components: dict = {}
    try:
        from app.ml.inference.registry_loader import RegistryModelLoader
        from app.ml.inference.ensemble_predictor import EnsemblePredictor

        async with session_factory() as s:
            loader   = RegistryModelLoader(session=s, num_threads=4)
            ensemble = await loader.load_production_ensemble()

        predictor = EnsemblePredictor.from_loaded_ensemble(ensemble, cache=cache_svc)
        ml_components = {
            "ensemble_predictor":       predictor,
            "ensemble_sequence_length": ensemble.sequence_length,
            "ensemble_n_features":      ensemble.n_features,
            "ensemble_feature_names":   ensemble.feature_names,
        }
        logger.info(
            "ML loaded: XGBoost v%s + GRU v%s | features=%d seq=%d",
            ensemble.xgboost_version, ensemble.gru_version,
            ensemble.n_features, ensemble.sequence_length,
        )
    except Exception as exc:
        logger.warning("ML load failed (%s) — correlations will hit ML_NEUTRAL rejection", exc)

    # ── Build engine ───────────────────────────────────────────────────────────
    from app.ml.inference.feature_loader import FeatureLoader

    _ml_predictor  = ml_components.get("ensemble_predictor")
    _seq_len       = ml_components.get("ensemble_sequence_length", 60)
    _n_features    = ml_components.get("ensemble_n_features", 37)
    _feature_names = ml_components.get("ensemble_feature_names", ())

    assembler = SignalAssembler(
        ensemble_predictor=_ml_predictor,
        feature_loader=None,
        redis=redis_raw,
    )
    corr_engine = EventCorrelationEngine(
        signal_assembler=assembler,
        redis=redis_raw,
        scanner_cache=cache_svc,
    )

    processed = 0

    async with session_factory() as session:
        assembler.feature_loader = FeatureLoader(
            db=session,
            redis=redis_raw,
            sequence_length=_seq_len,
            n_features=_n_features,
            feature_names=_feature_names,
        )

        # ── 1. Try scanner results (cache first, then fresh scan) ─────────────
        scan_results: list[ScanResult] = []

        cached = await cache_svc.get("scanner:results:v2:1d")
        if cached:
            scan_results = [ScanResult(**r) for r in cached["results"]]
            logger.info("Loaded %d results from scanner cache", len(scan_results))
        else:
            logger.info("No scanner cache — running fresh scan from DB data…")
            from app.services.market_scanner import MarketScannerService
            scanner_svc = MarketScannerService(cache=cache_svc)
            scan_results, live = await scanner_svc.scan_all(
                session, upstox_client, timeframe="1d", force_refresh=True
            )
            logger.info("Fresh scan: %d results (live_prices=%s)", len(scan_results), live)

        # Use lowered thresholds so we always get candidates even after market close.
        # Production thresholds: abs(score)>=5.0 & volume_ratio>=2.0
        candidates = sorted(
            [r for r in scan_results if abs(r.score) >= 1.0 and (r.volume_ratio or 0.0) >= 0.5],
            key=lambda r: abs(r.score),
            reverse=True,
        )[:3]  # cap at 3 to avoid flooding

        if candidates:
            logger.info(
                "Running correlation engine on %d scanner candidates (lowered thresholds):",
                len(candidates),
            )
            for r in candidates:
                logger.info(
                    "  → %s | score=%.2f volume_ratio=%.2f signal=%s",
                    r.trading_symbol or r.instrument_key, r.score, r.volume_ratio or 0, r.signal,
                )

            for result in candidates:
                try:
                    suggestion = await corr_engine.on_scanner_anomaly(
                        session, result.model_dump(mode="json")
                    )
                    processed += 1
                    if suggestion:
                        logger.info(
                            "  ✓ Suggestion committed: %s %s (score=%.1f)",
                            suggestion.signal_direction, suggestion.symbol,
                            float(suggestion.consensus_score),
                        )
                    else:
                        logger.info("  ✗ Rejected (see correlation_rejected WS event)")
                except Exception as exc:
                    logger.error("  ! Error on %s: %s", result.instrument_key, exc)

        # ── 2. Fall back to news events (last 24h, ignore dedup guard) ────────
        if processed == 0:
            logger.info("No scanner candidates — trying news events (last 24h)…")
            from datetime import datetime, timezone, timedelta
            from app.ai.fusion.models import AIEventClassification

            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            stmt = (
                select(AIEventClassification)
                .where(
                    AIEventClassification.impact_score >= 60,  # lowered from 80
                    AIEventClassification.created_at >= cutoff,
                )
                .order_by(AIEventClassification.impact_score.desc())
                .limit(2)
            )
            events = (await session.execute(stmt)).scalars().all()

            if events:
                logger.info("Found %d news events to process", len(events))
                for event in events:
                    try:
                        suggestions = await corr_engine.on_news_event(session, event)
                        processed += 1
                        logger.info(
                            "  ✓ News event %s → %d suggestions", event.id, len(suggestions)
                        )
                    except Exception as exc:
                        logger.error("  ! Error on event %s: %s", event.id, exc)
            else:
                logger.warning("No news events found in the last 24h either.")

    if processed == 0:
        logger.warning(
            "Nothing was processed — check that the scanner has been run recently "
            "and that the DB has market data."
        )
    else:
        logger.info(
            "Done — %d correlation(s) triggered. "
            "Check the ML Activity Card on Hawk-Eye Radar.",
            processed,
        )

    # ── Teardown ───────────────────────────────────────────────────────────────
    await upstox_client.stop()
    await close_redis()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
