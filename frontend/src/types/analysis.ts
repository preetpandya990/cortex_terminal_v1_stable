/**
 * CORTEX Trading Platform - Analysis Types
 *
 * Type definitions for ML analysis, AI insights, trading verdicts,
 * and the new AI Analysis Cards (pattern detection + sentiment + synthesis).
 * These types mirror the backend API response structures.
 */

// ─── AI Analysis ────────────────────────────────────────────────────────────

export interface SentimentAnalysis {
  overall_sentiment: 'positive' | 'negative' | 'neutral';
  sentiment_score: number;   // -1 to 1
  confidence: number;        // 0 to 1
}

export interface KeyInsight {
  category: string;
  insight: string;
  importance: number;        // 0 to 1
}

export interface AIAnalysisResponse {
  symbol: string;
  sentiment_analysis: SentimentAnalysis;
  key_insights: KeyInsight[];
}

// ─── ML Analysis ─────────────────────────────────────────────────────────────

export interface PricePrediction {
  predicted_price: number;
  direction: 'bullish' | 'bearish' | 'neutral';
  confidence: number;        // 0 to 1
  timeframe: string;
}

export interface PatternRecognition {
  detected_patterns: string[];
  strength: number;          // 0 to 1
  reliability: number;       // 0 to 1
}

export interface MLAnalysisResponse {
  symbol: string;
  price_prediction: PricePrediction;
  pattern_recognition: PatternRecognition;
}

// ─── Verdict ─────────────────────────────────────────────────────────────────

export interface VerdictResponse {
  symbol: string;
  overall_verdict: 'buy' | 'sell' | 'hold';
  confidence_score: number;  // 0 to 1
  risk_level: 'low' | 'medium' | 'high';
  summary: string;
}

// ─── AI Analysis Cards ────────────────────────────────────────────────────────
// New types for the revamped AnalysisCardsSection (ML Pattern + AI Sentiment + Synthesis)

export interface PatternDetection {
  name: string;           // e.g. "HAMMER", "ENGULFING"
  timestamp: string;      // ISO 8601
  confidence: number;     // 100 or 200 (TA-Lib values)
  direction: 'bullish' | 'bearish';
}

export interface HistoricalStats {
  sample_size: number;
  accuracy: number;       // 0.0 to 1.0
  avg_move_pct: number;
  tp1_hit_rate: number;
  tp2_hit_rate: number;
  tp3_hit_rate: number;
  sl_hit_rate: number;
}

export interface PatternAnalysisCard {
  patterns: PatternDetection[];
  total_detected: number;
  timeframe: string;
  analyzed_candles: number;
  best_pattern: PatternDetection | null;
  cache_tier: string | null;
  error: string | null;
  error_message: string | null;
  historical_accuracy: number | null;
  historical_stats: HistoricalStats | null;
}

export interface TopEvent {
  title: string;
  sentiment: 'positive' | 'negative' | 'neutral';
  confidence: number;
  source: string;
  published_at: string | null;
  impact_contribution: number;
}

export interface SentimentBreakdown {
  positive: number;
  negative: number;
  neutral: number;
  total: number;
  positive_pct: number;
  negative_pct: number;
  neutral_pct: number;
}

export interface SentimentAnalysisCard {
  instrument_key: string;
  symbol: string | null;
  breakdown: SentimentBreakdown;
  impact_score: number;   // -100 to +100
  sentiment_label: 'bullish' | 'bearish' | 'neutral';
  top_event: TopEvent | null;
  lookback_hours: number;
  cache_tier: string | null;
  computed_at: string;
  error: string | null;
  error_message: string | null;
}

/** Client-side synthesis of pattern + sentiment into a trading recommendation */
export interface PredictionSynthesis {
  recommendation: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;           // 0.0 to 1.0
  ml_insight: string;
  ai_insight: string;
  action_text: string;
  signal_strength: 'strong' | 'moderate' | 'weak';
}

/** SSE `analysis_update` event payload */
export interface AnalysisStreamEvent {
  pattern: PatternAnalysisCard | null;
  sentiment: SentimentAnalysisCard | null;
  instrument_key: string;
  emitted_at: string;
}
