"""
Signal Assembler Service

Assembles trading signals from multiple data sources:
  - Event signals with exponential temporal decay (48h lookback)
  - ML ensemble predictions (XGBoost + GRU via EnsemblePredictor)
  - Technical indicators (Phase 5 — RSI-14 / EMA crossover, stub until then)

Weights (Phase 2–4): event=0.50, ml=0.50, technical=0.00
Weights (Phase 5+):  event=0.35, ml=0.40, technical=0.25
"""
import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from time import monotonic
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import and_, desc, func, select, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AIActiveStrategy,
    AIEventClassification,
    AILLMAuditLog,
    AINLPResult,
    AIProcessedEvent,
    AIRawEvent,
    AIRegimeDetection,
    AITradingSignal,
)
from app.ai.fusion.news_forecaster import (
    CircuitBreaker,
    _BATCH_EVENT_FIELDS,
)
from app.ai.fusion.serializers import serialise_signal
from app.core.config import get_settings
from app.core.kafka import KafkaTopics, publish as kafka_publish
from app.core.metrics import (
    forecast_enqueue_gated_total,
    llm_news_forecasts_total,
    signal_generation_total,
    signal_staleness_abstentions_total,
)
from app.ai.fusion.forecast_cache import forecast_cache_key
from app.services.demand_registry import is_in_demand
from app.core.redis import PubSubClient, RedisChannels
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader
from app.ml.monitoring.metrics import feature_cache_hit_rate, feature_errors_total

logger = logging.getLogger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


# ── Technical indicator helpers (pure numpy, no extra deps) ───────────────────

def _compute_ema(prices: np.ndarray, period: int) -> np.ndarray:
    """Compute Exponential Moving Average using the standard recursive formula."""
    ema = np.empty_like(prices)
    k = 2.0 / (period + 1)
    ema[0] = prices[0]
    for i in range(1, len(prices)):
        ema[i] = prices[i] * k + ema[i - 1] * (1.0 - k)
    return ema


def _compute_rsi(prices: np.ndarray, period: int = 14) -> float:
    """
    Compute RSI using Wilder's smoothed moving average (industry standard).
    Returns a float in [0, 100].
    """
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    # Seed with simple average of first `period` values
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    # Wilder's smoothing for remaining values
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
_UTC = ZoneInfo("UTC")
_NSE_CLOSE = time(15, 30)

_TIMEFRAME_TO_HORIZON: dict[str, str] = {
    "1minute": "intraday",
    "5minute": "intraday",
    "15minute": "intraday",
    "30minute": "intraday",
    "1hour": "swing",
    "4hour": "swing",
    "1d": "swing",
    "1D": "swing",
    "1week": "positional",
    "1month": "positional",
}

_CONFIDENCE_BAND: list[tuple[float, str]] = [
    (0.80, "high"),
    (0.60, "medium"),
    (0.0, "low"),
]


