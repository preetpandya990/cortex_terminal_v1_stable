/**
 * Technical indicator definitions and calculations.
 *
 * All computation functions:
 * - Accept raw UpstoxCandle tuples (no pre-processing needed from callers)
 * - Return time-aligned series ready to pass to lightweight-charts setData()
 * - Filter invalid/incomplete leading entries (period warm-up values)
 * - Are pure functions — safe to memoize with React.useMemo
 * - Zero external dependencies — all math is native TypeScript
 *
 * Periods used (all industry-standard, fixed per product decision):
 *   SMA 20 / 50  |  EMA 20 / 50
 *   Bollinger Bands 20, 2σ
 *   VWAP (daily reset, IST timezone)
 *   RSI 14  (Wilder's smoothing)
 *   MACD 12 / 26 / 9  (all EMA)
 *   Stochastic 14 / 3 / 3  (slow %K + %D)
 */

import { normalizeTimestamp } from './candle-transforms';
import type { UpstoxCandle } from '@/types/upstox';

// ─── Indicator catalogue ──────────────────────────────────────────────────────

export type IndicatorId =
  | 'SMA_20' | 'SMA_50'
  | 'EMA_20' | 'EMA_50'
  | 'BB'
  | 'VWAP'
  | 'RSI'
  | 'MACD'
  | 'STOCH';

export type IndicatorType = 'overlay' | 'oscillator';

export interface IndicatorMeta {
  id: IndicatorId;
  label: string;
  type: IndicatorType;
  /** Primary colour shown in the selector and on the chart. */
  color: string;
  description: string;
}

export const INDICATOR_META: Record<IndicatorId, IndicatorMeta> = {
  SMA_20: { id: 'SMA_20', label: 'SMA 20',      type: 'overlay',    color: '#f59e0b', description: 'Simple Moving Average (20)' },
  SMA_50: { id: 'SMA_50', label: 'SMA 50',      type: 'overlay',    color: '#a855f7', description: 'Simple Moving Average (50)' },
  EMA_20: { id: 'EMA_20', label: 'EMA 20',      type: 'overlay',    color: '#06b6d4', description: 'Exponential Moving Average (20)' },
  EMA_50: { id: 'EMA_50', label: 'EMA 50',      type: 'overlay',    color: '#ec4899', description: 'Exponential Moving Average (50)' },
  BB:     { id: 'BB',     label: 'BB (20, 2σ)', type: 'overlay',    color: '#60a5fa', description: 'Bollinger Bands — period 20, 2 standard deviations' },
  VWAP:   { id: 'VWAP',  label: 'VWAP',         type: 'overlay',    color: '#fbbf24', description: 'Volume Weighted Average Price (daily reset, IST)' },
  RSI:    { id: 'RSI',   label: 'RSI (14)',      type: 'oscillator', color: '#22d3ee', description: 'Relative Strength Index — period 14' },
  MACD:   { id: 'MACD',  label: 'MACD',          type: 'oscillator', color: '#60a5fa', description: 'MACD (12, 26, 9) — fast 12, slow 26, signal 9' },
  STOCH:  { id: 'STOCH', label: 'Stoch',         type: 'oscillator', color: '#22d3ee', description: 'Stochastic Oscillator (14, 3, 3)' },
};

/** Ordered list of overlay indicators (top to bottom in selector). */
export const OVERLAY_INDICATORS: IndicatorId[]    = ['SMA_20', 'SMA_50', 'EMA_20', 'EMA_50', 'BB', 'VWAP'];
/** Ordered list of oscillators (determines vertical stacking order, top → bottom). */
export const OSCILLATOR_INDICATORS: IndicatorId[] = ['RSI', 'MACD', 'STOCH'];

// ─── Result types (time field is Unix seconds, matching lightweight-charts UTCTimestamp) ──

export interface LinePoint  { time: number; value: number }
export interface BBPoint    { time: number; upper: number; middle: number; lower: number }
export interface MACDPoint  { time: number; macd: number; signal: number; histogram: number }
export interface StochPoint { time: number; k: number; d: number }

