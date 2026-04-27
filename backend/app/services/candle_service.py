"""
Cortex AI — Candle Data Service
=================================
Manages OHLCV candle data with intelligent DB-first fetching strategy.

Architecture:
  1. Query database for requested candles
  2. If data exists and is complete → return from DB
  3. If data missing or incomplete → fetch from Upstox API
  4. Store fetched data in DB for future requests
  5. Return combined/merged data to client

Benefits:
  - Reduces API calls to Upstox (rate limit protection)
  - Faster response times (DB query < API call)
  - Data persistence for historical analysis
  - Automatic caching layer
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.upstox_data import UpstoxOHLCV

logger = logging.getLogger(__name__)


class CandleService:
    """
    Service for managing OHLCV candle data with DB-first strategy.
    
    This service implements a three-tier data fetching strategy:
    1. Check database for existing candles
    2. Fallback to Upstox API if data missing
    3. Store fetched data for future requests
    """

    @staticmethod
    def _timeframe_to_db_format(unit: str, interval: int) -> str:
        """
        Convert API timeframe format to database timeframe format.
        
        Examples:
          - unit='minutes', interval=1 → '1m'
          - unit='minutes', interval=5 → '5m'
          - unit='hours', interval=1 → '1h'
          - unit='days', interval=1 → '1D'
        """
        unit_map = {
            "minutes": "m",
            "hours": "h",
            "days": "D",
            "weeks": "W",
            "months": "M",
        }
        suffix = unit_map.get(unit, "D")
        return f"{interval}{suffix}"

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """Parse YYYY-MM-DD string to datetime."""
        return datetime.strptime(date_str, "%Y-%m-%d")

    @staticmethod
    def _format_candle_for_api(row: Any) -> list:
        """
        Format database row to Upstox API candle format.
        
        Upstox format: [timestamp, open, high, low, close, volume, oi]
        """
        return [
            row.timestamp.isoformat(),
            float(row.open),
            float(row.high),
            float(row.low),
            float(row.close),
            int(row.volume),
            int(row.oi),
        ]

    async def get_historical_candles(
        self,
        db: AsyncSession,
        instrument_key: str,
        unit: str,
        interval: int,
        from_date: str,
        to_date: str,
    ) -> tuple[list[list], bool, list[dict[str, str]]]:
        """
        Get historical candles with DB-first strategy and gap detection.
        
        Returns:
            tuple: (candles_list, is_from_db, missing_ranges)
                - candles_list: List of candles in Upstox format
                - is_from_db: True if data came from DB, False if needs full API fetch
                - missing_ranges: List of date gaps that need API fetch
        
        Logic:
          1. Query DB for candles in date range
          2. Detect gaps in the data
          3. If no gaps → return complete DB data
          4. If gaps exist → return partial DB data + gap ranges for frontend to fill
          5. If no DB data → return empty and signal full API fetch
        """
        timeframe = self._timeframe_to_db_format(unit, interval)
        from_dt = self._parse_date(from_date)
        to_dt = self._parse_date(to_date) + timedelta(days=1)  # Include end date

        try:
            # Query database for existing candles
            result = await db.execute(
                text("""
                    SELECT timestamp, open, high, low, close, volume, oi
                    FROM upstox_ohlcv
                    WHERE instrument_key = :key
                      AND timeframe = :tf
                      AND timestamp >= :from_dt
                      AND timestamp < :to_dt
                    ORDER BY timestamp ASC
                """),
                {
                    "key": instrument_key,
                    "tf": timeframe,
                    "from_dt": from_dt,
                    "to_dt": to_dt,
                },
            )
            rows = result.fetchall()

            if not rows:
                logger.info(
                    f"[CandleService] No DB data for {instrument_key} {timeframe} "
                    f"{from_date} to {to_date}, will fetch from API"
                )
                return [], False, []

            # Convert rows to Upstox API format
            candles = []
            for row in rows:
                candles.append([
                    row[0].isoformat(),  # timestamp
                    float(row[1]),       # open
                    float(row[2]),       # high
                    float(row[3]),       # low
                    float(row[4]),       # close
                    int(row[5]),         # volume
                    int(row[6]),         # oi
                ])

            # Detect gaps in the data
            gaps = self._detect_gaps(candles, from_date, to_date, unit)

            if gaps:
                logger.info(
                    f"[CandleService] Found {len(candles)} candles in DB with {len(gaps)} "
                    f"gap(s) for {instrument_key} {timeframe}: {gaps}"
                )
                return candles, True, gaps
            else:
                logger.info(
                    f"[CandleService] Found {len(candles)} complete candles in DB for "
                    f"{instrument_key} {timeframe} {from_date} to {to_date}"
                )
                return candles, True, []

        except Exception as e:
            logger.exception(
                f"[CandleService] Error querying DB for {instrument_key}: {e}"
            )
            return [], False, []

    async def get_intraday_candles(
        self,
        db: AsyncSession,
        instrument_key: str,
        unit: str,
        interval: int,
    ) -> tuple[list[list], bool]:
        """
        Get intraday candles for today with DB-first strategy.
        
        Returns:
            tuple: (candles_list, is_from_db)
        
        Note: Intraday data is typically cached for short periods (30s)
              so we're more aggressive about fetching from API for freshness.
        """
        timeframe = self._timeframe_to_db_format(unit, interval)
        today = datetime.now().date()
        today_start = datetime.combine(today, datetime.min.time())
        today_end = datetime.combine(today, datetime.max.time())

        try:
            # Query database for today's candles
            result = await db.execute(
                text("""
                    SELECT timestamp, open, high, low, close, volume, oi
                    FROM upstox_ohlcv
                    WHERE instrument_key = :key
                      AND timeframe = :tf
                      AND timestamp >= :from_dt
                      AND timestamp <= :to_dt
                    ORDER BY timestamp ASC
                """),
                {
                    "key": instrument_key,
                    "tf": timeframe,
                    "from_dt": today_start,
                    "to_dt": today_end,
                },
            )
            rows = result.fetchall()

            if not rows:
                logger.info(
                    f"[CandleService] No intraday DB data for {instrument_key} {timeframe}, "
                    "will fetch from API"
                )
                return [], False

            # Convert rows to Upstox API format
            candles = []
            for row in rows:
                candles.append([
                    row[0].isoformat(),  # timestamp
                    float(row[1]),       # open
                    float(row[2]),       # high
                    float(row[3]),       # low
                    float(row[4]),       # close
                    int(row[5]),         # volume
                    int(row[6]),         # oi
                ])

            # For intraday, check if data is recent (within last 5 minutes)
            if candles:
                last_candle_time = datetime.fromisoformat(candles[-1][0].replace('Z', '+00:00'))
                age_minutes = (datetime.now(last_candle_time.tzinfo) - last_candle_time).total_seconds() / 60
                
                if age_minutes > 5:
                    logger.info(
                        f"[CandleService] Intraday data for {instrument_key} is {age_minutes:.1f} "
                        "minutes old, will fetch fresh data from API"
                    )
                    return [], False

            logger.info(
                f"[CandleService] Found {len(candles)} recent intraday candles in DB for "
                f"{instrument_key} {timeframe}"
            )
            return candles, True

        except Exception as e:
            logger.exception(
                f"[CandleService] Error querying intraday DB for {instrument_key}: {e}"
            )
            return [], False

    async def store_candles(
        self,
        db: AsyncSession,
        instrument_key: str,
        unit: str,
        interval: int,
        candles: list[list],
    ) -> int:
        """
        Store fetched candles in database for future requests.
        
        Args:
            db: Database session
            instrument_key: Upstox instrument key
            unit: Candle unit (minutes, hours, days, etc.)
            interval: Candle interval
            candles: List of candles in Upstox format
        
        Returns:
            int: Number of candles stored
        
        Note: Uses INSERT ... ON CONFLICT DO NOTHING to avoid duplicates
        """
        if not candles:
            return 0

        timeframe = self._timeframe_to_db_format(unit, interval)
        stored_count = 0

        try:
            # Prepare bulk insert with conflict handling
            for candle in candles:
                timestamp_str = candle[0]
                # Handle both ISO format and timestamp strings
                if 'T' in timestamp_str:
                    timestamp = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                else:
                    timestamp = datetime.fromtimestamp(float(timestamp_str))

                await db.execute(
                    text("""
                        INSERT INTO upstox_ohlcv 
                        (instrument_key, timeframe, timestamp, open, high, low, close, volume, oi)
                        VALUES (:key, :tf, :ts, :open, :high, :low, :close, :vol, :oi)
                        ON CONFLICT (instrument_key, timeframe, timestamp) 
                        DO NOTHING
                    """),
                    {
                        "key": instrument_key,
                        "tf": timeframe,
                        "ts": timestamp,
                        "open": candle[1],
                        "high": candle[2],
                        "low": candle[3],
                        "close": candle[4],
                        "vol": candle[5],
                        "oi": candle[6],
                    },
                )
                stored_count += 1

            await db.commit()
            logger.info(
                f"[CandleService] Stored {stored_count} candles for {instrument_key} "
                f"{timeframe} in database"
            )
            return stored_count

        except Exception as e:
            await db.rollback()
            logger.exception(
                f"[CandleService] Error storing candles for {instrument_key}: {e}"
            )
            return 0

    @staticmethod
    def _estimate_expected_candles(
        unit: str, interval: int, from_date: str, to_date: str
    ) -> int:
        """
        Estimate expected number of candles for a date range.
        
        This is used to determine if DB data is complete enough to use.
        """
        from_dt = datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = datetime.strptime(to_date, "%Y-%m-%d")
        days = (to_dt - from_dt).days + 1

        if unit == "minutes":
            # Market hours: 9:15 AM - 3:30 PM IST = 375 minutes
            # Approximate trading days: ~70% of calendar days
            trading_days = days * 0.7
            candles_per_day = 375 / interval
            return int(trading_days * candles_per_day)
        elif unit == "hours":
            trading_days = days * 0.7
            candles_per_day = 6.25 / interval  # 6.25 hours per trading day
            return int(trading_days * candles_per_day)
        elif unit == "days":
            return int(days * 0.7)  # ~70% are trading days
        elif unit == "weeks":
            return int(days / 7)
        elif unit == "months":
            return int(days / 30)
        else:
            return int(days * 0.7)  # Default to daily estimate

    @staticmethod
    def _detect_gaps(
        candles: list[list],
        from_date: str,
        to_date: str,
        unit: str,
    ) -> list[dict[str, str]]:
        """
        Detect missing date ranges in candle data.
        
        For daily candles, detects gaps of 2+ weekdays.
        For intraday, detects missing days.
        
        Returns:
            List of gap ranges: [{"from_date": "YYYY-MM-DD", "to_date": "YYYY-MM-DD"}, ...]
        """
        if not candles or unit != "days":
            # Only detect gaps for daily data
            return []

        # Extract dates from candles
        candle_dates = set()
        for candle in candles:
            timestamp_str = candle[0]
            if 'T' in timestamp_str:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            else:
                dt = datetime.fromtimestamp(float(timestamp_str))
            candle_dates.add(dt.date())

        # Generate expected date range (excluding weekends)
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt = datetime.strptime(to_date, "%Y-%m-%d").date()
        
        gaps = []
        current_gap_start = None
        current_date = from_dt

        while current_date <= to_dt:
            # Skip weekends
            if current_date.weekday() < 5:  # Monday=0, Friday=4
                if current_date not in candle_dates:
                    if current_gap_start is None:
                        current_gap_start = current_date
                else:
                    if current_gap_start is not None:
                        # Gap ended, record it
                        gaps.append({
                            "from_date": current_gap_start.strftime("%Y-%m-%d"),
                            "to_date": (current_date - timedelta(days=1)).strftime("%Y-%m-%d")
                        })
                        current_gap_start = None
            
            current_date += timedelta(days=1)

        # Handle gap extending to end of range
        if current_gap_start is not None:
            gaps.append({
                "from_date": current_gap_start.strftime("%Y-%m-%d"),
                "to_date": to_dt.strftime("%Y-%m-%d")
            })

        return gaps

    @staticmethod
    def merge_candles(
        candles1: list[list],
        candles2: list[list]
    ) -> list[list]:
        """
        Merge two candle lists, deduplicating by timestamp.
        Newer data (candles2) takes precedence over older data (candles1).
        
        Args:
            candles1: First list of candles (typically from DB)
            candles2: Second list of candles (typically from API)
        
        Returns:
            Merged and sorted list of candles
        """
        # Use dict to deduplicate by timestamp (Unix epoch seconds)
        by_timestamp = {}
        
        # Helper to normalize timestamp to Unix epoch
        def to_unix_timestamp(ts_str: str) -> int:
            if 'T' in ts_str:
                dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
            else:
                # Already a Unix timestamp
                return int(float(ts_str))
            return int(dt.timestamp())
        
        # Add candles from first list
        for candle in candles1:
            unix_ts = to_unix_timestamp(candle[0])
            by_timestamp[unix_ts] = candle
        
        # Add/overwrite with candles from second list (newer data takes precedence)
        for candle in candles2:
            unix_ts = to_unix_timestamp(candle[0])
            by_timestamp[unix_ts] = candle
        
        # Convert back to list and sort by Unix timestamp
        merged = sorted(by_timestamp.values(), key=lambda c: to_unix_timestamp(c[0]))
        
        return merged



# Singleton instance
candle_service = CandleService()
