# PATH: backend/app/services/market_scanner.py
# ──────────────────────────────────────────────
"""
Cortex AI — Market Scanner Service
=====================================
Scans all NSE instruments and ranks them by price movement and volume activity.

Data architecture:
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  DB  ─►  quorum-based reference session  ─►  prev_close, volume history │
  │  Upstox (market OPEN)  ─►  live LTP + today's volume                   │
  │  Upstox (market CLOSED) ─►  skipped; DB data used for both sides       │
  └─────────────────────────────────────────────────────────────────────────┘

Reference session:
  The most recent trading session where ≥90% of known instruments have data.
  This guards against partial ingestion making a "latest" date meaningless.
  Example: April 23 has 2 instruments → FAIL. April 22 has 2,544 → PASS ✓

When market is OPEN:
  prev_close  = reference session close (DB)
  ltp         = live last traded price   (Upstox /v3/market-quote/ohlc)
  volume      = today's accumulated volume (Upstox)
  change%     = (ltp − prev_close) / prev_close × 100

When market is CLOSED:
  prev_close  = session before reference (DB candles[1])
  ltp         = reference session close  (DB candles[0])
  volume      = reference session volume (DB)
  change%     = end-of-day movement of the reference session

Volume spike:
  volume_ratio = current_volume / 20-day_avg_volume
  Threshold: ≥ 2× average  →  meaningful activity
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis import CacheService
from app.exceptions import DatabaseError
from app.schemas.scanner import ScanProgressEvent, ScanResult, ScanSignal
from app.services.indicators import TechnicalIndicators
from app.services.market_calendar import nse_calendar
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Constants ──────────────────────────────────────────────────────────────────
_UPSTOX_BATCH_SIZE = 500       # Upstox hard limit per market-quote request
_QUORUM_FRACTION = 0.90        # ≥90% instruments required for a session to be "complete"
_CANDLE_LOOKBACK_DAYS = 90     # calendar days; covers ~60 NSE trading days
_STALENESS_EXCLUDE_DAYS = 14   # exclude instruments whose latest candle is older than this
_MIN_CANDLES_RSI = 16          # minimum candles required for a valid RSI(14)
_MIN_CANDLES_VOL_AVG = 5       # minimum candles required for a meaningful volume average
_VOLUME_SPIKE_THRESHOLD = 2.0  # current_volume / avg_volume ≥ this → volume spike

_TIMEFRAME_MAP: dict[str, str] = {
    "1d": "1D",
    "1w": "1week",
    "1h": "1hour",
}


# ── Internal data structures ───────────────────────────────────────────────────

@dataclass(slots=True)
class _Candle:
    ts: datetime
    close: float
    volume: float


@dataclass(slots=True)
class _InstrumentData:
    instrument_key: str
    trading_symbol: str | None
    name: str | None
    candles: list[_Candle] = field(default_factory=list)  # newest → oldest


@dataclass(slots=True)
class _LiveQuote:
    ltp: float
    cp: float      # Upstox official previous close (yesterday's NSE closing price)
    volume: float


# ── Service ────────────────────────────────────────────────────────────────────

class MarketScannerService:
    """
    Production-grade NSE market scanner.

    Lifecycle:
      Instantiated per-request (lightweight — holds no mutable state beyond
      the injected CacheService and stateless TechnicalIndicators).
    """

    def __init__(self, cache: CacheService) -> None:
        self._cache = cache
        self._indicators = TechnicalIndicators()

    # ── Public API ─────────────────────────────────────────────────────────

    async def scan_all(
        self,
        session: AsyncSession,
        upstox_client: UpstoxClient,
        timeframe: str = "1d",
        *,
        force_refresh: bool = False,
    ) -> tuple[list[ScanResult], bool]:
        """
        Run a full market scan.  Returns (results, live_prices_available).
        Returns cached results when available unless force_refresh=True.
        Always returns an empty list rather than raising on data unavailability.

        Delegates to scan_all_stream() so the streaming and non-streaming paths
        share a single implementation.
        """
        cache_key = f"scanner:results:v2:{timeframe}"

        if not force_refresh:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                logger.debug("Scanner cache hit  timeframe=%s", timeframe)
                return (
                    [ScanResult(**r) for r in cached["results"]],
                    cached.get("live_prices_available", True),
                )

        results: list[ScanResult] = []
        live_prices_available = False

        async for event in self.scan_all_stream(session, upstox_client, timeframe):
            if event.results is not None:
                results = event.results
                live_prices_available = event.live_prices_available

        ttl = (
            settings.CACHE_TTL_SCANNER_OPEN
            if nse_calendar.get_session().is_open_now
            else settings.CACHE_TTL_SCANNER_CLOSED
        )
        await self._cache.set(
            cache_key,
            {
                "results": [r.model_dump(mode="json") for r in results],
                "live_prices_available": live_prices_available,
            },
            ttl=ttl,
        )
        return results, live_prices_available

    async def scan_all_stream(
        self,
        session: AsyncSession,
        upstox_client: UpstoxClient,
        timeframe: str = "1d",
    ) -> AsyncGenerator[ScanProgressEvent, None]:
        """
        Async generator that yields ScanProgressEvent at each pipeline stage.

        Progress events carry pct/stage/message only.
        The terminal event additionally carries results + live_prices_available.
        Always performs a fresh scan — cache logic lives in scan_all().
        """
        from zoneinfo import ZoneInfo
        _IST = ZoneInfo("Asia/Kolkata")

        db_timeframe = _TIMEFRAME_MAP.get(timeframe, timeframe)

        yield ScanProgressEvent(pct=2, stage="init", message="Initializing scan…")

        # ── Stage 1: quorum-based reference session ────────────────────────
        ref_ts = await self._get_reference_session(session, db_timeframe)
        if ref_ts is None:
            logger.warning(
                "Scanner stream: no complete reference session (timeframe=%s) — "
                "ensure ingestion has run recently.",
                timeframe,
            )
            yield ScanProgressEvent(
                pct=2,
                stage="error",
                message="No complete reference session found — ensure ingestion has run recently.",
            )
            return

        ref_label = ref_ts.astimezone(_IST).strftime("%d %b %Y")
        logger.info("Scanner stream reference session: %s  timeframe=%s", ref_label, timeframe)
        yield ScanProgressEvent(pct=15, stage="reference_session", message=f"Reference session: {ref_label}")

        # ── Stage 2: DB baselines ─────────────────────────────────────────
        instrument_data = await self._fetch_db_baselines(session, ref_ts, db_timeframe)
        if not instrument_data:
            logger.warning(
                "Scanner stream: no instrument data after staleness filter  ref=%s",
                ref_ts.isoformat(),
            )
            yield ScanProgressEvent(
                pct=15,
                stage="error",
                message="No instrument data found after staleness filter.",
            )
            return

        n = len(instrument_data)
        yield ScanProgressEvent(pct=40, stage="db_baselines", message=f"Loaded baselines for {n:,} instruments")

        # ── Stage 3: market state ─────────────────────────────────────────
        now_utc = datetime.now(timezone.utc)
        try:
            await asyncio.wait_for(nse_calendar.refresh_if_needed(), timeout=1.0)
        except Exception:
            pass
        market_session = nse_calendar.get_session(now_utc)
        is_market_open = market_session.is_open_now
        yield ScanProgressEvent(
            pct=45,
            stage="market_state",
            message="Market open — fetching live prices" if is_market_open else "Market closed — using end-of-day data",
        )

        # ── Stage 4: live quotes ──────────────────────────────────────────
        live_quotes: dict[str, _LiveQuote] = {}
        live_prices_available = False
        if upstox_client.has_token and is_market_open:
            yield ScanProgressEvent(
                pct=50,
                stage="live_quotes",
                message=f"Fetching live quotes for {n:,} instruments…",
            )
            live_quotes = await self._fetch_live_quotes(
                upstox_client, list(instrument_data.keys()), instrument_data
            )
            live_prices_available = len(live_quotes) > 0
            logger.info("Scanner stream: Upstox returned %d/%d quotes (ltp+cp)", len(live_quotes), n)
            yield ScanProgressEvent(
                pct=80,
                stage="live_quotes",
                message=f"Received {len(live_quotes):,} live quotes from Upstox",
            )
        else:
            if not upstox_client.has_token:
                logger.warning("Scanner stream: no Upstox token — falling back to DB close prices")
                fallback_msg = "No Upstox token — using stored close prices"
            else:
                logger.debug("Scanner stream: market closed — skipping live quotes, using DB close prices")
                fallback_msg = "Market closed — using end-of-day close prices"
            yield ScanProgressEvent(
                pct=80,
                stage="live_quotes",
                message=fallback_msg,
            )

        # ── Stage 5: score + sort ─────────────────────────────────────────
        yield ScanProgressEvent(pct=85, stage="scoring", message="Computing signals and scores…")
        results = self._build_results(instrument_data, live_quotes, is_market_open, now_utc)
        results.sort(key=lambda r: abs(r.price_change_pct), reverse=True)

        logger.info(
            "Scanner stream complete: %d instruments scored  live=%d  market_open=%s",
            len(results), len(live_quotes), is_market_open,
        )
        yield ScanProgressEvent(pct=95, stage="scoring", message=f"Scored {len(results):,} instruments")

        # ── Terminal: complete ────────────────────────────────────────────
        yield ScanProgressEvent(
            pct=100,
            stage="complete",
            message=f"Scan complete — {len(results):,} instruments evaluated",
            results=results,
            live_prices_available=live_prices_available,
        )

    # ── DB: reference session ──────────────────────────────────────────────

    async def _get_reference_session(
        self,
        session: AsyncSession,
        db_timeframe: str,
    ) -> datetime | None:
        """
        Return the timestamp of the most recent trading session where ≥90%
        of the known instrument universe has OHLCV data.

        Bounded to the last 30 days so the GROUP BY stays efficient even as
        the table grows to hundreds of millions of rows.  The composite index
        idx_upstox_timeframe_ts (timeframe, timestamp DESC, instrument_key)
        makes this query sub-millisecond.
        """
        query = text("""
            WITH universe AS (
                SELECT COUNT(DISTINCT instrument_key) AS total
                FROM   upstox_ohlcv
                WHERE  timeframe = :timeframe
            )
            SELECT   o.timestamp AS ref_ts
            FROM     upstox_ohlcv  o
            CROSS JOIN universe    u
            WHERE    o.timeframe  = :timeframe
              AND    o.timestamp >= NOW() - INTERVAL '30 days'
            GROUP BY o.timestamp, u.total
            HAVING   COUNT(DISTINCT o.instrument_key) >= FLOOR(u.total * :quorum)
            ORDER BY o.timestamp DESC
            LIMIT    1
        """)
        try:
            result = await session.execute(
                query,
                {"timeframe": db_timeframe, "quorum": _QUORUM_FRACTION},
            )
            row = result.fetchone()
            return row.ref_ts if row else None
        except Exception as exc:
            raise DatabaseError(f"Reference session query failed: {exc}") from exc

    # ── DB: candle baselines ───────────────────────────────────────────────

    async def _fetch_db_baselines(
        self,
        session: AsyncSession,
        ref_ts: datetime,
        db_timeframe: str,
    ) -> dict[str, _InstrumentData]:
        """
        Fetch up to _CANDLE_LOOKBACK_DAYS of OHLCV candles per instrument,
        bounded at ref_ts (inclusive upper bound — no future data leaks in).

        Returns instruments sorted newest→oldest per instrument_key.
        Instruments whose most recent candle is older than _STALENESS_EXCLUDE_DAYS
        are dropped entirely.
        """
        since = ref_ts - timedelta(days=_CANDLE_LOOKBACK_DAYS)

        query = text("""
            SELECT
                o.instrument_key,
                o.timestamp,
                o.close,
                o.volume,
                im.trading_symbol,
                im.name
            FROM       upstox_ohlcv      o
            LEFT JOIN  instrument_master  im  USING (instrument_key)
            WHERE  o.timeframe  = :timeframe
              AND  o.timestamp <= :ref_ts
              AND  o.timestamp >= :since
            ORDER BY o.instrument_key, o.timestamp DESC
        """)
        try:
            result = await session.execute(
                query,
                {"timeframe": db_timeframe, "ref_ts": ref_ts, "since": since},
            )
            rows = result.fetchall()
        except Exception as exc:
            raise DatabaseError(f"DB baseline query failed: {exc}") from exc

        if not rows:
            return {}

        # Group rows into InstrumentData (one dict-lookup per row, O(n))
        grouped: dict[str, _InstrumentData] = {}
        for row in rows:
            key = row.instrument_key
            if key not in grouped:
                grouped[key] = _InstrumentData(
                    instrument_key=key,
                    trading_symbol=row.trading_symbol,
                    name=row.name,
                )
            grouped[key].candles.append(
                _Candle(
                    ts=row.timestamp,
                    close=float(row.close),
                    volume=float(row.volume) if row.volume else 0.0,
                )
            )

        # Drop instruments that are:
        #   a) too sparse to compute prev_close (< 2 candles)
        #   b) too stale to be meaningful (latest candle older than threshold)
        stale_cutoff = ref_ts - timedelta(days=_STALENESS_EXCLUDE_DAYS)
        filtered: dict[str, _InstrumentData] = {}
        for k, v in grouped.items():
            if len(v.candles) >= 2 and v.candles[0].ts >= stale_cutoff:
                filtered[k] = v

        logger.debug(
            "DB baselines: %d total, %d after staleness filter  ref=%s",
            len(grouped), len(filtered), ref_ts.strftime("%Y-%m-%d"),
        )
        return filtered

    # ── Upstox: live LTP + official previous close + volume ───────────────

    async def _fetch_live_quotes(
        self,
        upstox_client: UpstoxClient,
        instrument_keys: list[str],
        instrument_data: dict[str, "_InstrumentData"],
    ) -> dict[str, _LiveQuote]:
        """
        Fetch LTP, official previous close (cp), and today's volume from
        Upstox /v3/market-quote/ltp, batched at 500 instruments per request.

        Response fields used:
          last_price — live LTP (intraday) or today's close (post-market)
          cp         — official NSE previous close (yesterday's VWAP close)
          volume     — total volume traded today

        Key resolution:
          Upstox v3 returns symbol-based keys (NSE_EQ:EMAMIREAL) regardless of
          whether the request used ISIN-based keys (NSE_EQ|INE778K01012).
          We build a symbol→ISIN reverse map from instrument_data so response
          keys resolve back to the ISIN keys used in the rest of the pipeline.
        """
        # Build symbol-key → ISIN-key reverse lookup.
        # e.g. "NSE_EQ|EMAMIREAL" → "NSE_EQ|INE778K01012"
        symbol_to_isin: dict[str, str] = {}
        for isin_key, data in instrument_data.items():
            if data.trading_symbol:
                prefix = isin_key.rsplit("|", 1)[0]
                symbol_to_isin[f"{prefix}|{data.trading_symbol}"] = isin_key

        batches = [
            instrument_keys[i : i + _UPSTOX_BATCH_SIZE]
            for i in range(0, len(instrument_keys), _UPSTOX_BATCH_SIZE)
        ]

        async def _fetch_batch(batch: list[str]) -> dict[str, _LiveQuote]:
            try:
                response = await upstox_client.get(
                    "market-quote/ltp",
                    params={"instrument_key": ",".join(batch)},
                )
                data: dict[str, Any] = response.get("data", {})
                quotes: dict[str, _LiveQuote] = {}
                for key, quote in data.items():
                    ltp = quote.get("last_price")
                    cp = quote.get("cp")
                    volume = quote.get("volume")
                    if ltp is not None and cp is not None and float(ltp) > 0 and float(cp) > 0:
                        # Upstox v3 returns symbol keys; resolve to ISIN key
                        symbol_key = key.replace(":", "|")
                        resolved_key = symbol_to_isin.get(symbol_key, symbol_key)
                        quotes[resolved_key] = _LiveQuote(
                            ltp=float(ltp),
                            cp=float(cp),
                            volume=float(volume) if volume is not None else 0.0,
                        )
                return quotes
            except Exception as exc:
                logger.warning(
                    "Live quote batch failed (%d instruments): %s", len(batch), exc
                )
                return {}

        batch_results = await asyncio.gather(*(_fetch_batch(b) for b in batches))
        merged: dict[str, _LiveQuote] = {}
        for batch_dict in batch_results:
            merged.update(batch_dict)
        return merged

    # ── Result assembly ────────────────────────────────────────────────────

    def _build_results(
        self,
        instrument_data: dict[str, _InstrumentData],
        live_quotes: dict[str, _LiveQuote],
        is_market_open: bool,
        now_utc: datetime,
    ) -> list[ScanResult]:
        results: list[ScanResult] = []
        for key, data in instrument_data.items():
            result = self._score_instrument(
                data, live_quotes.get(key), is_market_open, now_utc
            )
            if result is not None:
                results.append(result)
        return results

    def _score_instrument(
        self,
        data: _InstrumentData,
        live: _LiveQuote | None,
        is_market_open: bool,
        now_utc: datetime,
    ) -> ScanResult | None:
        """
        Compute price change, volume ratio, and RSI for a single instrument.

        Primary path (Upstox available):
          ltp        = Upstox last_price  (live intraday or today's post-market close)
          prev_close = Upstox cp          (official NSE previous-day VWAP close)
          volume     = Upstox volume      (today's accumulated volume)

        Fallback path (no Upstox token or circuit breaker open):
          ltp        = DB candles[0].close  (most recent stored close)
          prev_close = DB candles[1].close  (session before that)
          volume     = DB candles[0].volume

        DB candles are always used for RSI computation and volume average baseline.
        """
        candles = data.candles  # guaranteed len ≥ 2 by _fetch_db_baselines

        # ── Price & volume ──────────────────────────────────────────────────
        if live is not None and live.ltp > 0 and live.cp > 0:
            # Upstox path: always accurate regardless of DB freshness
            ltp = live.ltp
            prev_close = live.cp           # official NSE previous close
            current_volume = live.volume
            using_live = True
        else:
            # DB fallback: compare most recent stored session vs the one before
            ltp = candles[0].close
            prev_close = candles[1].close
            current_volume = candles[0].volume
            using_live = False

        if prev_close <= 0:
            return None

        price_change = ltp - prev_close
        price_change_pct = (price_change / prev_close) * 100.0

        # ── Staleness flag (DB data quality signal, not a price accuracy issue) ──
        warnings: list[str] = []
        if not using_live:
            age_calendar_days = (now_utc - candles[0].ts).days
            if age_calendar_days > 3:
                warnings.append(f"STALE_DATA:{age_calendar_days}d")

        # ── Volume average (20-session DB baseline) ─────────────────────────
        # When using live Upstox data:   avg over candles[0:20] (ref session + 19 prior)
        # When using DB fallback:        avg over candles[1:21] (excludes "current" session)
        vol_window = candles[0:20] if using_live else candles[1:21]
        avg_volume: float | None = None
        if len(vol_window) >= _MIN_CANDLES_VOL_AVG:
            avg_volume = sum(c.volume for c in vol_window) / len(vol_window)

        volume_ratio = (
            (current_volume / avg_volume)
            if avg_volume and avg_volume > 0
            else 0.0
        )

        # ── RSI (14) ────────────────────────────────────────────────────────
        # Reverse candles to oldest→newest for indicator computation
        close_series = [c.close for c in reversed(candles)]
        rsi_value: float | None = None
        if len(close_series) >= _MIN_CANDLES_RSI:
            rsi_series = self._indicators.rsi(close_series)
            rsi_value = next(
                (v for v in reversed(rsi_series) if v is not None), None
            )

        # ── Signals (retained for ScanResponse / GET /run compat) ──────────
        signals: list[ScanSignal] = []
        buy_score = 0
        sell_score = 0

        if rsi_value is not None:
            if rsi_value < 30:
                signals.append(ScanSignal(name="RSI_OVERSOLD", value=rsi_value, direction="buy"))
                buy_score += 2
            elif rsi_value > 70:
                signals.append(ScanSignal(name="RSI_OVERBOUGHT", value=rsi_value, direction="sell"))
                sell_score += 2

        if volume_ratio >= _VOLUME_SPIKE_THRESHOLD:
            signals.append(
                ScanSignal(name="VOLUME_SURGE", value=round(volume_ratio, 2), direction="neutral")
            )

        overall = (
            "buy" if price_change_pct > 0
            else "sell" if price_change_pct < 0
            else "neutral"
        )

        return ScanResult(
            instrument_key=data.instrument_key,
            trading_symbol=data.trading_symbol,
            name=data.name,
            signal=overall,
            score=round(price_change_pct, 4),   # % change is the primary sort key
            signals=signals,
            last_price=round(ltp, 4),
            previous_close=round(prev_close, 4),
            price_change=round(price_change, 4),
            price_change_pct=round(price_change_pct, 4),
            volume=round(current_volume, 2),
            avg_volume=round(avg_volume, 2) if avg_volume is not None else None,
            volume_ratio=round(volume_ratio, 4),
            rsi=round(rsi_value, 2) if rsi_value is not None else None,
            scanned_at=now_utc,
            warnings=warnings,
        )
