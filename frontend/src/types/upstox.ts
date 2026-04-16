export interface UpstoxInstrument {
  instrument_key: string;
  trading_symbol: string;
  name?: string;
  exchange?: string;
  segment?: string;
  isin?: string;
  instrument_type?: string;
}

export interface UpstoxInstrumentSearchResponse {
  query: string;
  count: number;
  results: UpstoxInstrument[];
}

export interface UpstoxOhlc {
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ts: number;
}

export interface UpstoxOhlcQuote {
  instrument_key: string;
  interval: string;
  last_price: number;
  instrument_token?: string;
  prev_ohlc?: UpstoxOhlc;
  live_ohlc?: UpstoxOhlc;
}

export interface UpstoxOhlcQuoteResponse {
  status: string;
  data: UpstoxOhlcQuote;
}

export interface UpstoxLtpTick {
  instrument_key: string;
  last_price: number;
  instrument_token?: string;
  last_trade_time?: string | number;
  last_trade_quantity?: number;
  volume_delta?: number;
  cumulative_volume?: number;
  volume?: number;
  server_timestamp?: string;
}

export interface UpstoxLtpQuoteResponse {
  status: string;
  data: UpstoxLtpTick;
}

/**
 * Raw candle data from Upstox API (tuple format)
 * [timestamp, open, high, low, close, volume, open_interest?]
 */
export type UpstoxCandle = [
  timestamp: string | number,
  open: number,
  high: number,
  low: number,
  close: number,
  volume: number,
  oi?: number
];

/**
 * Normalized candle data with named properties
 * Used for type-safe manipulation and transformation
 */
export interface UpstoxCandleData {
  timestamp: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  oi?: number;
}

export interface UpstoxCandlesPayload {
  candles: UpstoxCandle[];
}

export interface UpstoxCandlesResponse {
  status: string;
  data: UpstoxCandlesPayload;
}

export type UpstoxTickStreamMessage =
  | { type: "connected"; data: { instrument_key: string; interval_ms: number } }
  | { type: "heartbeat" }
  | { type: "tick"; data: UpstoxLtpTick }
  | { type: "error"; data: { message: string; status_code?: number; details?: string; server_timestamp?: string } };
