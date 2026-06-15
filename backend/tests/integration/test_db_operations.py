"""
Cortex AI — Integration Tests
===============================
Tests DB-layer operations against SQLite in-memory test DB.
Verifies bulk upsert idempotency, scanner query correctness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import delete, select

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
    """Exercises the reconciling sync. Each test starts from a clean table
    (committed into the fixture's outer transaction, rolled back at teardown) and
    passes ``min_instruments=1`` to bypass the production sanity-guard floor for
    these intentionally tiny fixtures."""

    @staticmethod
    async def _clean_slate(db_session: AsyncSession) -> None:
        await db_session.execute(delete(InstrumentMaster))
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_syncs_instruments(self, db_session: AsyncSession) -> None:
        await self._clean_slate(db_session)
        instruments = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        result = await sync_instrument_master(db_session, instruments, min_instruments=1)
        assert result.inserted == 1
        assert result.total_seen == 1
        assert result.active_after == 1

    @pytest.mark.asyncio
    async def test_updates_on_conflict(self, db_session: AsyncSession) -> None:
        await self._clean_slate(db_session)
        instruments = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries Ltd",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        await sync_instrument_master(db_session, instruments, min_instruments=1)

        updated = [
            {
                "instrument_key": "NSE_EQ|INE001",
                "trading_symbol": "RELIANCE",
                "name": "Reliance Industries Updated",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
        result = await sync_instrument_master(db_session, updated, min_instruments=1)
        assert result.inserted == 0
        assert result.updated == 1
        assert result.delisted == 0

        row = (
            await db_session.execute(
                select(InstrumentMaster).where(
                    InstrumentMaster.instrument_key == "NSE_EQ|INE001"
                )
            )
        ).scalar_one()
        assert row.name == "Reliance Industries Updated"


# ══════════════════════════════════════════════════════════════════════════════
# Scanner — single query, grouping
# ══════════════════════════════════════════════════════════════════════════════
class _StubUpstox:
    """Minimal UpstoxClient stand-in. ``has_token=False`` makes the scanner skip
    the live-quote stage, so scans run purely off the seeded OHLCV baselines."""

    has_token = False


@pytest.fixture
def mock_cache():
    """CacheService stand-in: every get is a miss; set is a no-op."""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    return cache


def _weekly_candles(instrument_key: str, timestamps: list[datetime]) -> list[dict]:
    """Candles for the isolated '1week' timeframe (no real rows exist for it),
    so a scan over '1w' sees only the seeded instruments — fully deterministic."""
    return [
        {
            "instrument_key": instrument_key,
            "timeframe": "1week",
            "timestamp": ts,
            "open": 100.0 + i,
            "high": 103.0 + i,
            "low": 99.0 + i,
            "close": 102.0 + i,
            "volume": 50_000 + i * 1000,
            "oi": 0,
        }
        for i, ts in enumerate(timestamps)
    ]


def _recent_weekly_timestamps(weeks: int = 4) -> list[datetime]:
    """A shared set of recent weekly timestamps (within the 30-day reference
    window and 14-day staleness window) — newest first."""
    anchor = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return [anchor - timedelta(days=7 * i) for i in range(weeks)]


class TestScannerService:
    @pytest.mark.asyncio
    async def test_scanner_returns_results(
        self, db_session: AsyncSession, mock_cache
    ) -> None:
        """Scanner processes seeded instruments and returns well-formed results."""
        timestamps = _recent_weekly_timestamps()
        keys = ["NSE_EQ|A001", "NSE_EQ|A002", "NSE_EQ|A003"]
        for key in keys:
            await bulk_upsert_ohlcv(db_session, _weekly_candles(key, timestamps))

        scanner = MarketScannerService(cache=mock_cache)
        results, live_prices_available = await scanner.scan_all(
            db_session, _StubUpstox(), timeframe="1w", force_refresh=True
        )

        assert isinstance(results, list)
        assert {r.instrument_key for r in results} == set(keys)
        assert live_prices_available is False  # stub has no token
        for r in results:
            assert r.signal in ("buy", "sell", "neutral")
            assert isinstance(r.score, (int, float))

    @pytest.mark.asyncio
    async def test_scanner_excludes_delisted_instruments(
        self, db_session: AsyncSession, mock_cache
    ) -> None:
        """A delisted (is_active=false) instrument must not appear in scan results,
        even while it still has recent candles (closes the staleness-window gap)."""
        timestamps = _recent_weekly_timestamps()
        live = ["NSE_EQ|B001", "NSE_EQ|B002"]
        delisted = "NSE_EQ|B003"
        for key in [*live, delisted]:
            await bulk_upsert_ohlcv(db_session, _weekly_candles(key, timestamps))

        # Master rows: two active, one delisted.
        for key, active in [(live[0], True), (live[1], True), (delisted, False)]:
            db_session.add(
                InstrumentMaster(
                    instrument_key=key,
                    trading_symbol=key.split("|")[1],
                    name=key,
                    exchange="NSE",
                    instrument_type="EQ",
                    is_active=active,
                    last_seen_at=datetime.now(timezone.utc),
                    delisted_at=None if active else datetime.now(timezone.utc),
                )
            )
        await db_session.commit()

        scanner = MarketScannerService(cache=mock_cache)
        results, _ = await scanner.scan_all(
            db_session, _StubUpstox(), timeframe="1w", force_refresh=True
        )

        result_keys = {r.instrument_key for r in results}
        assert delisted not in result_keys
        assert result_keys == set(live)

    @pytest.mark.asyncio
    async def test_scanner_returns_empty_for_no_data(
        self, db_session: AsyncSession, mock_cache
    ) -> None:
        # '1m' maps to itself and has no recent reference session in the
        # isolated test transaction -> empty result, no exception.
        scanner = MarketScannerService(cache=mock_cache)
        results, _ = await scanner.scan_all(
            db_session, _StubUpstox(), timeframe="1month", force_refresh=True
        )
        assert results == []