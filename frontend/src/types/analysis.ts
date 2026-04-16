/**
 * CORTEX Trading Platform - Analysis Types
 *
 * Type definitions for ML analysis, AI insights, and trading verdicts.
 * These types mirror the AI microservice API response structures.
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
