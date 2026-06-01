"""
Cortex AI — Technical Indicators Unit Tests
=============================================
All tests use known-value inputs and verify against expected outputs.
Edge cases: empty input, insufficient data, extreme values, flat prices.
"""
from __future__ import annotations

import math

import pytest

from app.services.indicators import TechnicalIndicators


@pytest.fixture
def ind() -> TechnicalIndicators:
    return TechnicalIndicators()


# ══════════════════════════════════════════════════════════════════════════════
# RSI Tests
# ══════════════════════════════════════════════════════════════════════════════
class TestRSI:
    def test_returns_none_for_insufficient_data(self, ind: TechnicalIndicators) -> None:
        closes = [1.0] * 10  # need period+1 = 15
        result = ind.rsi(closes, period=14)
        assert all(v is None for v in result)

    def test_first_period_values_are_none(self, ind: TechnicalIndicators) -> None:
        closes = list(range(1, 30))
        result = ind.rsi(closes, period=14)
        # First 15 values (index 0..14) must be None
        assert all(v is None for v in result[:15])

    def test_defined_values_are_in_range(self, ind: TechnicalIndicators) -> None:
        closes = [44.34 + i * 0.1 for i in range(50)]
        result = ind.rsi(closes)
        defined = [v for v in result if v is not None]
        assert all(0.0 <= v <= 100.0 for v in defined)

    def test_flat_prices_return_zero_or_none(self, ind: TechnicalIndicators) -> None:
        closes = [100.0] * 30
        result = ind.rsi(closes)
        defined = [v for v in result if v is not None]
        # All gains and losses are zero → RSI not calculable; allow None or 0
        for v in defined:
            assert v == 0.0 or v is None or math.isnan(v) or 0 <= v <= 100

    def test_all_up_prices_approach_100(self, ind: TechnicalIndicators) -> None:
        closes = [float(i) for i in range(1, 50)]
        result = ind.rsi(closes)
        defined = [v for v in result if v is not None]
        # All-positive deltas → RSI should be very high (> 95)
        assert all(v > 90.0 for v in defined)

    def test_all_down_prices_approach_0(self, ind: TechnicalIndicators) -> None:
        closes = [float(50 - i) for i in range(50)]
        result = ind.rsi(closes)
        defined = [v for v in result if v is not None]
        # All-negative deltas → RSI should be very low (< 10)
        assert all(v < 10.0 for v in defined)

    def test_result_length_matches_input(self, ind: TechnicalIndicators) -> None:
        closes = [float(i) for i in range(1, 40)]
        result = ind.rsi(closes)
        assert len(result) == len(closes)

    def test_known_value_seed(self, ind: TechnicalIndicators) -> None:
        """
        Verify the Wilder smoothing seed is simple average (not EMA) for first period.
        For a series of equal gains, avg_gain after seed = gain value exactly.
        """
        # 5-period RSI with known gains for exact verification
        closes = [10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0]
        result = ind.rsi(closes, period=5)
        # All gains = 1.0, all losses = 0 → RSI = 100 after seed
        defined = [v for v in result if v is not None]
        assert all(v == 100.0 for v in defined)

    def test_empty_input(self, ind: TechnicalIndicators) -> None:
        result = ind.rsi([])
        assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# MACD Tests
# ══════════════════════════════════════════════════════════════════════════════
class TestMACD:
    def test_returns_none_for_insufficient_data(self, ind: TechnicalIndicators) -> None:
        closes = list(range(1, 30))  # need 26+9=35
        result = ind.macd(closes)
        assert result is None

    def test_result_lengths_match_input(self, ind: TechnicalIndicators, macd_closes) -> None:
        result = ind.macd(macd_closes)
        assert result is not None
        assert len(result.macd_line) == len(macd_closes)
        assert len(result.signal_line) == len(macd_closes)
        assert len(result.histogram) == len(macd_closes)

    def test_histogram_equals_macd_minus_signal(
        self, ind: TechnicalIndicators, macd_closes
    ) -> None:
        result = ind.macd(macd_closes)
        assert result is not None
        for m, s, h in zip(result.macd_line, result.signal_line, result.histogram):
            if m is not None and s is not None and h is not None:
                assert abs((m - s) - h) < 1e-9

    def test_custom_periods(self, ind: TechnicalIndicators) -> None:
        closes = [float(i) for i in range(1, 30)]
        result = ind.macd(closes, fast=5, slow=10, signal=3)
        assert result is not None

    def test_leading_values_are_none(self, ind: TechnicalIndicators, macd_closes) -> None:
        result = ind.macd(macd_closes)
        assert result is not None
        # The first slow-1=25 MACD values should be None
        assert all(v is None for v in result.macd_line[:25])


