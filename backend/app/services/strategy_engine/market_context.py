"""
MarketContext — Signal-time snapshot for strategy rule evaluation.

Resolves an AITradingSignal (plus optional Redis LTP) into a flat, typed
dataclass.  The rule evaluator references fields by their string name so the
mapping between Pydantic ConditionLeaf.field strings and Python attributes
must remain stable.  Any rename here requires a corresponding schema migration.

Field namespace (stable contract):
    Signal-level    : action, confidence_score, regime_type, time_horizon, symbol
    Technical (JSONB): rsi_14, ema_20, ema_50, ema_crossover
    ML (JSONB)      : ml_direction, ml_confidence, entry_price, stop_loss,
                      take_profit_1, take_profit_2, take_profit_3
    Market          : ltp, ltp_age_seconds, ltp_stale
"""
from __future__ import annotations

import logging
import time as _time
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# Seconds after which an LTP value is considered stale.
# cai:ltp:{key} has a 5-minute TTL; we flag stale a bit earlier to avoid
# acting on prices that could expire before the order reaches the exchange.
_LTP_STALENESS_THRESHOLD_S: float = 240.0


@dataclass(frozen=True)
class MarketContext:
    """
    Immutable snapshot of all fields available to the rule evaluator.

    Naming mirrors the stable ConditionLeaf.field contract — do not rename
    fields without a corresponding schema migration.
    """

    # ── Signal-level ──────────────────────────────────────────────────────────
    symbol: str
    action: str                       # "BUY" | "SELL" | "HOLD"
    confidence_score: float           # 0.0 – 1.0  (normalised from DB Numeric)
    confidence_score_pct: float       # 0.0 – 100.0 (convenience alias, = *100)
    regime_type: str                  # e.g. "bull_trending"
    time_horizon: str                 # "intraday" | "swing" | "positional"

    # ── Technical indicators ──────────────────────────────────────────────────
    rsi_14: float | None
    ema_20: float | None
    ema_50: float | None
    ema_crossover: str | None         # "bullish" | "bearish"

    # ── ML prediction ─────────────────────────────────────────────────────────
    ml_direction: str | None          # "BUY" | "SELL" | "HOLD"
    ml_confidence: float | None       # 0.0 – 1.0
    entry_price: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    take_profit_3: float | None

    # ── Market / Redis ────────────────────────────────────────────────────────
    ltp: float | None                 # last traded price from Redis
    ltp_age_seconds: float | None     # seconds since the LTP was written
    ltp_stale: bool                   # True when age > _LTP_STALENESS_THRESHOLD_S

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
        return None if (f != f) else f  # reject NaN
    except (TypeError, ValueError):
        return None


async def resolve_market_context(
    *,
    symbol: str,
    action: str,
    confidence_score: float,
    regime_type: str,
    time_horizon: str,
    technical_indicators: dict[str, Any],
    ml_predictions: dict[str, Any],
    instrument_key: str | None = None,
    redis: Redis | None = None,
) -> MarketContext:
    """
    Build a MarketContext from the raw fields produced by SignalAssembler.

    Args:
        symbol:               NSE trading symbol (e.g. "RELIANCE")
        action:               "BUY" | "SELL" | "HOLD"
        confidence_score:     Fused confidence 0.0–1.0
        regime_type:          Market regime string
        time_horizon:         "intraday" | "swing" | "positional"
        technical_indicators: JSONB dict from AITradingSignal
        ml_predictions:       JSONB dict from AITradingSignal
        instrument_key:       Upstox instrument key for Redis LTP lookup
        redis:                Optional Redis client; LTP fields are None when absent

    Returns:
        Frozen MarketContext ready for rule evaluation.
    """
    # ── Technical indicators ──────────────────────────────────────────────────
    indicators: dict[str, Any] = technical_indicators.get("indicators") or technical_indicators
    rsi_14 = _safe_float(indicators.get("rsi_14"))
    ema_20 = _safe_float(indicators.get("ema_20"))
    ema_50 = _safe_float(indicators.get("ema_50"))
    ema_crossover = indicators.get("ema_crossover") or None

    # ── ML predictions ────────────────────────────────────────────────────────
    prediction: dict[str, Any] = ml_predictions.get("prediction") or {}
    ml_direction = prediction.get("direction") or None
    ml_confidence = _safe_float(ml_predictions.get("confidence"))
    entry_price = _safe_float(prediction.get("entry_price"))
    stop_loss = _safe_float(prediction.get("stop_loss"))
    targets: list = prediction.get("targets") or []
    take_profit_1 = _safe_float(targets[0]) if len(targets) > 0 else None
    take_profit_2 = _safe_float(targets[1]) if len(targets) > 1 else None
    take_profit_3 = _safe_float(targets[2]) if len(targets) > 2 else None

    # ── Redis LTP ─────────────────────────────────────────────────────────────
    ltp: float | None = None
    ltp_age_seconds: float | None = None
    ltp_stale: bool = False

    if redis is not None and instrument_key:
        redis_key = f"cai:ltp:{instrument_key}"
        try:
            raw = await redis.get(redis_key)
            if raw is not None:
                ltp = _safe_float(raw)
                # Derive age from TTL; Redis TTL ≈ remaining seconds, total = 300s
                ttl = await redis.ttl(redis_key)
                if ttl > 0:
                    ltp_age_seconds = max(0.0, float(300 - ttl))
                    ltp_stale = ltp_age_seconds >= _LTP_STALENESS_THRESHOLD_S
        except Exception:
            logger.debug("Redis LTP lookup failed for %s — proceeding without LTP", instrument_key)

    return MarketContext(
        symbol=symbol,
        action=action,
        confidence_score=confidence_score,
        confidence_score_pct=round(confidence_score * 100, 2),
        regime_type=regime_type,
        time_horizon=time_horizon,
        rsi_14=rsi_14,
        ema_20=ema_20,
        ema_50=ema_50,
        ema_crossover=ema_crossover,
        ml_direction=ml_direction,
        ml_confidence=ml_confidence,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit_1=take_profit_1,
        take_profit_2=take_profit_2,
        take_profit_3=take_profit_3,
        ltp=ltp,
        ltp_age_seconds=ltp_age_seconds,
        ltp_stale=ltp_stale,
    )