class SignalAssembler:
    """Assembles and persists trading signals from multiple sources."""

    # TTL for per-symbol ML prediction cache entries (seconds).
    _ML_CACHE_TTL = 30

    def __init__(
        self,
        ensemble_predictor: Optional[EnsemblePredictor] = None,
        feature_loader: Optional[FeatureLoader] = None,
        event_weight: float = 0.35,
        ml_weight: float = 0.40,
        technical_weight: float = 0.25,
        redis: Optional[Any] = None,
    ):
        self.ensemble_predictor = ensemble_predictor
        self.feature_loader = feature_loader
        self.event_weight = event_weight
        self.ml_weight = ml_weight
        self.technical_weight = technical_weight
        # raw redis.asyncio.Redis client; None disables caching transparently
        self._ml_cache = redis

        # News-forecaster circuit breaker (provider-wide; one per assembler).
        _s = get_settings()
        self._forecast_breaker = CircuitBreaker(
            threshold=_s.NEWS_FORECAST_BREAKER_THRESHOLD,
            cooldown=_s.NEWS_FORECAST_BREAKER_COOLDOWN,
        )
        # Cached global latest 1D OHLCV bar (the data frontier) for the staleness
        # guard: (frontier_timestamp_or_None, monotonic_cached_at).
        self._frontier_cache: tuple[Any, float] | None = None

        total_weight = event_weight + ml_weight + technical_weight
        if not math.isclose(total_weight, 1.0, rel_tol=1e-5):
            raise ValueError(f"Signal weights must sum to 1.0, got {total_weight}")

    # ── Public helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def calculate_event_decay(
        age_hours: float,
        fast_hl: float,
        slow_hl: float,
        fast_weight: float = 0.7,
    ) -> float:
        """
        Two-component Hawkes-kernel exponential decay.

        decay(t) = fast_weight · 0.5^(t/fast_hl) + (1−fast_weight) · 0.5^(t/slow_hl)

        The 0.7 / 0.3 fast / slow split is calibrated on NSE equity event data.
        fast_hl  captures intraday price reaction (e.g. 8–24 h for most events).
        slow_hl  captures multi-day fundamental repricing (e.g. 48–168 h).
        """
        if age_hours < 0:
            raise ValueError(f"Event age cannot be negative: {age_hours}")
        if fast_hl <= 0 or slow_hl <= 0:
            raise ValueError(f"Half-life values must be positive: fast={fast_hl}, slow={slow_hl}")
        return (
            fast_weight * math.pow(0.5, age_hours / fast_hl)
            + (1.0 - fast_weight) * math.pow(0.5, age_hours / slow_hl)
        )

    # ── Signal gathering ───────────────────────────────────────────────────────

    @staticmethod
    def _build_event_dict(row: Any) -> dict[str, Any]:
        """
        Build the contributing-event dict stored in ai_trading_signals.contributing_events.

        article_title comes from AIRawEvent.extra_data["title"] (populated for new events
        after the rss_fetcher bug-fix).  For the large corpus of existing events where
        extra_data was never written, we fall back to the first line of raw_content —
        the RSS fetcher always constructs raw_content as "{title}\n\n{description}", so
        line 0 is always the article headline.
        """
        raw = row.raw_content or ""
        parts = raw.split("\n\n", 1)
        title_from_content = parts[0].strip() or None
        summary_from_content = parts[1].strip()[:300] if len(parts) > 1 else None

        return {
            "id": row.id,
            "type": row.event_type,
            "impact": float(row.impact_score),
            "source_url": row.source_url or None,
            "source_name": row.source_name or None,
            "article_title": row.article_title or title_from_content or None,
            "summary": summary_from_content or None,
        }

    async def gather_event_signals(
        self,
        db: AsyncSession,
        symbol: str,
        lookback_hours: int = 48,
        reference_time: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """
        Gather event signals with temporal decay and source provenance for a symbol.

        Traverses the full classification chain to pull article metadata from the
        originating raw event: AIEventClassification → AINLPResult → AIProcessedEvent
        → AIRawEvent.  LEFT JOINs are used so that events survive even when
        intermediate chain records are absent (e.g. NLP pass produced no record).

        Args:
            reference_time: Anchor for the lookback window and decay calculation.
                            Defaults to ``datetime.now(timezone.utc)``.  Pass the
                            original signal timestamp when backfilling historical
                            records so decay is computed relative to signal creation
                            rather than the current wall-clock time.
        """
        now = reference_time if reference_time is not None else datetime.now(timezone.utc)
        cutoff_time = now - timedelta(hours=lookback_hours)
        # Upper bound: only events that existed at `now`.  In live operation
        # this is always satisfied (events are in the past); when backfilling
        # with a historical reference_time, events created after the signal
        # must not be included — they weren't available when it was generated.

        stmt = (
            select(
                AIEventClassification.id,
                AIEventClassification.event_type,
                AIEventClassification.impact_score,
                AIEventClassification.classification_confidence,
                AIEventClassification.decay_half_life_hours,
                AIEventClassification.decay_slow_half_life_hours,
                AIEventClassification.created_at,
                AIRawEvent.source_url,
                AIRawEvent.source_name,
                AIRawEvent.raw_content,
                func.jsonb_extract_path_text(
                    AIRawEvent.extra_data, "title"
                ).label("article_title"),
            )
            .outerjoin(AINLPResult, AINLPResult.id == AIEventClassification.nlp_result_id)
            .outerjoin(AIProcessedEvent, AIProcessedEvent.id == AINLPResult.processed_event_id)
            .outerjoin(AIRawEvent, AIRawEvent.id == AIProcessedEvent.raw_event_id)
            .where(
                and_(
                    AIEventClassification.affected_symbols.contains([symbol]),
                    AIEventClassification.created_at >= cutoff_time,
                    AIEventClassification.created_at <= now,
                )
            )
            .order_by(desc(AIEventClassification.created_at))
        )

        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            return {"score": 0.0, "confidence": 0.0, "event_count": 0, "events": [], "available": False}

        n = len(rows)

        # Collect per-event decayed scores first so we can apply √N normalization.
        decayed_scores: list[float] = []
        total_confidence = 0.0
        for row in rows:
            age_hours = (now - row.created_at).total_seconds() / 3600
            decay_factor = self.calculate_event_decay(
                age_hours,
                float(row.decay_half_life_hours),
                float(row.decay_slow_half_life_hours),
            )
            decayed_scores.append(float(row.impact_score) * decay_factor)
            total_confidence += float(row.classification_confidence) * decay_factor

        # Phase 3: 1/√N dampening prevents N correlated low-impact events from
        # outweighing 1 high-impact event.  Clip to ±100 before normalizing so
        # a single extreme event cannot dominate unboundedly either.
        raw_sum = sum(decayed_scores)
        total_score = float(np.clip(raw_sum, -100.0, 100.0)) / math.sqrt(n)

        return {
            "score": total_score,
            "confidence": total_confidence / n,
            "event_count": n,
            "available": True,
            "events": [
                self._build_event_dict(row)
                for row in rows[:5]
            ],
        }

    # ── News forecaster (Gemini, AI/news consensus slot) ───────────────────────

    async def gather_news_forecast(
        self,
        db: AsyncSession,
        symbol: str,
        *,
        ml_signal: dict[str, Any] | None = None,
        event_signals: dict[str, Any] | None = None,
        regime: str | None = None,
    ) -> dict[str, Any]:
        """
        Gemini news-conditioned forecast for the AI/news consensus slot.

        Reasons over the ML's exact indicators (``ml_signal["indicators"]``) plus
        recent news to produce {direction, confidence}, mapped to the −100..100
        event-slot shape consumed unchanged by ``fuse_signals`` and the engine
        consensus.

        Runs inside the 5s consensus budget with a tight per-call timeout, a
        per-symbol cache, and a circuit breaker.  On ANY failure it falls back to
        the deterministic NLP event score, so suggestion creation and the
        directional-alignment gate never stall.

        The forecaster only contributes when there is news: with no events it
        returns the (unavailable) event signal unchanged, keeping the AI slot
        purely news-driven and avoiding double-counting the technical slot.
        """
        if event_signals is None:
            event_signals = await self.gather_event_signals(db, symbol)
        events = event_signals.get("events") or []

        # Stale-data guard: the forecaster reasons over the same indicators the ML
        # saw, so if those are stale (daily sync lagging) it abstains too rather
        # than forecast on stale technicals.  Mirrors gather_ml_signals.
        if (ml_signal or {}).get("stale"):
            signal_staleness_abstentions_total.labels(component="forecaster").inc()
            return {**event_signals, "available": False, "forecast_source": "stale_data"}

        # No news → forecaster abstains; fusion excludes the (unavailable) AI slot.
        if not events:
            return {**event_signals, "forecast_source": "no_news"}

        indicators = (ml_signal or {}).get("indicators") or {}

        def _fallback(reason: str) -> dict[str, Any]:
            return {
                **event_signals,
                "available": True,
                "forecast_source": "fallback",
                "fallback_reason": reason,
            }

        # Circuit breaker open → skip the call, fall straight through.
        if not self._forecast_breaker.allow():
            llm_news_forecasts_total.labels(outcome="breaker_open").inc()
            return _fallback("circuit_breaker_open")

        cache_key = self._forecast_cache_key(symbol, events, indicators)
        cached = await self._forecast_cache_get(cache_key)
        if cached is not None:
            llm_news_forecasts_total.labels(outcome="cache_hit").inc()
            return cached

        # Demand gating (WS6): background forecast enqueues are an uncapped
        # RPD cost driver when fired for every scanned symbol. Behind the
        # flag, only symbols with live user demand (watchlist ∪ active
        # suggestions) enqueue; gated symbols return a fallback shape whose
        # AI weight the consensus renormalizes away (F.A). Cache HITS above
        # are always served regardless — they cost nothing. Fails open inside
        # is_in_demand on infrastructure errors.
        if (
            get_settings().FORECAST_DEMAND_GATING
            and self._ml_cache is not None
            and not await is_in_demand(self._ml_cache, db, symbol)
        ):
            forecast_enqueue_gated_total.inc()
            llm_news_forecasts_total.labels(outcome="demand_gated").inc()
            return _fallback("demand_gated")

        # Cache miss — enqueue for background batch processing; return NLP fallback
        # immediately so the hot path (signal assembly, consensus) never blocks on
        # Gemini.  The forecast_batch_worker drains this queue asynchronously,
        # fires one Gemini call per batch of symbols, and writes results to the
        # same cache keys.  The NEXT signal for this symbol within the 5-minute
        # TTL will hit cache and return the Gemini result with zero latency.
        await self._enqueue_for_batch_forecast(symbol, events, indicators, regime, cache_key)
        llm_news_forecasts_total.labels(outcome="batch_enqueued").inc()
        return _fallback("batch_pending")

    def _forecast_cache_key(
        self, symbol: str, events: list[dict], indicators: dict[str, Any]
    ) -> str:
        """Delegates to the SHARED builder (app.ai.fusion.forecast_cache) —
        the WS7 demand path writes the same keys; never fork this logic."""
        return forecast_cache_key(symbol, events, indicators)

    async def _forecast_cache_get(self, key: str) -> dict[str, Any] | None:
        if self._ml_cache is None:
            return None
        try:
            raw = await self._ml_cache.get(key)
            return json.loads(raw) if raw else None
        except Exception as exc:
            logger.debug("news_forecaster: cache read failed (non-fatal): %s", exc)
            return None

    async def _forecast_cache_set(self, key: str, value: dict[str, Any], ttl: int) -> None:
        if self._ml_cache is None:
            return
        try:
            await self._ml_cache.setex(key, ttl, json.dumps(value, default=str))
        except Exception as exc:
            logger.debug("news_forecaster: cache write failed (non-fatal): %s", exc)

    async def _enqueue_for_batch_forecast(
        self,
        symbol: str,
        events: list[dict],
        indicators: dict[str, Any],
        regime: str | None,
        cache_key: str,
    ) -> None:
        """Publish a forecast request to the batch worker topic (fire-and-forget).

        Idempotent: a dedup key (FORECAST_ENQUEUE_DEDUP_TTL_SECS, default 1h —
        aligned with the consumer's staleness gate) prevents the same
        (symbol, news-set) from re-enqueuing while a prior request is still
        pending or fresh.  ``enqueued_at`` lets the consumer skip payloads
        older than an hour — the staleness gate that replaced the old
        producer-side queue trim.
        """
        if self._ml_cache is None:
            return

        dedup_key = f"cortex:forecast:batch:dedup:{cache_key}"
        try:
            acquired = await self._ml_cache.set(
                dedup_key, "1", nx=True,
                ex=get_settings().FORECAST_ENQUEUE_DEDUP_TTL_SECS,
            )
            if not acquired:
                return  # Already queued for this (symbol, news-set) combination
        except Exception as exc:
            logger.debug("news_forecaster: dedup check failed for %s — %s", symbol, exc)
            return

        # Slim events to only the fields needed for prompt rendering.
        slim_events = [
            {k: v for k, v in ev.items() if k in _BATCH_EVENT_FIELDS}
            for ev in events[:6]
        ]
        # Slim indicators to only numeric values (non-numeric are not rendered).
        slim_indicators = {
            k: float(v) for k, v in indicators.items() if isinstance(v, (int, float))
        }

        payload = {
            "symbol":      symbol,
            "events":      slim_events,
            "indicators":  slim_indicators,
            "regime":      regime,
            "cache_key":   cache_key,
            "enqueued_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await kafka_publish(KafkaTopics.FORECAST_BATCH, payload, key=symbol)
        except Exception as exc:
            logger.debug("news_forecaster: enqueue failed for %s — %s", symbol, exc)

    async def _write_forecast_audit(
        self,
        *,
        invocation_id: Any,
        prompt_hash: str,
        model_id: str,
        latency_ms: int,
        output: Any,
        error: str | None,
        usage: dict[str, Any] | None,
    ) -> None:
        """Append one ai_llm_audit_log row for the forecast call (governance).

        Uses its OWN session so it never commits the in-flight consensus
        transaction prematurely.  Never raises — audit failures are logged only.
        """
        from app.core.database import AsyncSessionLocal
        from app.core.metrics import llm_audit_log_writes_total

        provider, _, mid = (model_id or "gemini/").partition("/")
        preview: str | None = None
        if output is not None:
            preview = (
                f"{output.direction} conf={float(output.confidence):.2f}: "
                f"{output.rationale}"
            )[:500]
        try:
            async with AsyncSessionLocal() as audit_db:
                audit_db.add(AILLMAuditLog(
                    invocation_id=invocation_id,
                    invocation_type="news_forecast",
                    reference_table="ai_trading_signals",
                    reference_id=None,
                    model_provider=provider or "gemini",
                    model_id=mid or model_id,
                    prompt_hash=prompt_hash,
                    retrieved_source_ids=None,
                    input_tokens=(usage or {}).get("input_tokens"),
                    output_tokens=(usage or {}).get("output_tokens"),
                    latency_ms=latency_ms,
                    guardrail_events=[],
                    output_preview=preview,
                    error_message=error,
                ))
                await audit_db.commit()
            llm_audit_log_writes_total.labels(status="success").inc()
        except Exception as exc:
            llm_audit_log_writes_total.labels(status="failure").inc()
            logger.error("news_forecaster: audit write failed: %s", exc, exc_info=True)

    # ── Data freshness guard (daily OHLCV sync lag) ────────────────────────────

    async def _ohlcv_frontier(self, db: AsyncSession) -> Any:
        """Robust global daily-data frontier, cached briefly.

        The newest day on which at least STALENESS_FRONTIER_MIN_INSTRUMENTS daily
        bars exist — i.e. the day the *bulk* of the universe has reached.  Using a
        count threshold (not ``max``) makes it outlier-proof: a single instrument
        synced a day ahead cannot drag the frontier forward and flag the rest of
        the universe stale.  Windowed to 30 days so the scan stays cheap (~18ms).
        """
        s = get_settings()
        if self._frontier_cache is not None:
            val, at = self._frontier_cache
            if monotonic() - at < s.STALENESS_FRONTIER_CACHE_SECS:
                return val
        val = (await db.execute(sa_text(
            "SELECT date_trunc('day', timestamp) AS d FROM upstox_ohlcv "
            "WHERE timeframe='1D' AND timestamp > NOW() - make_interval(days => 30) "
            "GROUP BY 1 HAVING COUNT(*) >= :minc ORDER BY d DESC LIMIT 1"
        ), {"minc": s.STALENESS_FRONTIER_MIN_INSTRUMENTS})).scalar()
        self._frontier_cache = (val, monotonic())
        return val

    async def _resolve_instrument_key(self, db: AsyncSession, symbol: str) -> str | None:
        if "|" in symbol:
            return symbol
        return (await db.execute(sa_text(
            "SELECT instrument_key FROM instrument_master "
            "WHERE trading_symbol=:s AND exchange='NSE' AND instrument_type='EQ' LIMIT 1"
        ), {"s": symbol})).scalar()

    async def _data_stale(self, db: AsyncSession, symbol: str) -> tuple[bool, dict[str, Any]]:
        """True if the symbol's latest daily bar lags the global 1D frontier.

        Frontier-relative (no trading calendar needed): if the rest of the universe
        has a newer daily bar than this symbol, the daily sync hasn't reached it
        yet — so its technicals are stale and the consumers should abstain.  When
        freshness can't be determined (no frontier / unknown symbol) it returns
        False so the guard never blocks on missing data.
        """
        frontier = await self._ohlcv_frontier(db)
        instrument_key = await self._resolve_instrument_key(db, symbol)
        if frontier is None or instrument_key is None:
            return False, {}
        latest = (await db.execute(sa_text(
            "SELECT max(timestamp) FROM upstox_ohlcv "
            "WHERE instrument_key=:k AND timeframe='1D'"
        ), {"k": instrument_key})).scalar()
        if latest is None:
            return True, {"latest": None, "frontier": frontier.isoformat(), "lag_days": None}
        lag = (frontier.date() - latest.date()).days
        info = {"latest": latest.isoformat(), "frontier": frontier.isoformat(), "lag_days": lag}
        return lag > get_settings().STALENESS_MAX_LAG_DAYS, info

    async def gather_ml_signals(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1d",
    ) -> dict[str, Any]:
        """
        Gather ML ensemble prediction signals for a symbol.

        Results are cached in Redis for `_ML_CACHE_TTL` seconds (30 s) under
        `cortex:ml:signal:{symbol}:{timeframe}`.  This prevents redundant
        feature-loading DB queries + inference calls when the correlation loop
        fires the same symbol multiple times within a single cycle.  Cache
        failures are non-fatal — the full pipeline runs as a fallback.
        """
        if not self.ensemble_predictor or not self.feature_loader:
            logger.debug("ML prediction skipped — predictor not initialised")
            return {"score": 0.0, "confidence": 0.0, "model": None, "available": False}

        # Freshness guard: abstain on stale daily OHLCV (the sync hasn't reached
        # this symbol yet) rather than predict on stale technicals.  Self-heals
        # next cycle.  The `stale` flag also makes the news forecaster abstain.
        if get_settings().ENABLE_STALENESS_GUARD:
            stale, info = await self._data_stale(db, symbol)
            if stale:
                signal_staleness_abstentions_total.labels(component="ml").inc()
                logger.info(
                    "gather_ml_signals: abstaining on stale OHLCV — symbol=%s lag_days=%s",
                    symbol, info.get("lag_days"),
                )
                return {
                    "score": 0.0, "confidence": 0.0, "model": None,
                    "available": False, "stale": True, "reason": "stale_data",
                    "staleness": info,
                }

        cache_key = f"cortex:ml:signal:{symbol}:{timeframe}"

        _cache_hits = 0
        _cache_requests = 1
        if self._ml_cache is not None:
            try:
                cached = await self._ml_cache.get(cache_key)
                if cached:
                    logger.debug("ML signal cache hit — symbol=%s timeframe=%s", symbol, timeframe)
                    _cache_hits = 1
                    feature_cache_hit_rate.labels(cache_type="ml_signal").set(
                        _cache_hits / _cache_requests * 100
                    )
                    return json.loads(cached)
            except Exception as exc:
                logger.debug("ML signal cache read failed (non-fatal): %s", exc)
        feature_cache_hit_rate.labels(cache_type="ml_signal").set(0.0)

        try:
            # Capture the raw labeled indicators the model sees so the Gemini news
            # forecaster reasons over the identical numbers (see gather_news_forecast).
            indicator_snapshot: dict[str, float] = {}
            tabular, sequence, current_price, volatility = await self.feature_loader.load_features(
                symbol=symbol,
                timeframe=timeframe,
                indicator_snapshot_out=indicator_snapshot,
            )

            prediction = await self.ensemble_predictor.predict(
                features_tabular=tabular,
                features_sequence=sequence,
                symbol=symbol,
                current_price=current_price,
                volatility=volatility,
                timeframe=timeframe,
                use_cache=True,
            )

            direction_map = {"BUY": 100.0, "SELL": -100.0, "HOLD": 0.0}
            direction = prediction.get("direction_label", "HOLD")

            meta = prediction.get("metadata", {})
            result: dict[str, Any] = {
                "score": direction_map.get(direction, 0.0),
                # Raw calibrated probability from EnsemblePredictor (0.0–1.0).
                # conviction_scale is preserved in the prediction sub-dict for
                # downstream position-sizing consumers.
                "confidence": prediction.get("confidence", 0.0),
                "model": (
                    f"xgb_{meta.get('xgboost_version', '')}+"
                    f"gru_{meta.get('gru_version', '')}"
                ).strip("+"),
                "available": True,
                "prediction": {
                    "direction":       direction,
                    "conviction_scale": prediction.get("conviction_scale", 0.0),
                    "entry_price":     prediction.get("entry_price"),
                    "stop_loss":       prediction.get("stop_loss"),
                    "targets": [
                        prediction.get("tp1"),
                        prediction.get("tp2"),
                        prediction.get("tp3"),
                    ],
                    "probabilities": prediction.get("probabilities"),
                },
                # Per-model breakdown — each constituent model's calibrated view
                # before the weighted blend. Passed through to JSONB for the
                # serialiser to surface individually in contributing_factors.
                "models": prediction.get("models", {}),
                # Raw labeled indicators (RSI/MACD/EMA/ATR/…) the model saw, for the
                # Gemini news forecaster to reuse verbatim (identical numbers).
                "indicators": indicator_snapshot,
            }

            if self._ml_cache is not None:
                try:
                    await self._ml_cache.setex(cache_key, self._ML_CACHE_TTL, json.dumps(result))
                    logger.debug(
                        "ML signal cached — symbol=%s timeframe=%s ttl=%ds",
                        symbol, timeframe, self._ML_CACHE_TTL,
                    )
                except Exception as exc:
                    logger.debug("ML signal cache write failed (non-fatal): %s", exc)

            return result

        except Exception as exc:
            logger.error("ML prediction failed for %s: %s", symbol, exc, exc_info=True)
            feature_errors_total.labels(
                feature_name="ml_prediction", error_type=type(exc).__name__
            ).inc()
            return {"score": 0.0, "confidence": 0.0, "model": None, "available": False, "error": str(exc)}

    async def gather_technical_signals(
        self,
        db: AsyncSession,
        symbol: str,
        candle_timeframe: str = "1D",
    ) -> dict[str, Any]:
        """
        Gather technical indicator signals via RSI-14 and EMA-20/50 crossover.

        candle_timeframe matches the signal's time horizon:
          swing / positional → "1D"   (daily candles)
          intraday           → "1hour"

        Queries upstox_ohlcv via instrument_master join so the caller can pass
        a trading_symbol (e.g. "RELIANCE") regardless of the instrument_key format.
        All computation is done in-process with numpy — no extra dependencies.

        Returns neutral zero if fewer than 52 candles are available.
        """
        from sqlalchemy import text as sa_text

        # Map candle_timeframe to the value stored in upstox_ohlcv.timeframe
        tf_map = {
            "1D": "1D",
            "1d": "1D",
            "1hour": "1hour",
            "4hour": "4hour",
        }
        db_timeframe = tf_map.get(candle_timeframe, "1D")

        # Minimum candles: EMA-50 needs 50, plus a buffer
        min_candles = 52

        try:
            rows = await db.execute(
                sa_text(
                    """
                    SELECT o.close
                    FROM   upstox_ohlcv o
                    JOIN   instrument_master im
                           ON im.instrument_key = o.instrument_key
                    WHERE  im.trading_symbol = :symbol
                    AND    o.timeframe        = :tf
                    ORDER  BY o.timestamp DESC
                    LIMIT  :limit
                    """
                ),
                {"symbol": symbol, "tf": db_timeframe, "limit": min_candles + 10},
            )
            closes_raw = [float(r[0]) for r in rows.fetchall()]
        except Exception as exc:
            logger.debug("Technical signals DB query failed for %s: %s", symbol, exc)
            feature_errors_total.labels(
                feature_name="technical_indicators", error_type=type(exc).__name__
            ).inc()
            return {"score": 0.0, "confidence": 0.0, "indicators": {}, "available": False}

        if len(closes_raw) < min_candles:
            return {"score": 0.0, "confidence": 0.0, "indicators": {}, "available": False}

        # Rows are DESC — reverse to chronological order for indicator computation
        closes = np.array(closes_raw[::-1], dtype=np.float64)

        rsi = _compute_rsi(closes, period=14)
        ema20 = _compute_ema(closes, period=20)[-1]
        ema50 = _compute_ema(closes, period=50)[-1]

        # Score derivation (plan §5.1)
        if rsi > 60 and ema20 > ema50:
            score = 100.0    # strong bullish
        elif rsi > 55 or ema20 > ema50:
            score = 50.0     # mild bullish
        elif rsi < 40 and ema20 < ema50:
            score = -100.0   # strong bearish
        elif rsi < 45 or ema20 < ema50:
            score = -50.0    # mild bearish
        else:
            score = 0.0      # neutral

        # Confidence: how far RSI is from neutral (50), scaled to [0, 1]
        confidence = abs(rsi - 50.0) / 50.0

        return {
            "score": score,
            "confidence": confidence,
            "available": True,
            "indicators": {
                "rsi_14": round(rsi, 2),
                "ema_20": round(ema20, 2),
                "ema_50": round(ema50, 2),
                "ema_crossover": "bullish" if ema20 > ema50 else "bearish",
            },
        }

    # ── Fusion ─────────────────────────────────────────────────────────────────

    def fuse_signals(
        self,
        event_signals: dict[str, Any],
        ml_signals: dict[str, Any],
        technical_signals: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Weighted average fusion with dynamic weight renormalization.

        Sources marked `available=False` are excluded from the weight denominator
        so that unavailable sources (no predictor, insufficient candles) do not
        dilute the remaining valid sources by contributing spurious zero signals.

        When only one source is available the fused confidence is capped at 0.65
        to reflect reduced conviction from a single data stream.
        """
        sources: dict[str, tuple[float, bool]] = {
            "event":     (self.event_weight,     bool(event_signals.get("available", True))),
            "ml":        (self.ml_weight,        bool(ml_signals.get("available", True))),
            "technical": (self.technical_weight, bool(technical_signals.get("available", True))),
        }
        weights = self._renormalize_weights(sources)

        fused_score = (
            weights["event"]     * event_signals.get("score", 0.0)
            + weights["ml"]      * ml_signals.get("score", 0.0)
            + weights["technical"] * technical_signals.get("score", 0.0)
        )
        fused_confidence = (
            weights["event"]     * event_signals.get("confidence", 0.0)
            + weights["ml"]      * ml_signals.get("confidence", 0.0)
            + weights["technical"] * technical_signals.get("confidence", 0.0)
        )

        n_available = sum(1 for _, avail in sources.values() if avail)
        if n_available == 1:
            # Single-source cap: 0.85 allows a high-conviction calibrated
            # probability to surface while still reflecting reduced multi-stream
            # corroboration.
            fused_confidence = min(fused_confidence, 0.85)

        # Inclusive thresholds: a score of exactly ±50 is actionable.  Source
        # scores are frequently pinned to round values (technical scoring emits
        # exactly ±50), so an exclusive comparison silently demoted legitimate
        # boundary signals to HOLD.
        if fused_score >= 50:
            action = "BUY"
        elif fused_score <= -50:
            action = "SELL"
        else:
            action = "HOLD"

        return {
            "action": action,
            "confidence_score": fused_confidence,
            "fused_score": fused_score,
            "sources_available": n_available,
            "contributing_events": event_signals.get("events", []),
            "ml_predictions": ml_signals,
            "technical_indicators": technical_signals,
        }

    # ── Private helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _renormalize_weights(
        sources: dict[str, tuple[float, bool]],
    ) -> dict[str, float]:
        """
        Redistribute base weights across available sources only.

        Unavailable sources receive weight 0.0; their share is redistributed
        proportionally among available sources so weights always sum to 1.0.
        When *all* sources are unavailable the original weights are returned
        unchanged — the caller will produce a neutral zero signal regardless.
        """
        denom = sum(w for w, avail in sources.values() if avail)
        if denom == 0.0:
            return {k: w for k, (w, _) in sources.items()}
        return {k: (w / denom if avail else 0.0) for k, (w, avail) in sources.items()}

    @staticmethod
    def _derive_time_horizon(timeframe: str) -> str:
        """Map prediction timeframe to a signal time horizon."""
        return _TIMEFRAME_TO_HORIZON.get(timeframe, "swing")

    @staticmethod
    def _compute_expires_at(time_horizon: str, generated_at: datetime) -> datetime:
        """
        Compute signal expiry anchored to NSE market hours.

        intraday   → end of current NSE session (15:30 IST), or next session
                     if generated after close
        swing      → 5 trading days forward at 15:30 IST
        positional → 21 trading days forward at 15:30 IST
        """
        from app.services.market_calendar import nse_calendar

        generated_ist = generated_at.astimezone(_IST)

        if time_horizon == "intraday":
            close_today = datetime.combine(generated_ist.date(), _NSE_CLOSE, tzinfo=_IST)
            if generated_ist <= close_today:
                return close_today.astimezone(_UTC)
            # Generated after close — use next trading day's session end
            next_day = generated_ist.date() + timedelta(days=1)
            for _ in range(10):  # guard against long holiday runs
                if nse_calendar.is_trading_day(next_day):
                    break
                next_day += timedelta(days=1)
            return datetime.combine(next_day, _NSE_CLOSE, tzinfo=_IST).astimezone(_UTC)

        n_days = 5 if time_horizon == "swing" else 21
        current = generated_ist.date()
        count = 0
        while count < n_days:
            current += timedelta(days=1)
            if nse_calendar.is_trading_day(current):
                count += 1
        return datetime.combine(current, _NSE_CLOSE, tzinfo=_IST).astimezone(_UTC)

    @staticmethod
    def _extract_price_levels(
        ml_signals: dict[str, Any],
    ) -> tuple[Optional[float], Optional[float]]:
        """
        Extract target price (TP1 / entry_price) and stop loss from ML prediction.
        Returns (target_price, stop_loss) — both None when ML was unavailable.
        """
        prediction = ml_signals.get("prediction") or {}
        target = prediction.get("entry_price")
        stop = prediction.get("stop_loss")
        targets = prediction.get("targets") or []

        # Use TP1 as target_price when available and non-zero
        tp1 = targets[0] if targets else None
        target_price = tp1 if tp1 and tp1 != target else target

        return (
            float(target_price) if target_price else None,
            float(stop) if stop else None,
        )

    @staticmethod
    def _build_reasoning(
        action: str,
        confidence: float,
        time_horizon: str,
        event_signals: dict[str, Any],
        ml_signals: dict[str, Any],
        regime: Optional[str],
        target_price: Optional[float],
        stop_loss: Optional[float],
    ) -> str:
        """Build a human-readable signal reasoning string from assembled data."""
        band = next(label for threshold, label in _CONFIDENCE_BAND if confidence >= threshold)
        direction_str = action.capitalize()
        horizon_str = time_horizon.capitalize()

        dominant = "ML ensemble" if ml_signals.get("score", 0.0) != 0.0 else "event analysis"
        ml_conf_pct = f"{ml_signals.get('confidence', 0.0) * 100:.0f}%"
        event_count = event_signals.get("event_count", 0)

        parts = [
            f"{horizon_str} {direction_str} ({band} confidence · {confidence * 100:.1f}%):",
            f"{dominant} projects {'bullish' if action == 'BUY' else 'bearish' if action == 'SELL' else 'neutral'} momentum"
            f" with {ml_conf_pct} confidence.",
        ]

        if event_count > 0:
            parts.append(
                f"{event_count} recent market event{'s' if event_count > 1 else ''} reinforce the setup."
            )

        if regime and regime != "unknown":
            parts.append(f"Market regime: {regime.replace('_', ' ')}.")

        if stop_loss:
            parts.append(f"Stop loss: ₹{stop_loss:,.2f}.")
        if target_price:
            parts.append(f"Target: ₹{target_price:,.2f}.")

        return " ".join(parts)

    # ── Assembly ───────────────────────────────────────────────────────────────

    async def assemble_signal(
        self,
        db: AsyncSession,
        pubsub: PubSubClient,
        symbol: str,
        timeframe: str = "1d",
        precomputed_ml: Optional[dict[str, Any]] = None,
    ) -> AITradingSignal:
        """
        Assemble, persist, and broadcast a trading signal for a symbol.

        Args:
            db:             Async database session
            pubsub:         Redis pub/sub client (properly injected)
            symbol:         NSE trading symbol
            timeframe:      Prediction timeframe (drives time_horizon derivation)
            precomputed_ml: Pre-computed ML prediction dict from predict_batch
                            (scheduler Phase B).  When provided, gather_ml_signals
                            is skipped entirely — the batch result is used directly.

        Returns:
            Persisted AITradingSignal ORM instance
        """
        # ── Symbol eligibility check ───────────────────────────────────────────
        # Determines whether this signal is actionable (NSE-listed equity present
        # in instrument_master) or informational only (company referenced in news
        # but not listed / tracked on the platform).
        #
        # Critically: the assembler NEVER raises here.  Raising was the old
        # behaviour; it silently dropped Non-NSE signals from the pipeline.
        # The caller (API endpoint) is responsible for enforcing hard rejection
        # when an explicit user request targets an ineligible symbol — the
        # internal pipeline (event processor, scheduler) simply stores the signal
        # with is_nse_eligible=False so it surfaces in the Non-NSE tab.
        #
        # Controlled by ENABLE_SYMBOL_VALIDATION: when False (emergency / testing)
        # all symbols are treated as eligible.
        from app.core.config import get_settings as _get_settings  # noqa: PLC0415
        from app.services.symbol_validator import symbol_validator  # noqa: PLC0415

        _settings = _get_settings()
        _is_nse_eligible: bool = True
        if _settings.ENABLE_SYMBOL_VALIDATION:
            _is_nse_eligible = await symbol_validator.validate_symbol(symbol, db)

        # Resolve company name and instrument_key from instrument_master.
        # Both calls hit separate Redis keys warmed by the eligibility check above,
        # so overhead is negligible for any previously seen symbol.
        # Failures are non-fatal; fields stay None rather than blocking assembly.
        _company_name: str | None = None
        _instrument_key: str | None = None
        try:
            _company_name = await symbol_validator.get_company_name(symbol, db)
        except Exception:
            pass
        try:
            _instrument_key = await symbol_validator.get_instrument_key(symbol, db)
        except Exception:
            pass

        regime = await self.get_latest_regime(db)
        strategy_id = await self.get_active_strategy(db, regime) if regime else None

        if precomputed_ml is not None:
            # Batch scheduler path — ML inference already completed in Phase B.
            # Re-wrap the prediction dict into the same shape gather_ml_signals returns.
            direction_map = {"BUY": 100.0, "SELL": -100.0, "HOLD": 0.0}
            direction = precomputed_ml.get("direction_label", "HOLD")
            meta = precomputed_ml.get("metadata", {})
            ml_signals = {
                "score": direction_map.get(direction, 0.0),
                # Raw calibrated probability from batch inference (0.0–1.0).
                # conviction_scale preserved for position-sizing consumers.
                "confidence": precomputed_ml.get("confidence", 0.0),
                "model": (
                    f"xgb_{meta.get('xgboost_version', '')}+"
                    f"gru_{meta.get('gru_version', '')}"
                ).strip("+"),
                "available": True,
                "prediction": {
                    "direction":       direction,
                    "conviction_scale": precomputed_ml.get("conviction_scale", 0.0),
                    "entry_price":     precomputed_ml.get("entry_price"),
                    "stop_loss":       precomputed_ml.get("stop_loss"),
                    "targets": [
                        precomputed_ml.get("tp1"),
                        precomputed_ml.get("tp2"),
                        precomputed_ml.get("tp3"),
                    ],
                    "probabilities": precomputed_ml.get("probabilities"),
                },
                "models": precomputed_ml.get("models", {}),
            }
        else:
            ml_signals = await self.gather_ml_signals(db, symbol, timeframe)

        # AI/news slot: the Gemini news-conditioned forecast reasons over the ML's
        # own indicators + recent news, then maps to the event-slot shape.  Falls
        # back to the deterministic event score on any failure (never blocks).
        event_signals = await self.gather_news_forecast(
            db, symbol, ml_signal=ml_signals, regime=regime,
        )

        # Derive time_horizon once — used for technical candle resolution,
        # expiry computation, and reasoning string.
        time_horizon = self._derive_time_horizon(timeframe)
        candle_tf = "1hour" if time_horizon == "intraday" else "1D"
        technical_signals = await self.gather_technical_signals(db, symbol, candle_tf)

        fused = self.fuse_signals(event_signals, ml_signals, technical_signals)

        now_utc = datetime.now(timezone.utc)
        expires_at = self._compute_expires_at(time_horizon, now_utc)
        target_price, stop_loss = self._extract_price_levels(ml_signals)
        reasoning = self._build_reasoning(
            action=fused["action"],
            confidence=fused["confidence_score"],
            time_horizon=time_horizon,
            event_signals=event_signals,
            ml_signals=ml_signals,
            regime=regime,
            target_price=target_price,
            stop_loss=stop_loss,
        )

        signal = AITradingSignal(
            signal_timestamp=now_utc,
            symbol=symbol,
            company_name=_company_name,
            is_nse_eligible=_is_nse_eligible,
            action=fused["action"],
            confidence_score=Decimal(str(round(fused["confidence_score"], 2))),
            strategy_id=strategy_id or "default",
            regime_type=regime or "unknown",
            time_horizon=time_horizon,
            expires_at=expires_at,
            target_price=Decimal(str(round(target_price, 2))) if target_price else None,
            stop_loss=Decimal(str(round(stop_loss, 2))) if stop_loss else None,
            contributing_events=fused["contributing_events"],
            ml_predictions=fused["ml_predictions"],
            technical_indicators=fused["technical_indicators"],
            reasoning=reasoning,
            extra_data={"instrument_key": _instrument_key} if _instrument_key else None,
        )

        db.add(signal)
        await db.commit()
        await db.refresh(signal)

        signal_generation_total.labels(
            signal_type="trading_signal", action=signal.action
        ).inc()

        # Publish full signal payload so WebSocket subscribers and
        # useSignalsRealtime can hydrate the React Query cache directly.
        payload = serialise_signal(signal)
        await pubsub.publish_json(RedisChannels.signals_for_symbol(symbol), payload)
        await pubsub.publish_json(RedisChannels.SIGNALS_ALL, payload)

        # Prime the on-demand Redis cache so that any concurrent or subsequent
        # generate_on_demand() call for this symbol within the TTL window finds
        # a cache hit and skips re-generation — prevents duplicate assembly
        # regardless of which code path (scheduler, event pipeline, API) triggers first.
        from app.core.redis import get_redis
        from app.core.config import get_settings as _get_settings
        try:
            _settings = _get_settings()
            _redis = get_redis()
            await _redis.setex(
                f"cai:signals:on-demand:{symbol}",
                _settings.SIGNAL_ON_DEMAND_CACHE_TTL,
                "1",
            )
        except Exception:
            _redis = None
            pass  # cache priming is best-effort; never fail assembly over it

        # Fire strategy rule-engine dispatch as a non-blocking background task.
        # The dispatcher creates its own DB session — completely decoupled from
        # this session's lifecycle.  Any failure is logged; it never fails assembly.
        from app.services.strategy_engine.dispatcher import strategy_dispatcher as _dispatcher
        asyncio.create_task(
            _dispatcher.dispatch_background(
                signal_id=signal.id,
                symbol=symbol,
                action=signal.action,
                confidence_score=float(signal.confidence_score),
                regime_type=signal.regime_type or "unknown",
                time_horizon=time_horizon,
                technical_indicators=dict(signal.technical_indicators or {}),
                ml_predictions=dict(signal.ml_predictions or {}),
                redis=_redis,
            ),
            name=f"strategy_dispatch_{symbol}_{signal.id}",
        )

        logger.info(
            "Signal assembled — symbol=%s action=%s confidence=%.2f horizon=%s expires=%s",
            symbol,
            signal.action,
            float(signal.confidence_score),
            time_horizon,
            expires_at.isoformat(),
        )
        return signal

    # ── Regime / strategy helpers ──────────────────────────────────────────────

    async def get_latest_regime(self, db: AsyncSession) -> Optional[str]:
        """Return the most recently detected market regime type."""
        stmt = (
            select(AIRegimeDetection)
            .order_by(desc(AIRegimeDetection.detection_timestamp))
            .limit(1)
        )
        result = await db.execute(stmt)
        regime = result.scalar_one_or_none()
        return regime.regime_type if regime else None

    async def get_active_strategy(
        self, db: AsyncSession, regime_type: str
    ) -> Optional[str]:
        """Return the highest-priority active strategy for the current regime."""
        stmt = (
            select(AIActiveStrategy)
            .where(
                and_(
                    AIActiveStrategy.regime_type == regime_type,
                    AIActiveStrategy.status == "active",
                )
            )
            .order_by(desc(AIActiveStrategy.priority_level))
            .limit(1)
        )
        result = await db.execute(stmt)
        strategy = result.scalar_one_or_none()
        return strategy.strategy_id if strategy else None
