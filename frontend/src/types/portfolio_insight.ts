/**
 * Portfolio Insight & Advise — client types
 *
 * Mirrors backend/app/schemas/portfolio_insight.py (B4 stats + B5 advice).
 * Snake_case fields match the JSON wire format 1:1. All figures are plain
 * numbers already rounded server-side.
 */

// ============================================================================
// Risk panel — GET /portfolio-insight/stats
// ============================================================================

export interface CapitalAtRiskStat {
  /** Σ |avg_cost − stop| × qty over stopped positions (INR) */
  capital_at_risk: number;
  /** capital_at_risk ÷ portfolio_value × 100 */
  capital_at_risk_pct: number;
  positions_with_stop: number;
  /** Open positions with no stop — excluded from CaR (honest gap) */
  positions_without_stop: number;
}

export interface HoldingWeight {
  symbol: string;
  /** Position market value ÷ portfolio_value × 100 */
  weight_pct: number;
}

export interface SingleNameConcentration {
  /** Largest single-name weight (% of portfolio value) */
  max_weight_pct: number;
  max_weight_symbol: string | null;
  /** Herfindahl-Hirschman Index Σ wᵢ² (1/n … 1); higher ⇒ more concentrated */
  hhi: number;
  /** 1 / HHI — how many equal-weight names the book behaves like */
  effective_positions: number;
  top_holdings: HoldingWeight[];
}

export interface SectorWeight {
  sector: string;
  weight_pct: number;
}

export interface SectorConcentration {
  max_sector: string | null;
  max_sector_weight_pct: number;
  /** Weight of positions with no resolvable sector (%) */
  unclassified_weight_pct: number;
  breakdown: SectorWeight[];
}

export interface CorrelationStat {
  /** Highest pairwise correlation in [-1, 1] */
  max_pair_correlation: number | null;
  /** The two symbols achieving max_pair_correlation */
  max_pair: [string, string] | null;
  avg_pairwise_correlation: number | null;
  /** Instruments with enough history to correlate */
  covered_positions: number;
  /** Instruments excluded for short history */
  excluded_positions: number;
  /** Trailing daily-return window used */
  window_days: number;
}

/** Stable scenario id emitted by the backend stress scan */
export type StressScenarioKey = "index_down" | "sector_down" | "vol_double";

export interface StressScenario {
  key: StressScenarioKey;
  label: string;
  /** Portfolio value change under the scenario (%) */
  delta_pct: number;
  detail: string | null;
}

export interface StressScan {
  scenarios: StressScenario[];
}

export interface PortfolioInsightStats {
  portfolio_id: string;
  portfolio_value: number;
  open_position_count: number;
  capital_at_risk: CapitalAtRiskStat;
  single_name: SingleNameConcentration;
  sector: SectorConcentration;
  correlation: CorrelationStat;
  stress: StressScan;
  /** Human-readable honest-gap notes (unstopped positions, excluded names, missing sectors) */
  notes: string[];
  /** ISO 8601 timestamp (UTC) */
  computed_at: string;
}

// ============================================================================
// AI advice — POST /portfolio-insight/advice
// ============================================================================

export interface PerPositionNote {
  symbol: string;
  note: string;
}

export interface PortfolioAdvice {
  assessment: string;
  key_risks: string[];
  considerations: string[];
  per_position: PerPositionNote[];
  /** Fixed regulatory notice (server-set, never LLM-generated) */
  disclaimer: string;
  /** True when served from cache during a quota/rate-limit degrade (not freshly generated) */
  stale: boolean;
  /** ISO 8601 timestamp (UTC) */
  generated_at: string;
  /** LLM model id that produced the advice, for provenance */
  model_id: string | null;
}
