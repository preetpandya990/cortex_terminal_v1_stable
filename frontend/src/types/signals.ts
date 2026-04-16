/**
 * Types for trading signals
 */

export enum SignalType {
  BUY = "buy",
  SELL = "sell",
  HOLD = "hold",
}

export enum TimeHorizon {
  INTRADAY = "intraday",
  SWING = "swing",
  POSITIONAL = "positional",
}

export enum RegimeType {
  BULL_TRENDING = "bull_trending",
  BEAR_TRENDING = "bear_trending",
  SIDEWAYS_RANGE = "sideways_range",
  HIGH_VOLATILITY = "high_volatility",
  LOW_LIQUIDITY = "low_liquidity",
  NEWS_DRIVEN = "news_driven",
}

export interface ContributingEvent {
  event_id: string;
  event_type: string;
  impact_score: number;
  summary: string;
}

export interface ContributingMLPrediction {
  model_id: string;
  model_name: string;
  prediction: string;
  confidence: number;
}

export interface ContributingTechnical {
  indicator: string;
  value: number;
  signal: string;
}

export interface ContributingFactors {
  events: ContributingEvent[];
  ml_predictions: ContributingMLPrediction[];
  technical: ContributingTechnical[];
}

export interface TradingSignal {
  signal_id: string;
  symbol: string;
  signal_type: SignalType;
  confidence: number;
  calibrated_confidence: number;
  target_price?: number;
  stop_loss?: number;
  time_horizon: TimeHorizon;
  reasoning: string;
  contributing_factors: ContributingFactors;
  regime_type: RegimeType;
  generated_at: string;
  expires_at: string;
}

export interface TradingSignalsResponse {
  signals: TradingSignal[];
  total: number;
  page: number;
  limit: number;
}

export interface SignalAuditEntry {
  audit_id: number;
  signal_id: string;
  symbol: string;
  signal_type: SignalType;
  confidence: number;
  generated_at: string;
  outcome?: string;
}

export interface SignalAuditResponse {
  audit_log: SignalAuditEntry[];
  total: number;
}

export interface SignalFilters {
  symbol?: string;
  signal_type?: SignalType;
  min_confidence?: number;
  time_horizon?: TimeHorizon;
  page?: number;
  limit?: number;
}
