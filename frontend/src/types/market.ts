/**
 * Cortex Terminal V1 - Market Data Types
 *
 * Type definitions for market data, technical indicators, and scanner results.
 * These types mirror the backend API response structures.
 */

/**
 * OHLCV (Open, High, Low, Close, Volume) data point
 * Returned by: GET /api/v1/market/stocks/{symbol}/data
 */
export interface OHLCVData {
  timestamp: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/**
 * Response from the stock data endpoint
 */
export interface StockDataResponse {
  symbol: string;
  timeframe: string;
  count: number;
  data: OHLCVData[];
}

/**
 * Technical indicators for a stock
 * Returned by: GET /api/v1/market/stocks/{symbol}/indicators
 */
export interface StockIndicators {
  timestamp: string;
  ema_5: number | null;
  ema_9: number | null;
  ema_21: number | null;
  ema_50: number | null;
  ema_200: number | null;
  rsi_14: number | null;
  macd: number | null;
  macd_signal: number | null;
  bb_upper: number | null;
  bb_middle: number | null;
  bb_lower: number | null;
}

/**
 * Response from the stock indicators endpoint
 */
export interface StockIndicatorsResponse {
  symbol: string;
  timeframe: string;
  count: number;
  data: StockIndicators[];
}

/**
 * Individual stock analysis from market scanner
 * Contains price, volume, and indicator data for a single stock
 */
export interface StockAnalysis {
  symbol: string;
  trading_symbol: string | null;
  name: string | null;
  sector: string | null;
  current_price: number;
  previous_close: number;
  price_change: number;
  price_change_pct: number;
  volume: number;
  avg_volume: number;
  volume_ratio: number;
  rsi: number | null;
  timestamp: string;
  warnings: string[];
}

/**
 * Supported market scan modes.
 */
export type ScanType = 'market_open' | 'market_close' | 'intraday';
export type ScanRunStatus = 'success' | 'partial' | 'failed';

/**
 * Nested results structure within scan results
 */
export interface ScanResultsData {
  top_gainers: StockAnalysis[];
  top_losers: StockAnalysis[];
  volume_spikes: StockAnalysis[];
  total_scanned: number;
  errors: Array<Record<string, unknown>>;
  metadata: Record<string, unknown>;
}

/**
 * Market scan results
 * Returned by: GET /api/v1/scanner/latest
 */
export interface LatestScanResponse {
  scan_id: number;
  timestamp: string;
  scan_type: ScanType;
  timeframe: string;
  total_scanned: number;
  processing_time_ms: number;
  stale_instrument_count: number;
  live_prices_available: boolean;
  results: ScanResultsData;
}

/**
 * Response from: POST /api/v1/scanner/run
 */
export interface RunScanResponse {
  status: ScanRunStatus;
  message: string;
  scan_type: ScanType;
  total_scanned: number;
  error_count: number;
  primary_error_code: string | null;
  stale_instrument_count: number;
  live_prices_available: boolean;
  results: ScanResultsData;
}

export interface ScannerContext {
  now_utc: string;
  is_trading_day: boolean;
  is_market_open: boolean;
  market_open_utc: string | null;
  market_close_utc: string | null;
  default_scan_type: ScanType;
  selectable_market_close_dates: string[];
  closed_reason: string | null;
}

/**
 * Response from the stock refresh endpoint
 */
export interface RefreshResponse {
  message: string;
  ohlcv_records: number;
  indicator_records: number;
}
