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
import type { CandlestickData, HistogramData, UTCTimestamp } from "lightweight-charts";

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

// Semi-transparent green/red — matches candle colours without overwhelming the price chart.
const VOLUME_UP_COLOR   = "rgba(16, 185, 129, 0.45)";  // #10b981 @ 45%
const VOLUME_DOWN_COLOR = "rgba(239, 68, 68,  0.45)";  // #ef4444 @ 45%

/**
 * Transforms raw Upstox candles into volume histogram data for lightweight-charts.
 * Bars are coloured green when close >= open (up candle) and red otherwise.
 */
export function transformVolumeForChart(candles: UpstoxCandle[]): HistogramData[] {
  if (!candles || candles.length === 0) return [];

  return candles
    .map((candle): HistogramData | null => {
      const time = normalizeTimestamp(candle[0]) as UTCTimestamp;
      if (time === 0) return null;
      const open  = candle[1];
      const close = candle[4];
      return {
        time,
        value: candle[5],
        color: close >= open ? VOLUME_UP_COLOR : VOLUME_DOWN_COLOR,
      };
    })
    .filter((d): d is HistogramData => d !== null)
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
 * Market hours constants for NSE (National Stock Exchange of India)
 */
const MARKET_OPEN_HOUR_IST = 9;
const MARKET_OPEN_MINUTE_IST = 15;
const MARKET_CLOSE_HOUR_IST = 15;
const MARKET_CLOSE_MINUTE_IST = 30;

/**
 * Check if market is currently open
 * NSE trading hours: 9:15 AM - 3:30 PM IST, Monday-Friday
 */
function isMarketOpen(): boolean {
  const now = new Date();
  const formatter = new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    hour: "numeric",
    minute: "numeric",
    weekday: "short",
    hour12: false,
  });
  const parts = formatter.formatToParts(now);
  const hour = parseInt(parts.find((p) => p.type === "hour")?.value ?? "0", 10);
  const minute = parseInt(parts.find((p) => p.type === "minute")?.value ?? "0", 10);
  const weekday = parts.find((p) => p.type === "weekday")?.value ?? "Mon";
  const dayOfWeek = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(weekday);
  
  // Check if weekday (Monday-Friday)
  const isWeekday = dayOfWeek >= 1 && dayOfWeek <= 5;
  if (!isWeekday) return false;
  
  // Check if within trading hours
  const currentMinutes = hour * 60 + minute;
  const openMinutes = MARKET_OPEN_HOUR_IST * 60 + MARKET_OPEN_MINUTE_IST;
  const closeMinutes = MARKET_CLOSE_HOUR_IST * 60 + MARKET_CLOSE_MINUTE_IST;
  
  return currentMinutes >= openMinutes && currentMinutes < closeMinutes;
}

/**
 * Get today's date in YYYY-MM-DD format (IST timezone)
 */
function getISTTodayYMD(): string {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return formatter.format(new Date());
}

/**
 * Get date from candle timestamp
 */
function getCandleDate(timestamp: string | number): string {
  let date: Date;
  if (typeof timestamp === 'string') {
    date = new Date(timestamp);
  } else {
    date = new Date(timestamp * 1000);
  }
  return date.toISOString().split('T')[0];
}

/**
 * Create today's candle from live tick data
 * Used when last candle is not today and market is open
 */
function createTodaysCandle(livePrice: number, openPrice?: number): UpstoxCandle {
  const today = getISTTodayYMD();
  const todayTimestamp = `${today}T00:00:00+05:30`;
  const open = openPrice ?? livePrice;
  
  return [
    todayTimestamp,
    open,
    livePrice, // high
    livePrice, // low
    livePrice, // close
    0, // volume (unknown for live candle)
    0, // oi (unknown for live candle)
  ];
}

/**
 * Updates the last candle with live tick data, or creates today's candle if needed.
 * 
 * Industry-standard behavior for daily charts:
 * - If last candle is today → update its OHLC with live price
 * - If last candle is NOT today AND market is open → create new candle for today
 * - If market is closed → no updates
 * 
 * This matches TradingView, Bloomberg Terminal, and other professional platforms.
 * 
 * @param candles - Current array of candles
 * @param livePrice - Current live price
 * @returns New array with updated or appended candle
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
  const lastCandleDate = getCandleDate(lastCandle[0]);
  const today = getISTTodayYMD();
  
  // Scenario 1: Last candle is today → update it with live price
  if (lastCandleDate === today) {
    updated[updated.length - 1] = [
      lastCandle[0], // timestamp unchanged
      lastCandle[1], // open unchanged
      Math.max(lastCandle[2], livePrice), // high = max(current high, live price)
      Math.min(lastCandle[3], livePrice), // low = min(current low, live price)
      livePrice, // close = live price
      lastCandle[5], // volume unchanged
      lastCandle[6], // oi unchanged
    ];
  } 
  // Scenario 2: Last candle is NOT today AND market is open → create today's candle
  else if (isMarketOpen()) {
    // Use yesterday's close as today's open (standard practice)
    const todaysCandle = createTodaysCandle(livePrice, lastCandle[4]);
    updated.push(todaysCandle);
  }
  // Scenario 3: Market is closed and last candle is not today → no updates

  return updated;
}
