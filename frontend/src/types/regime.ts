/**
 * Types for market regime data
 */

export enum RegimeType {
  BULL_TRENDING = "bull_trending",
  BEAR_TRENDING = "bear_trending",
  SIDEWAYS_RANGE = "sideways_range",
  HIGH_VOLATILITY = "high_volatility",
  LOW_LIQUIDITY = "low_liquidity",
  NEWS_DRIVEN = "news_driven",
}

export interface RegimeIndicators {
  adx: number;
  atr: number;
  bollinger_width: number;
  rsi: number;
}

export interface CurrentRegime {
  symbol: string;
  regime_type: RegimeType;
  confidence: number;
  indicators: RegimeIndicators;
  detected_at: string;
  previous_regime: RegimeType | null;
  regime_duration_minutes: number;
}

export interface RegimeHistory {
  symbol: string;
  regime_type: RegimeType;
  confidence: number;
  detected_at: string;
  duration_minutes: number;
}

export interface RegimeHistoryResponse {
  history: RegimeHistory[];
  total: number;
}

export interface ActiveStrategy {
  strategy_id: number;
  strategy_name: string;
  symbol: string;
  regime_type: RegimeType;
  activated_at: string;
  is_active: boolean;
}

export interface ActiveStrategiesResponse {
  strategies: ActiveStrategy[];
  total: number;
}
