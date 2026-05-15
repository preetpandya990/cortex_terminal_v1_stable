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
  source_url?: string;
  source_name?: string;
  article_title?: string;
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
  /** Official company name from instrument_master (e.g. "HDFC Bank Limited"). Null for legacy rows. */
  company_name: string | null;
  /** True when the symbol exists in instrument_master as an NSE EQ equity (tradeable).
   *  False for companies referenced in news/RSS that are not listed on the platform. */
  is_nse_eligible: boolean;
  /** Upstox instrument key (e.g. "NSE_EQ|INE001A01036"). Present for NSE-eligible signals only. */
  instrument_key: string | null;
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
  /** When true, returns only NSE-eligible signals. When false, returns Non-NSE only.
   *  Omit to return all signals (admin / bulk views). */
  is_nse_eligible?: boolean;
  page?: number;
  limit?: number;
}
