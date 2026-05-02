/**
 * Cortex AI — Paper Trading Types
 * =================================
 * TypeScript types mirroring the backend Pydantic schemas in
 * app.schemas.paper_trading. All Decimal DB columns are surfaced as
 * `number` (float) in JSON — matching the backend's coerce_decimal logic.
 */

// ============================================================================
// Enums (String Literal Unions)
// ============================================================================

export type PortfolioType = "PAPER" | "LIVE";
export type TransactionType = "BUY" | "SELL";
export type ProductType = "CNC" | "MIS" | "NRML";
export type OrderType = "MARKET" | "LIMIT" | "SL" | "SL-M";
export type OrderValidity = "DAY" | "IOC";
export type OrderStatus = "PENDING" | "OPEN" | "COMPLETE" | "REJECTED" | "CANCELLED";
export type PositionSide = "LONG" | "SHORT";
export type PositionStatus = "OPEN" | "CLOSED";
export type ExitReason = "TP1" | "TP2" | "TP3" | "SL" | "MANUAL" | "EXPIRED";

// ============================================================================
// Request Interfaces
// ============================================================================

export interface CreatePortfolioRequest {
  name: string;
  initial_capital: number;
  risk_per_trade_pct?: number;
  max_open_positions?: number;
}

export interface UpdatePortfolioSettingsRequest {
  risk_per_trade_pct?: number;
  max_open_positions?: number;
}

export interface PlaceOrderRequest {
  suggestion_id: string;
  transaction_type: TransactionType;
  product_type: ProductType;
  order_type: OrderType;
  validity?: OrderValidity;
  quantity: number;
  price?: number;
  trigger_price?: number;
}

export interface ClosePositionRequest {
  quantity: number;
  order_type?: OrderType;
  price?: number;
  exit_reason?: ExitReason;
}

// ============================================================================
// Response Interfaces
// ============================================================================

