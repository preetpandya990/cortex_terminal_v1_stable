"""
Cortex AI — Bulk Ingestion Service
====================================
Replaces per-row INSERT loops with batch PostgreSQL upserts.

Key improvements over the original:
  - insert().values(batch).on_conflict_do_nothing() — single round-trip per batch
  - Configurable batch size (default 1000 rows)
  - Explicit typed errors — never swallows exceptions silently
  - datetime.now(timezone.utc) throughout (replaces deprecated utcnow)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.exceptions import DatabaseError, UpstoxAPIError
from app.models.upstox_data import UpstoxOHLCV, InstrumentMaster
from app.services.upstox_client import UpstoxClient

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Candle ingestion ───────────────────────────────────────────────────────────
async def bulk_upsert_ohlcv(
    session: AsyncSession,
    rows: Sequence[dict],
    *,
    batch_size: int | None = None,
) -> int:
    """
    Upsert a sequence of OHLCV candle dicts into upstox_ohlcv.
    Uses PostgreSQL INSERT ... ON CONFLICT DO NOTHING — idempotent and efficient.
    Returns the number of rows processed.
    """
    if not rows:
        return 0

    size = batch_size or settings.BULK_INSERT_BATCH_SIZE
    total = 0

    for i in range(0, len(rows), size):
        batch = list(rows[i : i + size])
        stmt = (
            pg_insert(UpstoxOHLCV)
            .values(batch)
            .on_conflict_do_nothing(
                index_elements=["instrument_key", "timeframe", "timestamp"]
            )
        )
        try:
            await session.execute(stmt)
            total += len(batch)
            logger.debug("Upserted batch of %d OHLCV rows", len(batch))
        except Exception as exc:
            logger.error("OHLCV bulk upsert failed for batch starting at %d: %s", i, exc)
            raise DatabaseError(f"OHLCV bulk upsert failed: {exc}") from exc

    await session.commit()
    logger.info("Bulk OHLCV upsert complete: %d rows processed", total)
    return total


# ── Instrument master sync ─────────────────────────────────────────────────────
async def sync_instrument_master(
    session: AsyncSession,
    instruments: Sequence[dict],
    *,
    batch_size: int | None = None,
) -> int:
    """
    Sync instrument master data using bulk upsert.
    Conflicts on instrument_key update all mutable fields.
    """
    if not instruments:
        return 0

    size = batch_size or settings.BULK_INSERT_BATCH_SIZE
    total = 0

    for i in range(0, len(instruments), size):
        batch = list(instruments[i : i + size])
        # Timestamp all rows with UTC now
        now = datetime.now(timezone.utc)
        for row in batch:
            row.setdefault("updated_at", now)

        stmt = pg_insert(InstrumentMaster).values(batch).on_conflict_do_update(
            index_elements=["instrument_key"],
            set_={
                "name": pg_insert(InstrumentMaster).excluded.name,
                "symbol": pg_insert(InstrumentMaster).excluded.symbol,
                "exchange_segment": pg_insert(InstrumentMaster).excluded.exchange_segment,
                "isin": pg_insert(InstrumentMaster).excluded.isin,
                "source": pg_insert(InstrumentMaster).excluded.source,
                "updated_at": pg_insert(InstrumentMaster).excluded.updated_at,
            },
        )
        try:
            await session.execute(stmt)
            total += len(batch)
        except Exception as exc:
            logger.error("Instrument sync failed at batch %d: %s", i, exc)
            raise DatabaseError(f"Instrument sync failed: {exc}") from exc

    await session.commit()
    logger.info("Instrument master sync complete: %d records", total)
    return total


# ── Candle fetcher ─────────────────────────────────────────────────────────────
class DataIngestionService:
    """
    High-level service for fetching and storing OHLCV candle data.
    Lifecycle: instantiated once in lifespan, stored on app.state.
    """

    def __init__(self, upstox_client: UpstoxClient) -> None:
        self._client = upstox_client

    async def fetch_and_store_candles(
        self,
        session: AsyncSession,
        instrument_key: str,
        timeframe: str,
        from_date: str,
        to_date: str,
    ) -> int:
        """
        Fetch OHLCV candles from Upstox and bulk-upsert into the database.
        Returns the count of rows upserted.
        """
        try:
            data = await self._client.get(
                f"/historical-candle/{instrument_key}/{timeframe}/{to_date}/{from_date}"
            )
        except UpstoxAPIError:
            raise  # typed — let caller decide handling

        candles = data.get("data", {}).get("candles", [])
        if not candles:
            logger.info("No candles returned for %s [%s]", instrument_key, timeframe)
            return 0

        rows = [
            {
                "instrument_key": instrument_key,
                "timeframe": timeframe,
                "timestamp": candle[0],
                "open": candle[1],
                "high": candle[2],
                "low": candle[3],
                "close": candle[4],
                "volume": candle[5],
                "oi": candle[6] if len(candle) > 6 else 0,
            }
            for candle in candles
        ]

        return await bulk_upsert_ohlcv(session, rows)