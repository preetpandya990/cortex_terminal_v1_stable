# PATH: backend/app/services/hawk_eye.py
# ────────────────────────────────
"""
Cortex AI — HawkEye Multi-Timeframe Scanner
=============================================
HawkEye scans across multiple timeframes simultaneously for each instrument
and computes a composite signal based on cross-timeframe agreement.

Strength classification:
  strong   — 80%+ timeframes agree
  moderate — 60–79% agree
  weak     — < 60% agree

Composite score = weighted sum (higher timeframes weighted more heavily).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import CacheService
from app.exceptions import DatabaseError
from app.models.upstox_data import UpstoxOHLCV
from app.schemas.hawk_eye import (
    HawkEyeResponse,
    HawkEyeResult,
    HawkEyeSignal,
)
from app.services.indicators import TechnicalIndicators

logger = logging.getLogger(__name__)
settings = get_settings()

# Timeframes with weights — longer timeframes carry more weight
TIMEFRAME_WEIGHTS: dict[str, float] = {
    "1w": 3.0,
    "1d": 2.0,
    "4h": 1.5,
    "1h": 1.0,
    "15m": 0.5,
}

LOOKBACK_DAYS_BY_TIMEFRAME: dict[str, int] = {
    "1w": 180,
    "1d": 90,
    "4h": 30,
    "1h": 14,
    "15m": 7,
}

CACHE_KEY_PREFIX = "hawk_eye:results"


class HawkEyeService:
    """Multi-timeframe signal aggregation scanner."""

    def __init__(self, cache: CacheService) -> None:
        self._cache = cache
        self._indicators = TechnicalIndicators()

    async def scan(
        self,
        session: AsyncSession,
        timeframes: list[str] | None = None,
        *,
        force_refresh: bool = False,
    ) -> HawkEyeResponse:
        active_timeframes = timeframes or list(TIMEFRAME_WEIGHTS.keys())
        cache_key = f"{CACHE_KEY_PREFIX}:{'_'.join(sorted(active_timeframes))}"

        if not force_refresh:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return HawkEyeResponse(**cached)

        result = await self._run_scan(session, active_timeframes)

        await self._cache.set(
            cache_key,
            result.model_dump(mode="json"),
            ttl=settings.CACHE_TTL_SCANNER,
        )
        return result

    async def _run_scan(
        self, session: AsyncSession, timeframes: list[str]
    ) -> HawkEyeResponse:
        now = datetime.now(timezone.utc)
        max_lookback = max(LOOKBACK_DAYS_BY_TIMEFRAME.get(tf, 90) for tf in timeframes)
        since = now - timedelta(days=max_lookback)

        # Single bulk query for ALL timeframes at once
        stmt = (
            select(UpstoxOHLCV)
            .where(
                and_(
                    UpstoxOHLCV.timeframe.in_(timeframes),
                    UpstoxOHLCV.timestamp >= since,
                )
            )
            .order_by(UpstoxOHLCV.instrument_key, UpstoxOHLCV.timeframe, UpstoxOHLCV.timestamp)
        )
        try:
            rows = (await session.execute(stmt)).scalars().all()
        except Exception as exc:
            raise DatabaseError(f"HawkEye DB query failed: {exc}") from exc

        if not rows:
            return HawkEyeResponse(
                results=[],
                total=0,
                timeframes_scanned=timeframes,
                scanned_at=now.isoformat(),
            )

        # Group by (instrument_key, timeframe)
        grouped: dict[str, dict[str, list[UpstoxOHLCV]]] = {}
        for row in rows:
            grouped.setdefault(row.instrument_key, {}).setdefault(row.timeframe, []).append(row)

        results: list[HawkEyeResult] = []
        for instrument_key, tf_map in grouped.items():
            result = self._score_instrument(instrument_key, tf_map, timeframes)
            if result:
                results.append(result)

        results.sort(key=lambda r: abs(r.composite_score), reverse=True)

        logger.info(
            "HawkEye scan complete: %d instruments, %d timeframes",
            len(results),
            len(timeframes),
        )
        return HawkEyeResponse(
            results=results,
            total=len(results),
            timeframes_scanned=timeframes,
            scanned_at=now.isoformat(),
        )

    def _score_instrument(
        self,
        instrument_key: str,
        tf_map: dict[str, list[UpstoxOHLCV]],
        expected_timeframes: list[str],
    ) -> HawkEyeResult | None:
        tf_signals: list[HawkEyeSignal] = []
        weighted_score = 0.0
        total_weight = 0.0

        for tf in expected_timeframes:
            candles = tf_map.get(tf, [])
            if len(candles) < 20:
                continue

            closes = [float(c.close) for c in candles]
            weight = TIMEFRAME_WEIGHTS.get(tf, 1.0)

            # RSI signal
            rsi_vals = self._indicators.rsi(closes)
            defined_rsi = [v for v in rsi_vals if v is not None]
            if defined_rsi:
                rsi = defined_rsi[-1]
                if rsi < 30:
                    tf_signals.append(HawkEyeSignal(name="RSI_OVERSOLD", direction="buy", value=round(rsi, 2), timeframe=tf))
                    weighted_score += weight * 2
                elif rsi > 70:
                    tf_signals.append(HawkEyeSignal(name="RSI_OVERBOUGHT", direction="sell", value=round(rsi, 2), timeframe=tf))
                    weighted_score -= weight * 2
                total_weight += weight

            # MACD signal
            macd = self._indicators.macd(closes)
            if macd and len(macd.macd_line) >= 2:
                m = [v for v in macd.macd_line if v is not None]
                s = [v for v in macd.signal_line if v is not None]
                if len(m) >= 2 and len(s) >= 2:
                    if m[-1] > s[-1] and m[-2] <= s[-2]:
                        tf_signals.append(HawkEyeSignal(name="MACD_CROSS_UP", direction="buy", value=round(m[-1] - s[-1], 4), timeframe=tf))
                        weighted_score += weight * 3
                    elif m[-1] < s[-1] and m[-2] >= s[-2]:
                        tf_signals.append(HawkEyeSignal(name="MACD_CROSS_DOWN", direction="sell", value=round(m[-1] - s[-1], 4), timeframe=tf))
                        weighted_score -= weight * 3

        if not tf_signals:
            return None

        # Composite direction
        buy_count = sum(1 for s in tf_signals if s.direction == "buy")
        sell_count = sum(1 for s in tf_signals if s.direction == "sell")
        total_directional = buy_count + sell_count

        if total_directional == 0:
            composite_signal = "neutral"
            agreement_pct = 0.0
        elif buy_count > sell_count:
            composite_signal = "buy"
            agreement_pct = round(buy_count / total_directional * 100, 1)
        else:
            composite_signal = "sell"
            agreement_pct = round(sell_count / total_directional * 100, 1)

        if agreement_pct >= 80:
            strength = "strong"
        elif agreement_pct >= 60:
            strength = "moderate"
        else:
            strength = "weak"

        last_candle = max(
            (c for tf_candles in tf_map.values() for c in tf_candles),
            key=lambda c: c.timestamp,
        )

        parts = instrument_key.split("|")
        trading_symbol = parts[-1] if len(parts) > 1 else instrument_key
        exchange = parts[0].split("_")[0] if "_" in parts[0] else parts[0]

        return HawkEyeResult(
            instrument_key=instrument_key,
            trading_symbol=trading_symbol,
            exchange=exchange,
            composite_signal=composite_signal,
            composite_score=round(weighted_score, 3),
            timeframe_signals=tf_signals,
            last_price=float(last_candle.close),
            strength=strength,
            agreement_pct=agreement_pct,
            scanned_at=datetime.now(timezone.utc).isoformat(),
        )