export interface IndicatorData {
  SMA_20?: LinePoint[];
  SMA_50?: LinePoint[];
  EMA_20?: LinePoint[];
  EMA_50?: LinePoint[];
  BB?:     BBPoint[];
  VWAP?:   LinePoint[];
  RSI?:    LinePoint[];
  MACD?:   MACDPoint[];
  STOCH?:  StochPoint[];
}

// ─── Pane layout ──────────────────────────────────────────────────────────────

/** Vertical fraction each oscillator pane occupies (series height + inter-pane gap). */
const OSCILLATOR_ZONE = 0.20;  // 18 % series + 2 % visual gap above
const VOLUME_RATIO    = 0.22;  // Volume occupies bottom 22 % of the price pane

export interface PaneLayout {
  price:  { top: number; bottom: number };
  volume: { top: number; bottom: number };
  /** Oscillator margins in the same order as the input array (first = topmost). */
  oscillators: { id: IndicatorId; top: number; bottom: number }[];
}

/**
 * Computes non-overlapping scaleMargins for every price scale on the chart.
 * Oscillators are stacked bottom-up in the order supplied (so pass them in
 * top-to-bottom order, e.g. [RSI, MACD, STOCH]).
 */
export function computePaneLayout(activeOscillators: IndicatorId[]): PaneLayout {
  const oscCount      = activeOscillators.length;
  const pricePaneBtm  = oscCount * OSCILLATOR_ZONE;
  const priceZoneH    = 1 - pricePaneBtm;
  const volHeight     = priceZoneH * VOLUME_RATIO;
  const volTop        = priceZoneH - volHeight;

  // Oscillators stack from bottom of chart upward; activeOscillators[0] is topmost.
  const oscillators = activeOscillators.map((id, i) => {
    const reversedIdx = oscCount - 1 - i;
    const bottom = reversedIdx * OSCILLATOR_ZONE;
    const top    = 1 - bottom - (OSCILLATOR_ZONE - 0.02);  // 18 % visible, 2 % gap
    return { id, top, bottom: bottom + 0.02 };
  });

  return {
    price:  { top: 0, bottom: pricePaneBtm },
    volume: { top: volTop, bottom: pricePaneBtm },
    oscillators,
  };
}

// ─── Native math primitives ───────────────────────────────────────────────────
//
// All functions operate on plain number[] and produce plain number[] (or typed
// objects). They are the source-of-truth for every indicator calculation; no
// external library is used.

/**
 * Simple Moving Average — O(n) sliding-window.
 * Output length: N - period + 1
 */
function sma(values: number[], period: number): number[] {
  if (values.length < period) return [];
  const out: number[] = [];
  let windowSum = 0;
  for (let i = 0; i < period; i++) windowSum += values[i];
  out.push(windowSum / period);
  for (let i = period; i < values.length; i++) {
    windowSum += values[i] - values[i - period];
    out.push(windowSum / period);
  }
  return out;
}

/**
 * Exponential Moving Average — seeded with SMA of first `period` values.
 * Multiplier: 2 / (period + 1).
 * Output length: N - period + 1
 */
function ema(values: number[], period: number): number[] {
  if (values.length < period) return [];
  const k = 2 / (period + 1);

  // Seed: SMA of first window
  let prev = 0;
  for (let i = 0; i < period; i++) prev += values[i];
  prev /= period;

  const out: number[] = [prev];
  for (let i = period; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out.push(prev);
  }
  return out;
}

/**
 * Bollinger Bands — O(n) sliding window using the computational formula
 * var = E[X²] − (E[X])² to avoid two-pass summation.
 * Uses population standard deviation (same as most charting platforms).
 * Output length: N - period + 1
 */
function bollingerBands(
  values: number[],
  period: number,
  mult: number,
): { upper: number; middle: number; lower: number }[] {
  if (values.length < period) return [];
  const out: { upper: number; middle: number; lower: number }[] = [];

  let sum = 0, sumSq = 0;
  for (let i = 0; i < period - 1; i++) {
    sum   += values[i];
    sumSq += values[i] * values[i];
  }

  for (let i = period - 1; i < values.length; i++) {
    sum   += values[i];
    sumSq += values[i] * values[i];

    const mean     = sum / period;
    // Clamp to 0 to absorb floating-point rounding below the floor
    const variance = Math.max(0, sumSq / period - mean * mean);
    const std      = Math.sqrt(variance);

    out.push({ upper: mean + mult * std, middle: mean, lower: mean - mult * std });

    sum   -= values[i - period + 1];
    sumSq -= values[i - period + 1] * values[i - period + 1];
  }
  return out;
}

