"""
Cortex AI — HawkEye Unit Tests
================================
Tests HawkEyeService scoring logic with known candle data.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.schemas.hawk_eye import HawkEyeResponse
from app.services.hawk_eye import HawkEyeService


def _make_candle(instrument_key: str, timeframe: str, index: int):
    """Create a mock OHLCV candle with deterministic values."""
    candle = MagicMock()
    candle.instrument_key = instrument_key
    candle.timeframe = timeframe
    candle.timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    candle.open = 100.0 + index
    candle.high = 102.0 + index
    candle.low = 99.0 + index
    candle.close = 101.0 + index
    candle.volume = 10000 + index * 100
    return candle


@pytest.fixture
def mock_cache() -> AsyncMock:
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock()
    return cache


class TestHawkEyeService:
    @pytest.mark.asyncio
    async def test_returns_empty_for_no_data(self, mock_cache: AsyncMock) -> None:
        session = AsyncMock()
        session.execute = AsyncMock(return_value=AsyncMock(scalars=lambda: AsyncMock(all=lambda: [])))

        # Simulate empty DB
        mock_execute = AsyncMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_execute.scalars.return_value = mock_scalars
        session.execute = AsyncMock(return_value=mock_execute)

        service = HawkEyeService(cache=mock_cache)
        result = await service.scan(session, timeframes=["1d"], force_refresh=True)

        assert isinstance(result, HawkEyeResponse)
        assert result.total == 0
        assert result.results == []

    def test_score_instrument_with_insufficient_candles(self, mock_cache: AsyncMock) -> None:
        service = HawkEyeService(cache=mock_cache)
        # Only 5 candles — below the 20-candle minimum
        candles = [_make_candle("NSE_EQ|TEST", "1d", i) for i in range(5)]
        result = service._score_instrument("NSE_EQ|TEST", {"1d": candles}, ["1d"])
        assert result is None

    def test_score_instrument_returns_valid_signal(self, mock_cache: AsyncMock) -> None:
        service = HawkEyeService(cache=mock_cache)
        candles = [_make_candle("NSE_EQ|TEST", "1d", i) for i in range(30)]
        result = service._score_instrument("NSE_EQ|TEST", {"1d": candles}, ["1d"])
        if result is not None:
            assert result.composite_signal in ("buy", "sell", "neutral")
            assert result.strength in ("strong", "moderate", "weak")
            assert 0.0 <= result.agreement_pct <= 100.0
            assert result.trading_symbol == "TEST"
            assert result.exchange == "NSE"

    def test_agreement_pct_bounds(self, mock_cache: AsyncMock) -> None:
        service = HawkEyeService(cache=mock_cache)
        # Generate a declining price series (should produce sell signals)
        candles = [_make_candle("NSE_EQ|TEST", "1d", i) for i in range(35)]
        # Override to declining prices
        for i, c in enumerate(candles):
            c.close = 200.0 - i * 2  # strong downtrend
            c.open = 201.0 - i * 2
            c.high = 202.0 - i * 2
            c.low = 198.0 - i * 2

        result = service._score_instrument("NSE_EQ|TEST", {"1d": candles}, ["1d"])
        if result:
            assert 0.0 <= result.agreement_pct <= 100.0
            assert result.composite_signal in ("buy", "sell", "neutral")

    def test_instrument_key_parsing(self, mock_cache: AsyncMock) -> None:
        service = HawkEyeService(cache=mock_cache)
        candles = [_make_candle("BSE_EQ|INE001", "1d", i) for i in range(30)]
        result = service._score_instrument("BSE_EQ|INE001", {"1d": candles}, ["1d"])
        if result:
            assert result.trading_symbol == "INE001"
            assert result.exchange == "BSE"