export interface Portfolio {
  id: string;
  user_id: number;
  portfolio_type: PortfolioType;
  name: string;
  initial_capital: number;
  current_cash: number;
  currency: string;
  risk_per_trade_pct: number;
  max_open_positions: number;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface PortfolioSummary extends Portfolio {
  open_position_count: number;
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  portfolio_value: number;
  total_return_pct: number;
  total_trades: number;
  win_rate: number | null;
}

export interface PaperOrder {
  id: string;
  portfolio_id: string;
  suggestion_id: string | null;
  symbol: string;
  instrument_key: string;
  transaction_type: TransactionType;
  product_type: ProductType;
  order_type: OrderType;
  validity: OrderValidity;
  quantity: number;
  price: number | null;
  trigger_price: number | null;
  status: OrderStatus;
  rejection_reason: string | null;
  placed_at: string;
  updated_at: string;
}

export interface PaperFill {
  id: string;
  order_id: string;
  portfolio_id: string;
  symbol: string;
  fill_quantity: number;
  fill_price: number;
  slippage_bps: number;
  brokerage: number;
  stt: number;
  exchange_charges: number;
  sebi_charges: number;
  gst: number;
  stamp_duty: number;
  total_charges: number;
  net_amount: number;
  settlement_date: string;
  executed_at: string;
}

export interface PaperPosition {
  id: string;
  portfolio_id: string;
  suggestion_id: string | null;
  symbol: string;
  instrument_key: string;
  quantity: number;
  avg_cost_price: number;
  last_price: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number;
  total_charges: number;
  side: PositionSide;
  target_price_1: number | null;
  target_price_2: number | null;
  target_price_3: number | null;
  stop_loss: number | null;
  status: PositionStatus;
  opened_at: string;
  closed_at: string | null;
  updated_at: string;
}

export interface PaperPositionDetail {
  position: PaperPosition;
  fills: PaperFill[];
  outcome: PaperTradeOutcome | null;
  fill_count: number;
}

export interface PaperTradeOutcome {
  id: string;
  portfolio_id: string;
  position_id: string;
  suggestion_id: string | null;
  user_id: number;
  symbol: string;
  signal_direction: TransactionType;
  entry_price: number;
  exit_price: number;
  quantity: number;
  gross_pnl: number;
  total_charges: number;
  net_pnl: number;
  pnl_pct: number;
  hold_duration_seconds: number;
  exit_reason: ExitReason;
  suggested_entry_price: number | null;
  suggested_stop_loss: number | null;
  suggested_tp1: number | null;
  suggested_tp2: number | null;
  suggested_tp3: number | null;
  suggestion_consensus_score: number | null;
  suggestion_confidence_level: string | null;
  entry_slippage_pct: number | null;
  ml_direction_correct: boolean | null;
  hit_tp1: boolean;
  hit_tp2: boolean;
  hit_tp3: boolean;
  hit_sl: boolean;
  market_regime_at_entry: string | null;
  created_at: string;
}

export interface PaperPnlSnapshot {
  id: number;
  portfolio_id: string;
  snapshot_date: string;
  total_realized_pnl: number;
  total_unrealized_pnl: number;
  portfolio_value: number;
  cash_balance: number;
  num_open_positions: number;
  num_trades_today: number;
  win_rate: number | null;
  nifty_close: number | null;
  captured_at: string;
}

// ============================================================================
// List / Paginated Responses
// ============================================================================

export interface OrdersListResponse {
  orders: PaperOrder[];
  total: number | null;
  next_cursor: string | null;
  has_more: boolean;
  limit: number;
}

export interface PositionsListResponse {
  positions: PaperPosition[];
  open_count: number;
  closed_count: number;
  total_unrealized_pnl: number;
}

export interface OutcomesListResponse {
  outcomes: PaperTradeOutcome[];
  total: number | null;
  next_cursor: string | null;
  has_more: boolean;
  limit: number;
}

export interface PnlSnapshotsListResponse {
  snapshots: PaperPnlSnapshot[];
  count: number;
  from_date: string | null;
  to_date: string | null;
}

// ============================================================================
// Utility Responses
// ============================================================================

export interface QtySuggestionResponse {
  suggested_qty: number;
  max_affordable_qty: number;
  capital_at_risk: number;
  risk_pct_of_cash: number;
  entry_price: number;
  stop_loss: number;
  stop_distance: number;
  current_cash: number;
  risk_per_trade_pct: number;
  note: string | null;
}

export interface OutcomeStatsBreakdown {
  count: number;
  win_rate: number | null;
  avg_net_pnl: number | null;
  avg_pnl_pct: number | null;
  ml_direction_accuracy: number | null;
}

export interface OutcomeStatsResponse {
  total_trades: number;
  total_winners: number;
  total_losers: number;
  win_rate: number | null;
  total_gross_pnl: number;
  total_charges: number;
  total_net_pnl: number;
  avg_net_pnl_per_trade: number | null;
  avg_pnl_pct: number | null;
  avg_hold_duration_hours: number | null;
  ml_direction_accuracy: number | null;
  tp1_hit_rate: number | null;
  tp2_hit_rate: number | null;
  tp3_hit_rate: number | null;
  sl_hit_rate: number | null;
  by_exit_reason: Record<string, number>;
  by_confidence_level: Record<string, OutcomeStatsBreakdown>;
  by_market_regime: Record<string, OutcomeStatsBreakdown>;
  by_signal_direction: Record<string, OutcomeStatsBreakdown>;
  from_date: string | null;
  to_date: string | null;
  generated_at: string;
}

// ============================================================================
// Real-time WebSocket Payloads
// ============================================================================

export interface LivePositionPnL {
  position_id: string;
  symbol: string;
  quantity: number;
  side: PositionSide;
  avg_cost_price: number;
  last_price: number;
  unrealized_pnl: number;
  pnl_pct: number;
  stop_loss: number | null;
  target_price_1: number | null;
}

export interface LivePnLUpdate {
  portfolio_id: string;
  positions: LivePositionPnL[];
  total_unrealized_pnl: number;
  total_realized_pnl: number;
  current_cash: number;
  portfolio_value: number;
  total_return_pct: number;
  ts: string;
}

// ============================================================================
// Query / Filter Interfaces
// ============================================================================

export interface OrdersQueryParams {
  status?: OrderStatus;
  cursor?: string;
  limit?: number;
}

export interface PositionsQueryParams {
  status?: PositionStatus;
}

export interface OutcomesQueryParams {
  cursor?: string;
  limit?: number;
}

export interface PnlSnapshotsQueryParams {
  from_date?: string;
  to_date?: string;
  limit?: number;
}

// ============================================================================
// UI Helper Constants
// ============================================================================

export const SIDE_COLORS: Record<PositionSide, { bg: string; text: string }> = {
  LONG: { bg: "bg-emerald-100", text: "text-emerald-700" },
  SHORT: { bg: "bg-rose-100", text: "text-rose-700" },
};

export const ORDER_STATUS_COLORS: Record<OrderStatus, { bg: string; text: string }> = {
  PENDING: { bg: "bg-amber-100", text: "text-amber-700" },
  OPEN: { bg: "bg-blue-100", text: "text-blue-700" },
  COMPLETE: { bg: "bg-emerald-100", text: "text-emerald-700" },
  REJECTED: { bg: "bg-rose-100", text: "text-rose-700" },
  CANCELLED: { bg: "bg-slate-100", text: "text-slate-600" },
};

export const EXIT_REASON_LABELS: Record<ExitReason, string> = {
  TP1: "Target 1",
  TP2: "Target 2",
  TP3: "Target 3",
  SL: "Stop Loss",
  MANUAL: "Manual Close",
  EXPIRED: "Expired",
};
