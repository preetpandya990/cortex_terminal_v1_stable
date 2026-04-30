"""
Signal Scheduler — Automated Trading Signal Generation

Implements a hybrid symbol universe strategy:
  - Scheduled universe (Nifty 50 + Next 50): generated every 15 minutes
    during NSE market hours via a batched ML inference cycle.
  - On-demand universe: any watchlist symbol not in the scheduled set
    is generated on first access and cached for 15 minutes.

Batch inference architecture (per cycle):
  Phase A — Parallel feature loading (DB semaphore=20, I/O bound)
  Phase B — Single batched XGBoost + GRU inference call (GPU bound)
  Phase C — Parallel signal assembly, persistence, Redis publish
             (write semaphore=10)

Lifecycle follows the same start/stop pattern as MarketFeedService.
"""
import asyncio
import logging
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels

logger = logging.getLogger(__name__)
settings = get_settings()

_IST = ZoneInfo("Asia/Kolkata")
_NSE_OPEN = time(9, 15)
_NSE_CLOSE = time(15, 30)


class SignalScheduler:
    """
    Market-hours-aware signal generation scheduler.

    Constructor args:
        db_factory    — AsyncSessionLocal callable; each task gets its own session.
        ml_predictor  — EnsemblePredictor from app.state (None = ML disabled).
        pubsub        — PubSubClient backed by the singleton Redis pool.
        redis         — redis.asyncio.Redis singleton (for FeatureLoader).
        scheduled_universe — list of NSE trading symbols to process every cycle.
    """

    def __init__(
        self,
        db_factory,
        ml_predictor,
        pubsub: PubSubClient,
        redis,
        scheduled_universe: list[str],
    ) -> None:
        self._db_factory = db_factory
        self._ml_predictor = ml_predictor
        self._pubsub = pubsub
        self._redis = redis
        self._universe = scheduled_universe
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop(), name="signal_scheduler")
        logger.info(
            "SignalScheduler started — universe=%d symbols, interval=%dm",
            len(self._universe),
            settings.SIGNAL_SCHEDULER_INTERVAL_MINUTES,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        logger.info("SignalScheduler stopped")

    # ── Scheduler loop ─────────────────────────────────────────────────────────

    async def _scheduler_loop(self) -> None:
        from app.services.market_calendar import nse_calendar

        try:
            while self._running:
                await self._sleep_until_next_run()

                if not self._running:
                    break

                session = nse_calendar.get_session()
                if not session.is_open_now:
                    logger.debug("SignalScheduler: market closed, skipping cycle")
                    continue

                await self._run_scheduled_cycle()

        except asyncio.CancelledError:
            logger.info("SignalScheduler loop cancelled")
        except Exception as exc:
            logger.error("SignalScheduler loop crashed: %s", exc, exc_info=True)

    async def _sleep_until_next_run(self) -> None:
        """Sleep until the next 15-minute boundary in IST (09:15, 09:30, …, 15:15)."""
        import math
        from datetime import datetime

        now_ist = datetime.now(_IST)
        interval = settings.SIGNAL_SCHEDULER_INTERVAL_MINUTES

        # Minutes past the last boundary
        minutes_past = now_ist.minute % interval
        seconds_past = minutes_past * 60 + now_ist.second
        seconds_to_next = interval * 60 - seconds_past

        # Never sleep more than one full interval
        sleep_secs = min(seconds_to_next, interval * 60)
        logger.debug("SignalScheduler sleeping %.0fs until next run", sleep_secs)
        await asyncio.sleep(sleep_secs)

    # ── Scheduled cycle ────────────────────────────────────────────────────────

    async def _run_scheduled_cycle(self) -> None:
        """
        Full generation cycle for the scheduled universe.

        Phase A: concurrent feature loading (one DB session per symbol)
        Phase B: single batched ML inference
        Phase C: concurrent signal assembly + persistence
        """
        from datetime import datetime
        symbols = self._universe
        logger.info("SignalScheduler cycle starting — %d symbols", len(symbols))
        t0 = asyncio.get_event_loop().time()

        # ── Phase A: parallel feature loading ─────────────────────────────
        feature_sem = asyncio.Semaphore(settings.SIGNAL_SCHEDULER_FEATURE_CONCURRENCY)
        feature_tasks = [
            asyncio.create_task(self._load_features_safe(sym, feature_sem))
            for sym in symbols
        ]
        feature_results = await asyncio.gather(*feature_tasks)

        # Separate successful loads from failures
        loaded: list[dict[str, Any]] = []
        for sym, result in zip(symbols, feature_results):
            if result is not None:
                loaded.append(result)
            else:
                logger.debug("Feature load failed for %s — skipping", sym)

        if not loaded:
            logger.warning("SignalScheduler: no features loaded for any symbol")
            return

        # ── Phase B: single batched ML inference ──────────────────────────
        if self._ml_predictor:
            predictions = await self._ml_predictor.predict_batch(loaded)
            # Map symbol → prediction
            pred_map: dict[str, dict] = {
                item["symbol"]: pred
                for item, pred in zip(loaded, predictions)
                if pred is not None
            }
        else:
            pred_map = {}

        # ── Phase C: parallel signal assembly ─────────────────────────────
        assembly_sem = asyncio.Semaphore(settings.SIGNAL_SCHEDULER_ASSEMBLY_CONCURRENCY)
        assembly_tasks = [
            asyncio.create_task(
                self._assemble_signal_safe(
                    symbol=item["symbol"],
                    timeframe=item.get("timeframe", "1d"),
                    ml_prediction=pred_map.get(item["symbol"]),
                    sem=assembly_sem,
                )
            )
            for item in loaded
        ]
        assembly_results = await asyncio.gather(*assembly_tasks)

        success = sum(1 for r in assembly_results if r)
        elapsed = asyncio.get_event_loop().time() - t0
        logger.info(
            "SignalScheduler cycle complete — %d/%d signals in %.1fs",
            success,
            len(loaded),
            elapsed,
        )

    # ── Feature loading ────────────────────────────────────────────────────────

    async def _load_features_safe(
        self,
        symbol: str,
        sem: asyncio.Semaphore,
    ) -> dict[str, Any] | None:
        """Load features for one symbol in its own DB session."""
        from app.ml.inference.feature_loader import FeatureLoader

        async with sem:
            try:
                async with self._db_factory() as db:
                    loader = FeatureLoader(db=db, redis=self._redis)
                    tabular, sequence, current_price, volatility = (
                        await loader.load_features(symbol=symbol, timeframe="1d")
                    )
                return {
                    "symbol": symbol,
                    "tabular": tabular,
                    "sequence": sequence,
                    "current_price": current_price,
                    "volatility": volatility,
                    "timeframe": "1d",
                }
            except Exception as exc:
                logger.debug("Feature load error for %s: %s", symbol, exc)
                return None

    # ── Signal assembly ────────────────────────────────────────────────────────

    async def _assemble_signal_safe(
        self,
        symbol: str,
        timeframe: str,
        ml_prediction: dict[str, Any] | None,
        sem: asyncio.Semaphore,
    ) -> bool:
        """Assemble, persist, and publish a signal for one symbol."""
        from app.ai.fusion.signal_assembler import SignalAssembler

        async with sem:
            try:
                assembler = SignalAssembler(
                    ensemble_predictor=None,   # ML result pre-computed in Phase B
                    feature_loader=None,
                )
                async with self._db_factory() as db:
                    await assembler.assemble_signal(
                        db=db,
                        pubsub=self._pubsub,
                        symbol=symbol,
                        timeframe=timeframe,
                        precomputed_ml=ml_prediction,
                    )
                return True
            except Exception as exc:
                logger.error("Assembly error for %s: %s", symbol, exc, exc_info=False)
                return False

    # ── On-demand generation ───────────────────────────────────────────────────

    async def generate_on_demand(self, symbol: str) -> None:
        """
        Generate a signal for a watchlist symbol not in the scheduled universe.

        Checks the Redis on-demand cache first (15-minute TTL).  On a cache miss,
        fires an asyncio task that loads features and assembles the signal without
        blocking the caller.
        """
        from app.core.redis import get_redis

        cache_key = f"cai:signals:on-demand:{symbol}"
        redis = get_redis()

        cached = await redis.get(cache_key)
        if cached:
            logger.debug("On-demand cache hit for %s", symbol)
            return

        # Mark as in-flight immediately to prevent duplicate generations
        # from rapid concurrent requests for the same symbol.
        await redis.setex(cache_key, settings.SIGNAL_ON_DEMAND_CACHE_TTL, "1")

        asyncio.create_task(
            self._generate_on_demand_task(symbol),
            name=f"signal_on_demand_{symbol}",
        )

    async def _generate_on_demand_task(self, symbol: str) -> None:
        """
        Background task: load features + assemble a signal for a single symbol.

        Before generating, checks whether a signal was assembled within the last
        5 minutes (recency guard).  This prevents duplicate assembly when the event
        pipeline's fusion layer and generate_on_demand() fire concurrently for the
        same symbol — a natural race for high-impact events.
        """
        from datetime import datetime, timedelta, timezone

        from sqlalchemy import desc, select

        from app.ai.fusion.models import AITradingSignal
        from app.ai.fusion.signal_assembler import SignalAssembler
        from app.ml.inference.feature_loader import FeatureLoader

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=5)
            async with self._db_factory() as db:
                recent = (
                    await db.execute(
                        select(AITradingSignal)
                        .where(AITradingSignal.symbol == symbol)
                        .where(AITradingSignal.signal_timestamp >= cutoff)
                        .order_by(desc(AITradingSignal.signal_timestamp))
                        .limit(1)
                    )
                ).scalar_one_or_none()

                if recent is not None:
                    logger.debug(
                        "On-demand skipped for %s — signal assembled at %s (within 5-min window)",
                        symbol,
                        recent.signal_timestamp.isoformat(),
                    )
                    return

                feature_loader = (
                    FeatureLoader(db=db, redis=self._redis)
                    if self._ml_predictor
                    else None
                )
                assembler = SignalAssembler(
                    ensemble_predictor=self._ml_predictor,
                    feature_loader=feature_loader,
                )
                await assembler.assemble_signal(
                    db=db,
                    pubsub=self._pubsub,
                    symbol=symbol,
                    timeframe="1d",
                )

            logger.info("On-demand signal generated for %s", symbol)
        except Exception as exc:
            logger.error("On-demand generation failed for %s: %s", symbol, exc, exc_info=False)
