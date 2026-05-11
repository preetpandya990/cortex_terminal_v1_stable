// PATH: frontend/src/types/strategies.ts
/**
 * Trading Strategies — TypeScript type definitions
 * Mirrors the Pydantic schemas in backend/app/schemas/strategies.py
 */

// ─── Enums ────────────────────────────────────────────────────────────────────

export type EnforcementMode = 'hard_gate' | 'soft_filter' | 'portfolio_opt_in';
export type SizingMode = 'fixed_qty' | 'risk_pct' | 'kelly';
export type StrategyStatus = 'draft' | 'active' | 'paused' | 'archived';
export type StrategyScope = 'private' | 'shared';
export type ActivationState =
  | 'WATCHING'
  | 'IN_SETUP'
  | 'IN_TRADE'
  | 'PARTIAL_EXIT'
  | 'EXITED'
  | 'CANCELLED';
export type ActivationExitReason =
  | 'TP1'
  | 'TP2'
  | 'TP3'
  | 'FULL_TP'
  | 'SL'
  | 'MANUAL'
  | 'SIGNAL_EXPIRED'
  | 'STRATEGY_PAUSED'
  | 'RULE_VIOLATED';
export type BacktestMode = 'signal_replay' | 'model_replay' | 'hybrid';
export type BacktestStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';
export type ConditionOperator =
  | '>'
  | '>='
  | '<'
  | '<='
  | '=='
  | '!='
  | 'between'
  | 'crosses_above'
  | 'crosses_below';
export type LogicalOperator = 'AND' | 'OR' | 'NOT';
export type StopLossType = 'fixed_pct' | 'atr_multiple' | 'price_level';

// ─── Condition Tree ───────────────────────────────────────────────────────────

export interface ConditionLeaf {
  type: 'leaf';
  field: string;
  op: ConditionOperator;
  value: number | string | [number, number];
}

export interface ConditionGroup {
  type: 'group';
  operator: LogicalOperator;
  children: ConditionNode[];
}

export type ConditionNode = ConditionLeaf | ConditionGroup;

// ─── Configuration Sub-Types ──────────────────────────────────────────────────

export interface StopLossConfig {
  type: StopLossType;
  value: number;
  atr_period: number;
  trail: boolean;
}

export interface TakeProfitLevel {
  pct: number;
  exit_fraction: number;
}

export interface TakeProfitConfig {
  levels: TakeProfitLevel[];
}

export interface PositionSizingConfig {
  mode: SizingMode;
  fixed_qty?: number | null;
  risk_pct?: number | null;
  kelly_fraction?: number | null;
  max_position_pct: number;
}

export interface GuardrailsConfig {
  max_concurrent_activations: number;
  min_confidence_score: number;
  allowed_sessions: string[];
  require_volume_confirm: boolean;
  max_daily_loss_pct?: number | null;
}

export interface SignalGatesConfig {
  allowed_symbols: string[];
  blocked_symbols: string[];
  allowed_directions: ('BUY' | 'SELL')[];
  allowed_timeframes: string[];
  min_market_cap_cr?: number | null;
  allowed_regimes: string[];
}

export interface StrategyDefinition {
  signal_gates: SignalGatesConfig;
  signal_condition?: ConditionGroup | null;
  indicator_condition?: ConditionGroup | null;
  stop_loss?: StopLossConfig | null;
  take_profit?: TakeProfitConfig | null;
  position_sizing: PositionSizingConfig;
  guardrails: GuardrailsConfig;
}

// ─── API Response Types ───────────────────────────────────────────────────────

export interface UserPreferences {
  id: number;
  user_id: number;
  enforcement_mode: EnforcementMode;
  hard_gate_notify: boolean;
  default_position_sizing_mode: SizingMode;
  default_risk_per_trade_pct: number | null;
  max_concurrent_activations: number;
  created_at: string;
  updated_at: string;
}

