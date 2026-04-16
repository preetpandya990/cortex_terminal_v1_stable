"""
Cortex AI — Technical Indicators
==================================
All indicators are pure functions (no side effects, no I/O).
Each function is independently unit-testable with known-value assertions.

RSI:  Wilder (1978) smoothing — simple average for seed, then EMA.
MACD: Standard 12/26/9 with EMA.
BB:   Bollinger Bands — 20-period SMA ± 2σ.
ATR:  Average True Range — Wilder smoothing over 14 periods.
VWAP: Rolling session VWAP over a configurable window (NOT cumulative).
"""
from __future__ import annotations

import math
from typing import NamedTuple


class MACDResult(NamedTuple):
    macd_line: list[float]
    signal_line: list[float]
    histogram: list[float]


class BollingerResult(NamedTuple):
    upper: list[float]
    middle: list[float]
    lower: list[float]


class TechnicalIndicators:
    """
    Stateless indicator computation class.
    All methods accept plain Python lists and return lists of the same length
    as the input, with leading values padded with None where insufficient data
    exists for the calculation window.
    """

    # ── RSI ────────────────────────────────────────────────────────────────────
    @staticmethod
    def rsi(closes: list[float], period: int = 14) -> list[float | None]:
        """
        Relative Strength Index using Wilder's smoothing.

        Algorithm:
          1. Compute price deltas.
          2. Seed avg_gain / avg_loss with simple average of first `period` deltas.
          3. Apply Wilder EMA: avg = (prev_avg * (period - 1) + current) / period.
          4. RSI = 100 - 100 / (1 + avg_gain / avg_loss).

        Returns a list the same length as `closes`; first `period` values are None.
        """
        if len(closes) < period + 1:
            return [None] * len(closes)

        deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(d, 0.0) for d in deltas]
        losses = [abs(min(d, 0.0)) for d in deltas]

        # Seed with simple average over the first window
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        result: list[float | None] = [None] * (period + 1)

        def _rsi(ag: float, al: float) -> float:
            if al == 0.0:
                return 100.0
            rs = ag / al
            return 100.0 - (100.0 / (1.0 + rs))

        result.append(_rsi(avg_gain, avg_loss))

        for i in range(period, len(deltas)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            result.append(_rsi(avg_gain, avg_loss))

        return result

    # ── EMA (internal) ─────────────────────────────────────────────────────────
    @staticmethod
    def _ema(values: list[float], period: int) -> list[float | None]:
        """Exponential Moving Average. Returns None for the first period-1 values."""
        if len(values) < period:
            return [None] * len(values)

        k = 2.0 / (period + 1)
        result: list[float | None] = [None] * (period - 1)

        seed = sum(values[:period]) / period
        result.append(seed)

        prev = seed
        for v in values[period:]:
            ema = v * k + prev * (1 - k)
            result.append(ema)
            prev = ema

        return result

    # ── MACD ───────────────────────────────────────────────────────────────────
    def macd(
        self,
        closes: list[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> MACDResult | None:
        """
        MACD = EMA(fast) - EMA(slow).
        Signal = EMA(MACD, signal_period).
        Histogram = MACD - Signal.

        Returns None if insufficient data; otherwise MACDResult with lists
        aligned to the input length (leading Nones where undefined).
        """
        if len(closes) < slow + signal:
            return None

        fast_ema = self._ema(closes, fast)
        slow_ema = self._ema(closes, slow)

        # MACD line: defined only where both EMAs are defined
        macd_line: list[float | None] = []
        for f, s in zip(fast_ema, slow_ema):
            if f is None or s is None:
                macd_line.append(None)
            else:
                macd_line.append(f - s)

        # Extract defined values to compute signal EMA
        defined_macd = [v for v in macd_line if v is not None]
        if len(defined_macd) < signal:
            return None

        raw_signal = self._ema(defined_macd, signal)

        # Pad signal to align with macd_line
        none_prefix = sum(1 for v in macd_line if v is None)
        signal_line: list[float | None] = [None] * none_prefix + raw_signal  # type: ignore[operator]

        # Histogram: defined only where both MACD and signal are defined
        histogram: list[float | None] = []
        for m, s in zip(macd_line, signal_line):
            if m is None or s is None:
                histogram.append(None)
            else:
                histogram.append(m - s)

        return MACDResult(
            macd_line=[v for v in macd_line],     # type: ignore[misc]
            signal_line=[v for v in signal_line],  # type: ignore[misc]
            histogram=[v for v in histogram],      # type: ignore[misc]
        )

    # ── Bollinger Bands ────────────────────────────────────────────────────────
    @staticmethod
    def bollinger_bands(
        closes: list[float], period: int = 20, num_std: float = 2.0
    ) -> BollingerResult | None:
        """
        Bollinger Bands: middle = SMA(period), upper/lower = SMA ± num_std × σ.
        Returns None if insufficient data.
        """
        if len(closes) < period:
            return None

        upper: list[float | None] = [None] * (period - 1)
        middle: list[float | None] = [None] * (period - 1)
        lower: list[float | None] = [None] * (period - 1)

        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1 : i + 1]
            sma = sum(window) / period
            variance = sum((x - sma) ** 2 for x in window) / period
            std = math.sqrt(variance)
            upper.append(sma + num_std * std)
            middle.append(sma)
            lower.append(sma - num_std * std)

        return BollingerResult(upper=upper, middle=middle, lower=lower)  # type: ignore[arg-type]

    # ── ATR ────────────────────────────────────────────────────────────────────
    @staticmethod
    def atr(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        period: int = 14,
    ) -> list[float | None]:
        """
        Average True Range using Wilder smoothing.
        True Range = max(high - low, |high - prev_close|, |low - prev_close|)
        """
        if len(closes) < period + 1:
            return [None] * len(closes)

        trs: list[float] = []
        for i in range(1, len(closes)):
            hl = highs[i] - lows[i]
            hc = abs(highs[i] - closes[i - 1])
            lc = abs(lows[i] - closes[i - 1])
            trs.append(max(hl, hc, lc))

        result: list[float | None] = [None] * (period)  # first `period` closes → None
        atr_val = sum(trs[:period]) / period
        result.append(atr_val)

        for tr in trs[period:]:
            atr_val = (atr_val * (period - 1) + tr) / period
            result.append(atr_val)

        return result

    # ── VWAP (rolling) ─────────────────────────────────────────────────────────
    @staticmethod
    def vwap(
        highs: list[float],
        lows: list[float],
        closes: list[float],
        volumes: list[float],
        window: int = 20,
    ) -> list[float | None]:
        """
        Rolling VWAP over `window` bars.

        NOTE: This is a rolling VWAP, NOT a cumulative session VWAP.
        Cumulative VWAP is only correct intraday from market open.
        Use rolling VWAP for multi-day / daily timeframe analysis.

        VWAP = Σ(typical_price × volume) / Σ(volume) over window bars.
        Typical price = (high + low + close) / 3
        """
        n = len(closes)
        if n < window:
            return [None] * n

        typical = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(n)]
        pv = [typical[i] * volumes[i] for i in range(n)]

        result: list[float | None] = [None] * (window - 1)
        for i in range(window - 1, n):
            total_pv = sum(pv[i - window + 1 : i + 1])
            total_vol = sum(volumes[i - window + 1 : i + 1])
            result.append(total_pv / total_vol if total_vol > 0 else None)

        return result