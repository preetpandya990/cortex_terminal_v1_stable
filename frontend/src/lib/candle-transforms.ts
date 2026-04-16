/**
 * Cortex AI — Candle Data Transformations
 * =========================================
 * Production-ready utilities for transforming Upstox candle data
 * between different formats (tuple → object → chart format).
 *
 * Design Principles:
 * - Type-safe transformations with zero `any` types
 * - Performance-optimized batch operations
 * - Immutable data handling
 * - Comprehensive timestamp normalization
 */

import type { UpstoxCandle, UpstoxCandleData } from "@/types/upstox";
import type { CandlestickData, UTCTimestamp } from "lightweight-charts";

/**
 * Normalizes timestamp to Unix timestamp in seconds
 * Handles multiple formats:
 * - ISO 8601 strings
 * - Unix timestamps in milliseconds
 * - Unix timestamps in seconds
 */
export function normalizeTimestamp(value: string | number): number {
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (!Number.isFinite(parsed)) {
      console.warn(`Invalid timestamp string: ${value}`);
      return 0;
    }
    return Math.floor(parsed / 1000);
  }

  if (!Number.isFinite(value)) {
    console.warn(`Invalid timestamp number: ${value}`);
    return 0;
  }

  // If timestamp is in milliseconds (> 10 billion), convert to seconds
  return value > 10_000_000_000 ? Math.floor(value / 1000) : value;
}

/**
 * Transforms Upstox tuple format to normalized object format
 * 
 * @param candle - Raw candle data from Upstox API
 * @returns Normalized candle with named properties
 * 
 * @example
 * const raw = [1712345678000, 100, 105, 99, 103, 50000, 1000];
 * const normalized = normalizeUpstoxCandle(raw);
 * // { timestamp: 1712345678000, open: 100, high: 105, ... }
 */
export function normalizeUpstoxCandle(candle: UpstoxCandle): UpstoxCandleData {
  return {
    timestamp: candle[0],
    open: candle[1],
    high: candle[2],
    low: candle[3],
    close: candle[4],
    volume: candle[5],
    oi: candle[6],
  };
}

/**
 * Transforms normalized candle to lightweight-charts format
 * 
 * @param candle - Normalized candle data
 * @returns CandlestickData compatible with lightweight-charts
 * 
 * @example
 * const normalized = { timestamp: 1712345678000, open: 100, ... };
 * const chartData = toLightweightChartData(normalized);
 * // { time: 1712345678, open: 100, high: 105, low: 99, close: 103 }
 */
export function toLightweightChartData(candle: UpstoxCandleData): CandlestickData {
  return {
    time: normalizeTimestamp(candle.timestamp) as UTCTimestamp,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
  };
}

/**
 * Batch transformation for performance
 * Transforms array of Upstox candles to chart-ready format
 * 
 * @param candles - Array of raw Upstox candles
 * @returns Sorted array of CandlestickData for lightweight-charts
 * 
 * @example
 * const rawCandles = [[...], [...], [...]];
 * const chartData = transformCandlesForChart(rawCandles);
 */
export function transformCandlesForChart(candles: UpstoxCandle[]): CandlestickData[] {
  if (!candles || candles.length === 0) {
    return [];
  }

  return candles
    .map(normalizeUpstoxCandle)
    .map(toLightweightChartData)
    .sort((a, b) => (a.time as number) - (b.time as number));
}

/**
 * Merges two arrays of candles, removing duplicates by timestamp
 * Used for combining historical and intraday data
 * 
 * @param candles1 - First array of candles
 * @param candles2 - Second array of candles
 * @returns Merged and deduplicated array, sorted by timestamp
 */
export function mergeCandles(
  candles1: UpstoxCandle[],
  candles2: UpstoxCandle[]
): UpstoxCandle[] {
  const byTimestamp = new Map<number, UpstoxCandle>();

  // Add all candles from first array
  for (const candle of candles1) {
    const ts = normalizeTimestamp(candle[0]);
    byTimestamp.set(ts, candle);
  }

  // Add/overwrite with candles from second array (newer data takes precedence)
  for (const candle of candles2) {
    const ts = normalizeTimestamp(candle[0]);
    byTimestamp.set(ts, candle);
  }

  // Convert back to array and sort
  return Array.from(byTimestamp.values()).sort(
    (a, b) => normalizeTimestamp(a[0]) - normalizeTimestamp(b[0])
  );
}

/**
 * Updates the last candle with live tick data
 * Used for real-time price updates without full data refresh
 * 
 * @param candles - Current array of candles
 * @param livePrice - Current live price
 * @returns New array with updated last candle
 */
export function updateLastCandleWithLivePrice(
  candles: UpstoxCandle[],
  livePrice: number
): UpstoxCandle[] {
  if (!candles || candles.length === 0) {
    return candles;
  }

  const updated = [...candles];
  const lastCandle = updated[updated.length - 1];

  // Update last candle: [timestamp, open, high, low, close, volume, oi]
  updated[updated.length - 1] = [
    lastCandle[0], // timestamp unchanged
    lastCandle[1], // open unchanged
    Math.max(lastCandle[2], livePrice), // high = max(current high, live price)
    Math.min(lastCandle[3], livePrice), // low = min(current low, live price)
    livePrice, // close = live price
    lastCandle[5], // volume unchanged
    lastCandle[6], // oi unchanged
  ];

  return updated;
}