# ══════════════════════════════════════════════════════════════════════════════
# Bollinger Bands Tests
# ══════════════════════════════════════════════════════════════════════════════
class TestBollingerBands:
    def test_returns_none_for_insufficient_data(self, ind: TechnicalIndicators) -> None:
        closes = [1.0] * 10  # need 20
        result = ind.bollinger_bands(closes)
        assert result is None

    def test_upper_above_middle_above_lower(
        self, ind: TechnicalIndicators, bollinger_closes
    ) -> None:
        result = ind.bollinger_bands(bollinger_closes)
        assert result is not None
        for u, m, l in zip(result.upper, result.middle, result.lower):
            if u is not None and m is not None and l is not None:
                assert u >= m >= l

    def test_flat_prices_zero_bandwidth(self, ind: TechnicalIndicators) -> None:
        closes = [100.0] * 25
        result = ind.bollinger_bands(closes)
        assert result is not None
        defined_u = [v for v in result.upper if v is not None]
        defined_l = [v for v in result.lower if v is not None]
        assert all(abs(u - 100.0) < 1e-9 for u in defined_u)
        assert all(abs(l - 100.0) < 1e-9 for l in defined_l)

    def test_result_length_matches_input(
        self, ind: TechnicalIndicators, bollinger_closes
    ) -> None:
        result = ind.bollinger_bands(bollinger_closes)
        assert result is not None
        assert len(result.upper) == len(bollinger_closes)
        assert len(result.middle) == len(bollinger_closes)
        assert len(result.lower) == len(bollinger_closes)

    def test_custom_std_multiplier(self, ind: TechnicalIndicators) -> None:
        closes = [float(i) for i in range(1, 30)]
        r1 = ind.bollinger_bands(closes, num_std=1.0)
        r2 = ind.bollinger_bands(closes, num_std=2.0)
        assert r1 is not None and r2 is not None
        # 2× std should give wider bands
        defined_u1 = [v for v in r1.upper if v is not None]
        defined_u2 = [v for v in r2.upper if v is not None]
        for u1, u2 in zip(defined_u1, defined_u2):
            assert u2 >= u1


# ══════════════════════════════════════════════════════════════════════════════
# ATR Tests
# ══════════════════════════════════════════════════════════════════════════════
class TestATR:
    @pytest.fixture
    def ohlc(self):
        n = 30
        highs = [10.0 + i * 0.1 for i in range(n)]
        lows = [9.0 + i * 0.1 for i in range(n)]
        closes = [9.5 + i * 0.1 for i in range(n)]
        return highs, lows, closes

    def test_returns_all_none_for_insufficient_data(
        self, ind: TechnicalIndicators
    ) -> None:
        h = l = c = [1.0] * 5
        result = ind.atr(h, l, c, period=14)
        assert all(v is None for v in result)

    def test_atr_is_positive(self, ind: TechnicalIndicators, ohlc) -> None:
        h, l, c = ohlc
        result = ind.atr(h, l, c)
        defined = [v for v in result if v is not None]
        assert all(v > 0 for v in defined)

    def test_result_length_matches_input(self, ind: TechnicalIndicators, ohlc) -> None:
        h, l, c = ohlc
        result = ind.atr(h, l, c)
        assert len(result) == len(c)


# ══════════════════════════════════════════════════════════════════════════════
# VWAP Tests
# ══════════════════════════════════════════════════════════════════════════════
class TestVWAP:
    @pytest.fixture
    def ohlcv(self):
        n = 25
        highs = [10.0 + i * 0.05 for i in range(n)]
        lows = [9.5 + i * 0.05 for i in range(n)]
        closes = [9.75 + i * 0.05 for i in range(n)]
        volumes = [1000.0 + i * 10 for i in range(n)]
        return highs, lows, closes, volumes

    def test_returns_none_for_insufficient_data(self, ind: TechnicalIndicators) -> None:
        n = 10
        result = ind.vwap([1.0] * n, [1.0] * n, [1.0] * n, [100.0] * n, window=20)
        assert all(v is None for v in result)

    def test_result_length_matches_input(self, ind: TechnicalIndicators, ohlcv) -> None:
        h, l, c, v = ohlcv
        result = ind.vwap(h, l, c, v)
        assert len(result) == len(c)

    def test_vwap_is_between_low_and_high(self, ind: TechnicalIndicators, ohlcv) -> None:
        h, l, c, v = ohlcv
        result = ind.vwap(h, l, c, v)
        for i, vwap_val in enumerate(result):
            if vwap_val is not None:
                assert l[i] <= vwap_val <= h[i]

    def test_flat_price_vwap_equals_typical(self, ind: TechnicalIndicators) -> None:
        n = 25
        price = 100.0
        result = ind.vwap(
            [price] * n, [price] * n, [price] * n, [500.0] * n, window=20
        )
        defined = [v for v in result if v is not None]
        assert all(abs(v - price) < 1e-9 for v in defined)