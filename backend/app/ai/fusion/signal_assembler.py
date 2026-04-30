"""
Signal Assembler Service

Assembles trading signals from multiple data sources:
  - Event signals with exponential temporal decay (48h lookback)
  - ML ensemble predictions (XGBoost + GRU via EnsemblePredictor)
  - Technical indicators (Phase 5 — RSI-14 / EMA crossover, stub until then)

Weights (Phase 2–4): event=0.50, ml=0.50, technical=0.00
Weights (Phase 5+):  event=0.35, ml=0.40, technical=0.25
"""
import logging
import math
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import (
    AIActiveStrategy,
    AIEventClassification,
    AINLPResult,
    AIProcessedEvent,
    AIRawEvent,
    AIRegimeDetection,
    AITradingSignal,
)
from app.ai.fusion.serializers import serialise_signal
from app.core.redis import PubSubClient, RedisChannels
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader

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

    def __init__(
        self,
        ensemble_predictor: Optional[EnsemblePredictor] = None,
        feature_loader: Optional[FeatureLoader] = None,
        event_decay_half_life_hours: float = 24.0,
        event_weight: float = 0.35,
        ml_weight: float = 0.40,
        technical_weight: float = 0.25,
    ):
        self.ensemble_predictor = ensemble_predictor
        self.feature_loader = feature_loader
        self.event_decay_half_life_hours = event_decay_half_life_hours
        self.event_weight = event_weight
        self.ml_weight = ml_weight
        self.technical_weight = technical_weight

        total_weight = event_weight + ml_weight + technical_weight
        if not math.isclose(total_weight, 1.0, rel_tol=1e-5):
            raise ValueError(f"Signal weights must sum to 1.0, got {total_weight}")

    # ── Public helpers ─────────────────────────────────────────────────────────

    def calculate_event_decay(
        self, age_hours: float, half_life_hours: Optional[float] = None
    ) -> float:
        """Exponential decay factor: 0.5 ^ (age_hours / half_life_hours)."""
        if age_hours < 0:
            raise ValueError(f"Event age cannot be negative: {age_hours}")
        half_life = half_life_hours or self.event_decay_half_life_hours
        return math.pow(0.5, age_hours / half_life)

    # ── Signal gathering ───────────────────────────────────────────────────────

    async def gather_event_signals(
        self,
        db: AsyncSession,
        symbol: str,
        lookback_hours: int = 48,
    ) -> dict[str, Any]:
        """
        Gather event signals with temporal decay and source provenance for a symbol.

        Traverses the full classification chain to pull article metadata from the
        originating raw event: AIEventClassification → AINLPResult → AIProcessedEvent
        → AIRawEvent.  LEFT JOINs are used so that events survive even when
        intermediate chain records are absent (e.g. NLP pass produced no record).
        """
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

        stmt = (
            select(
                AIEventClassification.id,
                AIEventClassification.event_type,
                AIEventClassification.impact_score,
                AIEventClassification.classification_confidence,
                AIEventClassification.decay_half_life_hours,
                AIEventClassification.created_at,
                AIRawEvent.source_url,
                AIRawEvent.source_name,
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
                )
            )
            .order_by(desc(AIEventClassification.created_at))
        )

        result = await db.execute(stmt)
        rows = result.all()

        if not rows:
            return {"score": 0.0, "confidence": 0.0, "event_count": 0, "events": []}

        total_score = 0.0
        total_confidence = 0.0
        now = datetime.now(timezone.utc)

        for row in rows:
            age_hours = (now - row.created_at).total_seconds() / 3600
            decay_factor = self.calculate_event_decay(age_hours, row.decay_half_life_hours)
            total_score += float(row.impact_score) * decay_factor
            total_confidence += float(row.classification_confidence) * decay_factor

        return {
            "score": total_score,
            "confidence": total_confidence / len(rows),
            "event_count": len(rows),
            "events": [
                {
                    "id": row.id,
                    "type": row.event_type,
                    "impact": float(row.impact_score),
                    "source_url": row.source_url or None,
                    "source_name": row.source_name or None,
                    "article_title": row.article_title or None,
                }
                for row in rows[:5]
            ],
        }

    async def gather_ml_signals(
        self,
        db: AsyncSession,
        symbol: str,
        timeframe: str = "1d",
    ) -> dict[str, Any]:
        """Gather ML ensemble prediction signals for a symbol."""
        if not self.ensemble_predictor or not self.feature_loader:
            logger.debug("ML prediction skipped — predictor not initialised")
            return {"score": 0.0, "confidence": 0.0, "model": None}

        try:
            tabular, sequence, current_price, volatility = await self.feature_loader.load_features(
                symbol=symbol,
                timeframe=timeframe,
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

            return {
                "score": direction_map.get(direction, 0.0),
                "confidence": prediction.get("confidence", 0.0),
                "model": prediction.get("metadata", {}).get("model_version"),
                "prediction": {
                    "direction": direction,
                    "entry_price": prediction.get("entry_price"),
                    "stop_loss": prediction.get("stop_loss"),
                    "targets": [
                        prediction.get("tp1"),
                        prediction.get("tp2"),
                        prediction.get("tp3"),
                    ],
                    "probabilities": prediction.get("probabilities"),
                },
            }

        except Exception as exc:
            logger.error("ML prediction failed for %s: %s", symbol, exc, exc_info=True)
            return {"score": 0.0, "confidence": 0.0, "model": None, "error": str(exc)}

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
            return {"score": 0.0, "confidence": 0.0, "indicators": {}}

        if len(closes_raw) < min_candles:
            return {"score": 0.0, "confidence": 0.0, "indicators": {}}

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
        """Weighted average fusion across all three signal sources."""
        fused_score = (
            self.event_weight * event_signals.get("score", 0.0)
            + self.ml_weight * ml_signals.get("score", 0.0)
            + self.technical_weight * technical_signals.get("score", 0.0)
        )
        fused_confidence = (
            self.event_weight * event_signals.get("confidence", 0.0)
            + self.ml_weight * ml_signals.get("confidence", 0.0)
            + self.technical_weight * technical_signals.get("confidence", 0.0)
        )

        if fused_score > 50:
            action = "BUY"
        elif fused_score < -50:
            action = "SELL"
        else:
            action = "HOLD"

        return {
            "action": action,
            "confidence_score": fused_confidence,
            "fused_score": fused_score,
            "contributing_events": event_signals.get("events", []),
            "ml_predictions": ml_signals,
            "technical_indicators": technical_signals,
        }

    # ── Private helpers ────────────────────────────────────────────────────────

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
        regime = await self.get_latest_regime(db)
        strategy_id = await self.get_active_strategy(db, regime) if regime else None

        event_signals = await self.gather_event_signals(db, symbol)

        if precomputed_ml is not None:
            # Batch scheduler path — ML inference already completed in Phase B.
            # Re-wrap the prediction dict into the same shape gather_ml_signals returns.
            direction_map = {"BUY": 100.0, "SELL": -100.0, "HOLD": 0.0}
            direction = precomputed_ml.get("direction_label", "HOLD")
            ml_signals = {
                "score": direction_map.get(direction, 0.0),
                "confidence": precomputed_ml.get("confidence", 0.0),
                "model": precomputed_ml.get("metadata", {}).get("model_version"),
                "prediction": {
                    "direction": direction,
                    "entry_price": precomputed_ml.get("entry_price"),
                    "stop_loss": precomputed_ml.get("stop_loss"),
                    "targets": [
                        precomputed_ml.get("tp1"),
                        precomputed_ml.get("tp2"),
                        precomputed_ml.get("tp3"),
                    ],
                    "probabilities": precomputed_ml.get("probabilities"),
                },
            }
        else:
            ml_signals = await self.gather_ml_signals(db, symbol, timeframe)
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
        )

        db.add(signal)
        await db.commit()
        await db.refresh(signal)

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
            await get_redis().setex(
                f"cai:signals:on-demand:{symbol}",
                _settings.SIGNAL_ON_DEMAND_CACHE_TTL,
                "1",
            )
        except Exception:
            pass  # cache priming is best-effort; never fail assembly over it

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