export interface UserProfile {
  id: number;
  username: string;
  email: string;
  full_name: string | null;
  role: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface Strategy {
  id: string;
  created_by: number;
  name: string;
  description: string | null;
  tags: string[] | null;
  scope: StrategyScope;
  status: StrategyStatus;
  version: number;
  definition: StrategyDefinition;
  total_subscribers: number;
  avg_daily_return_pct: number | null;
  win_rate_pct: number | null;
  total_trades: number;
  created_at: string;
  updated_at: string;
}

export interface StrategySummary {
  id: string;
  name: string;
  description: string | null;
  tags: string[] | null;
  scope: StrategyScope;
  status: StrategyStatus;
  version: number;
  total_subscribers: number;
  avg_daily_return_pct: number | null;
  win_rate_pct: number | null;
  total_trades: number;
  created_at: string;
}

export interface UserStrategySubscription {
  id: number;
  user_id: number;
  strategy_id: string;
  priority: number;
  is_active: boolean;
  notes: string | null;
  subscribed_at: string;
  unsubscribed_at: string | null;
  strategy?: StrategySummary | null;
}

export interface StrategyActivation {
  id: string;
  user_id: number;
  strategy_id: string;
  strategy_snapshot: StrategyDefinition;
  signal_id: number | null;
  symbol: string;
  state: ActivationState;
  state_history: Array<{ state: string; at: string; from?: string; [key: string]: unknown }>;
  auto_execute: boolean;
  entry_order_id: string | null;
  exit_order_id: string | null;
  exit_reason: ActivationExitReason | null;
  entry_at: string | null;
  exit_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface StrategyTrade {
  id: string;
  strategy_id: string;
  user_id: number;
  activation_id: string;
  symbol: string;
  direction: 'LONG' | 'SHORT';
  entry_price: number;
  exit_price: number;
  quantity: number;
  gross_pnl: number;
  total_charges: number;
  net_pnl: number;
  pnl_pct: number;
  exit_reason: ActivationExitReason;
  strategy_version: number;
  hold_duration_seconds: number;
  entry_at: string;
  exit_at: string;
  created_at: string;
}

export interface StrategyBacktestRun {
  id: string;
  user_id: number;
  strategy_id: string;
  strategy_snapshot: StrategyDefinition;
  mode: BacktestMode;
  symbols: string[];
  start_dt: string;
  end_dt: string;
  status: BacktestStatus;
  progress_pct: number;
  result_metrics: Record<string, unknown> | null;
  error_detail: string | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
}

export interface StrategyPerformance {
  strategy_id: string;
  total_trades: number;
  win_rate_pct: number | null;
  avg_daily_return_pct: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown_pct: number | null;
  total_net_pnl: number;
  avg_hold_duration_seconds: number | null;
  best_trade_pnl: number | null;
  worst_trade_pnl: number | null;
}

// ─── List Response Types ──────────────────────────────────────────────────────

export interface StrategiesListResponse {
  items: StrategySummary[];
  total: number;
  page: number;
  page_size: number;
}

export interface SubscriptionsListResponse {
  items: UserStrategySubscription[];
  total: number;
}

export interface ActivationsListResponse {
  items: StrategyActivation[];
  total: number;
  page: number;
  page_size: number;
}

export interface TradesListResponse {
  items: StrategyTrade[];
  total: number;
  page: number;
  page_size: number;
  cursor: string | null;
}

export interface BacktestRunsListResponse {
  items: StrategyBacktestRun[];
  total: number;
}

// ─── Request Types ────────────────────────────────────────────────────────────

export interface CreateStrategyRequest {
  name: string;
  description?: string | null;
  tags?: string[];
  definition: StrategyDefinition;
}

export interface UpdateStrategyRequest {
  name?: string;
  description?: string | null;
  tags?: string[];
  status?: StrategyStatus;
  definition?: StrategyDefinition;
}

export interface UpdateUserPreferencesRequest {
  enforcement_mode?: EnforcementMode;
  hard_gate_notify?: boolean;
  default_position_sizing_mode?: SizingMode;
  default_risk_per_trade_pct?: number | null;
  max_concurrent_activations?: number;
}

export interface UpdateUserProfileRequest {
  full_name?: string | null;
  email?: string;
  current_password?: string;
  new_password?: string;
}

export interface SubscribeStrategyRequest {
  priority: number;
  notes?: string | null;
}

export interface ManualExitRequest {
  exit_price?: number | null;
}

export interface ReorderSubscriptionsRequest {
  subscription_ids: number[];
}

export interface TriggerBacktestRequest {
  symbols: string[];
  start_dt: string;
  end_dt: string;
  mode?: BacktestMode;
}
