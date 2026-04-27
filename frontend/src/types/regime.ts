/**
 * Market Regime — shared TypeScript types
 *
 * Regime is computed server-side from 60 daily OHLCV candles per instrument.
 * All values are derived from real DB data — no mock or random generation.
 */

export type RegimeType =
  | "bull_trending"
  | "bear_trending"
  | "sideways_range"
  | "high_volatility"
  | "low_liquidity"
  | "insufficient_data";

export type TrendDirection = "up" | "down" | "flat";
export type RegimeStrength = "strong" | "moderate" | "weak";

export interface RegimeIndicators {
  adx: number;
  rsi: number;
  atr: number;
  atr_pct: number;
  bollinger_width: number;
}

/** Single instrument regime — used in constituent lists and the detail pane. */
export interface InstrumentRegime {
  instrument_key: string;
  trading_symbol: string;
  company_name: string;
  current_price: number;
  change_pct: number;
  change_pct_20d: number;
  trend: TrendDirection;
  regime: RegimeType;
  confidence: number;
  strength: RegimeStrength;
  data_points: number;
  indicators: RegimeIndicators;
  computed_at: string;
}

/** Aggregated regime for one Nifty index (derived from its constituents). */
export interface IndexRegime {
  index_key: string;
  name: string;
  short: string;
  regime: RegimeType;
  confidence: number;
  strength: RegimeStrength;
  change_pct: number;
  trend: TrendDirection;
  bullish_count: number;
  bearish_count: number;
  sideways_count: number;
  volatile_count: number;
  total_constituents: number;
  computed_at: string;
}

/** Market breadth summary — % of Nifty 50 in each regime. */
export interface MarketBreadth {
  bullish_pct: number;
  bearish_pct: number;
  sideways_pct: number;
  volatile_pct: number;
}

/** Top-level market overview response from GET /strategy/market/overview. */
export interface MarketOverview {
  nifty50: IndexRegime;
  breadth: MarketBreadth;
  all_indices: IndexRegime[];
  computed_at: string;
}

/** Index constituents response from GET /strategy/index/{key}/constituents. */
export interface IndexConstituentsResponse {
  index_key: string;
  name: string;
  constituents: InstrumentRegime[];
  total: number;
  computed_at: string;
}
