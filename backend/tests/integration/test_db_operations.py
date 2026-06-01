"""
Cortex AI — Integration Tests
===============================
Tests DB-layer operations against SQLite in-memory test DB.
Verifies bulk upsert idempotency, scanner query correctness.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.upstox_data import UpstoxOHLCV, InstrumentMaster
from app.services.data_ingestion import bulk_upsert_ohlcv, sync_instrument_master
from app.services.market_scanner import MarketScannerService


def _make_candles(instrument_key: str, count: int = 30) -> list[dict]:
    """Generate deterministic candle rows for testing."""
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    from datetime import timedelta

    return [
        {
            "instrument_key": instrument_key,
            "timeframe": "1d",
            "timestamp": base + timedelta(days=i),
            "open": 100.0 + i,
            "high": 102.0 + i,
            "low": 99.0 + i,
            "close": 101.0 + i,
            "volume": 10000 + i * 100,
            "oi": 0,
        }
        for i in range(count)
    ]


# ══════════════════════════════════════════════════════════════════════════════
# Bulk upsert
# ══════════════════════════════════════════════════════════════════════════════
class TestBulkUpsert:
    @pytest.mark.asyncio
    async def test_inserts_rows(self, db_session: AsyncSession) -> None:
        candles = _make_candles("NSE_EQ|TEST001", count=5)
        count = await bulk_upsert_ohlcv(db_session, candles)
        assert count == 5

    @pytest.mark.asyncio
    async def test_idempotent_on_duplicate(self, db_session: AsyncSession) -> None:
        """Inserting the same rows twice must not create duplicates."""
        candles = _make_candles("NSE_EQ|TEST002", count=5)
        await bulk_upsert_ohlcv(db_session, candles)
        await bulk_upsert_ohlcv(db_session, candles)  # duplicate — should be no-op

        result = await db_session.execute(
            select(UpstoxOHLCV).where(UpstoxOHLCV.instrument_key == "NSE_EQ|TEST002")
        )
        rows = result.scalars().all()
        assert len(rows) == 5  # not 10

    @pytest.mark.asyncio
    async def test_batches_large_input(self, db_session: AsyncSession) -> None:
        """Verify large inputs are processed in batches without error."""
        candles = _make_candles("NSE_EQ|TEST003", count=150)
        count = await bulk_upsert_ohlcv(db_session, candles, batch_size=50)
        assert count == 150

    @pytest.mark.asyncio
    async def test_empty_input_returns_zero(self, db_session: AsyncSession) -> None:
        count = await bulk_upsert_ohlcv(db_session, [])
        assert count == 0


# ══════════════════════════════════════════════════════════════════════════════
# Instrument master sync
# ══════════════════════════════════════════════════════════════════════════════
class TestInstrumentSync:
    @pytest.mark.asyncio
    async def test_syncs_instruments(self, db_session: AsyncSession) -> None:
        instruments = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        count = await sync_instrument_master(db_session, instruments)
        assert count == 1

    @pytest.mark.asyncio
    async def test_updates_on_conflict(self, db_session: AsyncSession) -> None:
        instruments = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries Ltd",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        await sync_instrument_master(db_session, instruments)

        updated = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries Updated",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        await sync_instrument_master(db_session, updated)

        result = await db_session.execute(
            select(InstrumentMaster).where(
                InstrumentMaster.instrument_key == "NSE_EQ|INE001"
            )
        )
        row = result.scalar_one()
        assert row.name == "Reliance Industries Updated"


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — single query, grouping
# ══════════════════════════════════════════════════════════════════════════════
class TestScannerService:
    @pytest.mark.asyncio
    async def test_scanner_returns_results(
        self, db_session: AsyncSession, mock_cache
    ) -> None:
        """Scanner should process instruments from the DB in one query."""
        # Seed data for 3 instruments
        for symbol in ["NSE_EQ|A001", "NSE_EQ|A002", "NSE_EQ|A003"]:
            candles = _make_candles(symbol, count=30)
            await bulk_upsert_ohlcv(db_session, candles)

        scanner = MarketScannerService(cache=mock_cache)
        results = await scanner.scan_all(db_session, timeframe="1d", force_refresh=True)

        assert isinstance(results, list)
        # Each result must have required fields
        for r in results:
            assert hasattr(r, "instrument_key")
            assert hasattr(r, "signal")
            assert hasattr(r, "score")
            assert r.signal in ("buy", "sell", "neutral")

    @pytest.mark.asyncio
    async def test_scanner_returns_empty_for_no_data(
        self, db_session: AsyncSession, mock_cache
    ) -> None:
        scanner = MarketScannerService(cache=mock_cache)
        results = await scanner.scan_all(
            db_session, timeframe="1w", force_refresh=True
        )
        assert results == []