/**
 * RSI — Wilder's smoothing method (alpha = 1/period).
 * Output length: N - period
 */
function rsi(values: number[], period: number): number[] {
  if (values.length < period + 1) return [];
  const out: number[] = [];

  // Seed: simple average of first `period` changes
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const delta = values[i] - values[i - 1];
    if (delta > 0) avgGain += delta;
    else           avgLoss -= delta;
  }
  avgGain /= period;
  avgLoss /= period;
  out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));

  // Wilder's smoothing for remaining values
  for (let i = period + 1; i < values.length; i++) {
    const delta = values[i] - values[i - 1];
    const gain  = delta > 0 ? delta : 0;
    const loss  = delta < 0 ? -delta : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    out.push(avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss));
  }
  return out;
}

/**
 * MACD — all EMA (no simple-MA variants).
 * Returns only fully-defined values (no undefined warm-up entries).
 * Output length: N - (slow + signal - 2)
 */
function macd(
  values: number[],
  fast: number,
  slow: number,
  signal: number,
): { macd: number; signal: number; histogram: number }[] {
  const fastEMA = ema(values, fast);
  const slowEMA = ema(values, slow);
  // fastEMA is longer; tail-align to slowEMA
  const fastOffset = fastEMA.length - slowEMA.length;
  const macdLine   = slowEMA.map((s, i) => fastEMA[i + fastOffset] - s);
  const signalLine = ema(macdLine, signal);
  const sigOffset  = macdLine.length - signalLine.length;
  return signalLine.map((sig, i) => ({
    macd:      macdLine[i + sigOffset],
    signal:    sig,
    histogram: macdLine[i + sigOffset] - sig,
  }));
}

/**
 * Stochastic Oscillator — slow variant (14, 3, 3).
 * rawK   = (Close − LowestLow₁₄) / (HighestHigh₁₄ − LowestLow₁₄) × 100
 * slow%K = SMA₃(rawK)
 * %D     = SMA₃(slow%K)
 * Output length: N - period - 2*(signalPeriod - 1) = N - period - 2*signalPeriod + 2
 */
function stochastic(
  highs: number[],
  lows: number[],
  closes: number[],
  period: number,
  signalPeriod: number,
): { k: number; d: number }[] {
  const rawK: number[] = [];
  for (let i = period - 1; i < closes.length; i++) {
    let hh = -Infinity, ll = Infinity;
    for (let j = i - period + 1; j <= i; j++) {
      if (highs[j]  > hh) hh = highs[j];
      if (lows[j]   < ll) ll = lows[j];
    }
    rawK.push(hh === ll ? 50 : ((closes[i] - ll) / (hh - ll)) * 100);
  }

  const slowK   = sma(rawK, signalPeriod);
  const dLine   = sma(slowK, signalPeriod);
  const dOffset = slowK.length - dLine.length;

  return dLine.map((d, i) => ({ k: slowK[i + dOffset], d }));
}

// ─── Internal chart helpers ───────────────────────────────────────────────────

/** IST trading-day string (YYYY-MM-DD) for a Unix-seconds timestamp. */
function istDayKey(timestampSeconds: number): string {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Kolkata',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(timestampSeconds * 1000));
}

/** Tail-align a shorter indicator output array to the candle timestamp array. */
function alignToEnd(candles: UpstoxCandle[], values: number[]): LinePoint[] {
  if (!values.length) return [];
  const offset = candles.length - values.length;
  return values.map((value, i) => ({
    time:  normalizeTimestamp(candles[i + offset][0]),
    value,
  })).filter(d => d.time > 0);
}

// ─── Computation functions ────────────────────────────────────────────────────

function computeSMA(candles: UpstoxCandle[], period: number): LinePoint[] {
  if (candles.length < period) return [];
  return alignToEnd(candles, sma(candles.map(c => c[4]), period));
}

