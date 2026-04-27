/**
 * Cortex AI — Trade Suggestions Types
 * =====================================
 * Type definitions for multi-agent validated trade suggestions.
 * Mirrors backend Pydantic schemas from app.schemas.trade_suggestions.
 */

// ============================================================================
// Enums (String Literal Unions)
// ============================================================================

/**
 * Trade signal direction
 */
export type SignalDirection = "BUY" | "SELL";

/**
 * Consensus confidence level
 */
export type ConfidenceLevel = "HIGH" | "MEDIUM" | "LOW";

/**
 * Trade suggestion lifecycle status
 */
export type SuggestionStatus = "active" | "expired" | "executed" | "invalidated";

/**
 * Event correlation trigger pathway
 */
export type TriggerPathway = "TECHNICAL_FIRST" | "FUNDAMENTAL_FIRST";

/**
 * Event correlation trigger type
 */
export type TriggerType = "SCANNER_ANOMALY" | "NEWS_EVENT";

// ============================================================================
// Core Interfaces
// ============================================================================

/**
 * Multi-agent validated trade suggestion
 */
export interface TradeSuggestion {
  suggestion_id: string;
  symbol: string;
  instrument_key: string;
  trading_symbol: string | null;
  consensus_score: number;
  confidence_level: ConfidenceLevel;
  signal_direction: SignalDirection;
  trigger_pathway: TriggerPathway;
  scanner_signal: Record<string, any>;
  ai_signal: Record<string, any>;
  ml_signal: Record<string, any>;
  entry_price: number | null;
  stop_loss: number | null;
  risk_reward_ratio: number | null;
  take_profit_1: number | null;
  take_profit_2: number | null;
  take_profit_3: number | null;
  generated_at: string;
  expires_at: string;
  status: SuggestionStatus;
  created_at: string;
  updated_at: string;
}

/**
 * Event correlation audit trail
 */
export interface EventCorrelation {
  correlation_id: string;
  suggestion_id: string | null;
  trigger_type: TriggerType;
  trigger_timestamp: string;
  scanner_response_ms: number | null;
  ai_response_ms: number | null;
  ml_response_ms: number | null;
  total_latency_ms: number | null;
  consensus_reached: boolean;
  rejection_reason: string | null;
  scanner_output: Record<string, any> | null;
  ai_output: Record<string, any> | null;
  ml_output: Record<string, any> | null;
  created_at: string;
}

// ============================================================================
// API Response Interfaces
// ============================================================================

/**
 * Paginated list of trade suggestions
 * Response from: GET /api/v1/trade-suggestions
 */
export interface SuggestionsListResponse {
  suggestions: TradeSuggestion[];
  total: number;
  page: number;
  page_size: number;
  has_more: boolean;
}

/**
 * Detailed suggestion with correlation history
 * Response from: GET /api/v1/trade-suggestions/{suggestion_id}
 */
export interface SuggestionDetailResponse {
  suggestion: TradeSuggestion;
  correlations: EventCorrelation[];
  correlation_count: number;
}

/**
 * Aggregate statistics for trade suggestions
 * Response from: GET /api/v1/trade-suggestions/stats/summary
 */
export interface SuggestionStatsResponse {
  total_active: number;
  total_today: number;
  avg_consensus_score: number;
  high_confidence_count: number;
  buy_count: number;
  sell_count: number;
  avg_latency_ms: number;
  consensus_rate: number;
}

// ============================================================================
// Filter/Query Interfaces
// ============================================================================

/**
 * Query filters for listing trade suggestions
 */
export interface SuggestionFilters {
  direction?: SignalDirection;
  confidence_level?: ConfidenceLevel;
  min_confidence?: number;
  status?: SuggestionStatus;
  symbol?: string;
  page?: number;
  page_size?: number;
}

// ============================================================================
// Helper Constants
// ============================================================================

export const CONFIDENCE_COLORS: Record<ConfidenceLevel, string> = {
  HIGH: "green",
  MEDIUM: "yellow",
  LOW: "orange",
};

export const DIRECTION_COLORS: Record<SignalDirection, string> = {
  BUY: "green",
  SELL: "red",
};

export const STATUS_COLORS: Record<SuggestionStatus, string> = {
  active: "blue",
  expired: "gray",
  executed: "green",
  invalidated: "red",
};
