/**
 * Cortex AI — Market Events Types
 * ================================
 * TypeScript types mirroring the backend intelligence/events API response.
 * Event types and field names match the actual DB values in ai_event_classifications.
 */

// ============================================================================
// Enums (String Literal Unions — match DB values exactly)
// ============================================================================

export type EventType =
  | "company_news"
  | "sector_news"
  | "market_data"
  | "geopolitical"
  | "regulatory"
  | "general"
  | "earnings"
  | "merger_acquisition"
  | "fed_announcement"
  | "management_change"
  | "product_launch"
  | "analyst_rating";

export type SentimentLabel = "positive" | "negative" | "neutral";

// ============================================================================
// Core Event Interface — matches GET /intelligence/events response shape
// ============================================================================

export interface MarketEvent {
  id: number;
  event_type: EventType;
  source_name: string;
  source_url: string;
  content: string;
  generated_at: string;
  impact_score: number;
  confidence: number | null;
  affected_symbols: string[];
  sentiment: SentimentLabel | null;
  sentiment_score: number | null;
  reasoning: string | null;
}

// ============================================================================
// Response / List Interface
// ============================================================================

export interface MarketEventsResponse {
  events: MarketEvent[];
  total: number;
  page: number;
  limit: number;
}

// ============================================================================
// Filter Interface
// ============================================================================

export interface EventFilters {
  symbol?: string;
  event_type?: EventType;
  min_impact?: number;
  page?: number;
  limit?: number;
}

// ============================================================================
// UI Helpers
// ============================================================================

export const EVENT_TYPE_META: Record<
  EventType,
  { label: string; badgeCls: string }
> = {
  company_news:      { label: "Company",        badgeCls: "bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/20" },
  sector_news:       { label: "Sector",         badgeCls: "bg-indigo-500/10 text-indigo-700 dark:text-indigo-300 border-indigo-500/20" },
  market_data:       { label: "Market",         badgeCls: "bg-slate-500/10 text-slate-700 dark:text-slate-300 border-slate-500/20" },
  geopolitical:      { label: "Geopolitical",   badgeCls: "bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/20" },
  regulatory:        { label: "Regulatory",     badgeCls: "bg-red-500/10 text-red-700 dark:text-red-300 border-red-500/20" },
  general:           { label: "General",        badgeCls: "bg-gray-500/10 text-gray-700 dark:text-gray-300 border-gray-500/20" },
  earnings:          { label: "Earnings",       badgeCls: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/20" },
  merger_acquisition:{ label: "M&A",            badgeCls: "bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/20" },
  fed_announcement:  { label: "Fed / RBI",      badgeCls: "bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/20" },
  management_change: { label: "Management",     badgeCls: "bg-pink-500/10 text-pink-700 dark:text-pink-300 border-pink-500/20" },
  product_launch:    { label: "Product",        badgeCls: "bg-cyan-500/10 text-cyan-700 dark:text-cyan-300 border-cyan-500/20" },
  analyst_rating:    { label: "Analyst",        badgeCls: "bg-violet-500/10 text-violet-700 dark:text-violet-300 border-violet-500/20" },
};

export const SENTIMENT_META: Record<
  SentimentLabel,
  { label: string; cls: string; dotCls: string }
> = {
  positive: { label: "Positive", cls: "text-emerald-600 dark:text-emerald-400", dotCls: "bg-emerald-500" },
  negative: { label: "Negative", cls: "text-rose-600 dark:text-rose-400",       dotCls: "bg-rose-500" },
  neutral:  { label: "Neutral",  cls: "text-slate-500 dark:text-slate-400",     dotCls: "bg-slate-400" },
};