function computeEMA(candles: UpstoxCandle[], period: number): LinePoint[] {
  if (candles.length < period) return [];
  return alignToEnd(candles, ema(candles.map(c => c[4]), period));
}

function computeBB(candles: UpstoxCandle[]): BBPoint[] {
  const PERIOD = 20, MULT = 2;
  if (candles.length < PERIOD) return [];

  const results = bollingerBands(candles.map(c => c[4]), PERIOD, MULT);
  const offset  = candles.length - results.length;

  return results.map((r, i) => ({
    time:   normalizeTimestamp(candles[i + offset][0]),
    upper:  r.upper,
    middle: r.middle,
    lower:  r.lower,
  })).filter(d => d.time > 0);
}

function computeVWAP(candles: UpstoxCandle[]): LinePoint[] {
  const result: LinePoint[] = [];
  let cumulativePV     = 0;
  let cumulativeVolume = 0;
  let currentDay       = '';

  for (const candle of candles) {
    const time = normalizeTimestamp(candle[0]);
    if (time === 0) continue;

    const day = istDayKey(time);
    if (day !== currentDay) {
      cumulativePV     = 0;
      cumulativeVolume = 0;
      currentDay       = day;
    }

    const typicalPrice = (candle[2] + candle[3] + candle[4]) / 3;  // (H + L + C) / 3
    const volume       = candle[5];

    cumulativePV     += typicalPrice * volume;
    cumulativeVolume += volume;

    if (cumulativeVolume > 0) {
      result.push({ time, value: cumulativePV / cumulativeVolume });
    }
  }
  return result;
}

function computeRSI(candles: UpstoxCandle[]): LinePoint[] {
  const PERIOD = 14;
  if (candles.length < PERIOD + 1) return [];
  return alignToEnd(candles, rsi(candles.map(c => c[4]), PERIOD));
}

function computeMACD(candles: UpstoxCandle[]): MACDPoint[] {
  const FAST = 12, SLOW = 26, SIGNAL = 9;
  if (candles.length < SLOW + SIGNAL) return [];

  const results = macd(candles.map(c => c[4]), FAST, SLOW, SIGNAL);
  const offset  = candles.length - results.length;

  return results.map((r, i) => ({
    time:      normalizeTimestamp(candles[i + offset][0]),
    macd:      r.macd,
    signal:    r.signal,
    histogram: r.histogram,
  })).filter(d => d.time > 0);
}

function computeStochastic(candles: UpstoxCandle[]): StochPoint[] {
  const PERIOD = 14, SIGNAL_K = 3, SIGNAL_D = 3;
  if (candles.length < PERIOD + SIGNAL_K) return [];

  const results = stochastic(
    candles.map(c => c[2]),
    candles.map(c => c[3]),
    candles.map(c => c[4]),
    PERIOD,
    SIGNAL_K,
  );
  const offset = candles.length - results.length;

  return results.map((r, i) => ({
    time: normalizeTimestamp(candles[i + offset][0]),
    k:    r.k,
    d:    r.d,
  })).filter(d => d.time > 0);
}

// ─── Public API ───────────────────────────────────────────────────────────────

/**
 * Computes all requested indicators in a single pass.
 * Safe to call from React.useMemo — pure, no side effects.
 */
export function computeIndicators(
  candles: UpstoxCandle[],
  activeIds: IndicatorId[],
): IndicatorData {
  if (!candles.length || !activeIds.length) return {};

  const result: IndicatorData = {};

  for (const id of activeIds) {
    switch (id) {
      case 'SMA_20': result.SMA_20 = computeSMA(candles, 20);   break;
      case 'SMA_50': result.SMA_50 = computeSMA(candles, 50);   break;
      case 'EMA_20': result.EMA_20 = computeEMA(candles, 20);   break;
      case 'EMA_50': result.EMA_50 = computeEMA(candles, 50);   break;
      case 'BB':     result.BB     = computeBB(candles);         break;
      case 'VWAP':   result.VWAP   = computeVWAP(candles);      break;
      case 'RSI':    result.RSI    = computeRSI(candles);        break;
      case 'MACD':   result.MACD   = computeMACD(candles);       break;
      case 'STOCH':  result.STOCH  = computeStochastic(candles); break;
    }
  }

  return result;
}
