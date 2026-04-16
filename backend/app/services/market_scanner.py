# PATH: backend/app/services/market_scanner.py
# ──────────────────────────────────────
"""
Cortex AI — Market Scanner Service
=====================================
Scans stock OHLCV data with proper symbol→instrument_key mapping.
Uses TimescaleDB hypertable for optimal time-series query performance.

Performance profile:
  Single bulk query with JOIN → in-memory grouping → indicator computation
  ~10-50ms for 500+ instruments
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import CacheService
from app.exceptions import DatabaseError
from app.models.upstox_data import StockOHLCV, InstrumentMaster
from app.services.indicators import TechnicalIndicators
from app.schemas.scanner import ScanResult, ScanSignal

logger = logging.getLogger(__name__)
settings = get_settings()

CANDLE_LOOKBACK_DAYS = 60  # how many days of OHLCV to pull for indicators


class MarketScannerService:
    """
    Scans all instruments in the DB with a single query, computes technical
    signals in-memory, and caches the result.
    """

    def __init__(self, cache: CacheService) -> None:
        self._cache = cache
        self._indicators = TechnicalIndicators()

    async def scan_all(
        self,
        session: AsyncSession,
        timeframe: str = "1d",
        *,
        force_refresh: bool = False,
    ) -> list[ScanResult]:
        cache_key = f"scanner:results:{timeframe}"

        if not force_refresh:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Scanner cache hit for timeframe=%s", timeframe)
                return [ScanResult(**r) for r in cached]

        results = await self._run_scan(session, timeframe)

        await self._cache.set(
            cache_key,
            [r.model_dump(mode="json") for r in results],
            ttl=settings.CACHE_TTL_SCANNER,
        )
        return results

    async def _run_scan(
        self, session: AsyncSession, timeframe: str
    ) -> list[ScanResult]:
        """
        Single bulk query with JOIN → in-memory grouping → indicator computation.
        
        Joins stock_ohlcv (TimescaleDB) with instrument_master to get instrument_key.
        """
        since = datetime.now(timezone.utc) - timedelta(days=CANDLE_LOOKBACK_DAYS)

        # ── 1. Fetch all OHLCV data with instrument mapping in one query ───
        stmt = (
            select(
                StockOHLCV,
                InstrumentMaster.instrument_key
            )
            .join(
                InstrumentMaster,
                and_(
                    StockOHLCV.symbol == InstrumentMaster.symbol,
                    StockOHLCV.exchange == InstrumentMaster.exchange_segment
                ),
                isouter=False
            )
            .where(
                and_(
                    StockOHLCV.timeframe == timeframe,
                    StockOHLCV.timestamp >= since,
                )
            )
            .order_by(InstrumentMaster.instrument_key, StockOHLCV.timestamp)
        )
        try:
            rows = (await session.execute(stmt)).all()
        except Exception as exc:
            raise DatabaseError(f"Scanner DB query failed: {exc}") from exc

        if not rows:
            logger.info("Scanner: no OHLCV data found for timeframe=%s", timeframe)
            return []

        # ── 2. Group by instrument_key in Python ──────────────────────────
        grouped: dict[str, list[StockOHLCV]] = {}
        for ohlcv, instrument_key in rows:
            grouped.setdefault(instrument_key, []).append(ohlcv)

        # ── 3. Compute signals for each instrument ─────────────────────────
        results: list[ScanResult] = []
        failed: list[str] = []

        for instrument_key, candles in grouped.items():
            try:
                result = self._score_instrument(instrument_key, candles)
                if result:
                    results.append(result)
            except Exception:
                logger.exception("Signal computation failed for %s", instrument_key)
                failed.append(instrument_key)

        if failed:
            logger.warning(
                "Scanner: %d instruments failed signal computation: %s",
                len(failed),
                failed[:10],
            )

        results.sort(key=lambda r: r.score, reverse=True)
        logger.info(
            "Scanner complete: %d/%d instruments scored, %d failed",
            len(results),
            len(grouped),
            len(failed),
        )
        return results

    def _score_instrument(
        self, instrument_key: str, candles: list[StockOHLCV]
    ) -> ScanResult | None:
        if len(candles) < 20:
            return None  # insufficient data for meaningful signals

        closes = [float(c.close) for c in candles]
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        volumes = [float(c.volume) for c in candles]
        last_price = closes[-1]
        previous_close = closes[-2] if len(closes) > 1 else None
        price_change = last_price - previous_close if previous_close else 0.0
        price_change_pct = (price_change / previous_close * 100) if previous_close else 0.0

        signals: list[ScanSignal] = []
        buy_score = 0
        sell_score = 0

        # RSI
        rsi_values = self._indicators.rsi(closes)
        rsi_value: float | None = None
        if rsi_values:
            rsi = next((v for v in reversed(rsi_values) if v is not None), None)
            rsi_value = rsi
            if rsi is not None and rsi < 30:
                signals.append(ScanSignal(name="RSI_OVERSOLD", value=rsi, direction="buy"))
                buy_score += 2
            elif rsi is not None and rsi > 70:
                signals.append(ScanSignal(name="RSI_OVERBOUGHT", value=rsi, direction="sell"))
                sell_score += 2

        # MACD
        macd_line, signal_line, histogram = self._indicators.macd(closes)
        if macd_line and signal_line:
            if macd_line[-1] > signal_line[-1] and macd_line[-2] <= signal_line[-2]:
                signals.append(ScanSignal(name="MACD_CROSSOVER", value=histogram[-1], direction="buy"))
                buy_score += 3
            elif macd_line[-1] < signal_line[-1] and macd_line[-2] >= signal_line[-2]:
                signals.append(ScanSignal(name="MACD_CROSSUNDER", value=histogram[-1], direction="sell"))
                sell_score += 3

        # Bollinger Bands
        bb = self._indicators.bollinger_bands(closes)
        if bb:
            upper, middle, lower = bb
            price = closes[-1]
            if price < lower[-1]:
                signals.append(ScanSignal(name="BB_BELOW_LOWER", value=price, direction="buy"))
                buy_score += 2
            elif price > upper[-1]:
                signals.append(ScanSignal(name="BB_ABOVE_UPPER", value=price, direction="sell"))
                sell_score += 2

        # Volume surge
        avg_volume = sum(volumes[-20:-1]) / 19 if len(volumes) >= 20 else None
        volume_ratio = (volumes[-1] / avg_volume) if avg_volume else 0.0
        if avg_volume and volumes[-1] > avg_volume * 2:
            signals.append(ScanSignal(name="VOLUME_SURGE", value=volumes[-1], direction="neutral"))
            buy_score += 1

        total_score = buy_score - sell_score
        if buy_score > sell_score:
            overall = "buy"
        elif sell_score > buy_score:
            overall = "sell"
        else:
            overall = "neutral"

        return ScanResult(
            instrument_key=instrument_key,
            signal=overall,
            score=total_score,
            signals=signals,
            last_price=last_price,
            previous_close=previous_close,
            price_change=price_change,
            price_change_pct=price_change_pct,
            volume=volumes[-1] if volumes else None,
            avg_volume=avg_volume,
            volume_ratio=volume_ratio,
            rsi=rsi_value,
            scanned_at=datetime.now(timezone.utc),
            warnings=[],
        )
