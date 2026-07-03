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
  name: string;            // e.g. "HAMMER", "ENGULFING"
  timestamp: string;       // ISO 8601
  confidence: number;      // 100 or 200 (TA-Lib values)
  direction: 'bullish' | 'bearish';
  composite_score?: number; // reliability × signal_strength × recency_decay × volume_factor
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

/** Client-side synthesis of ensemble + pattern + sentiment into a trading recommendation */
export interface PredictionSynthesis {
  recommendation:  'BUY' | 'SELL' | 'HOLD';
  confidence:      number;                          // 0.0 to 1.0
  signal_strength: 'strong' | 'moderate' | 'weak';
  confirmed_count: number;                          // 0–3 sources that confirm direction
  synthesis_text:  string;                          // generated reasoning paragraph
}

// ─── ML Ensemble Prediction ───────────────────────────────────────────────────

/** Per-model view before ensemble blending (XGBoost or GRU). */
export interface ModelPrediction {
  direction: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;         // 0–1
  conviction_scale: number;   // 0–1 (0 = at regime threshold, 1 = max confidence)
  threshold: number;          // regime-adaptive gate: 0.55 | 0.60 | 0.70
  probabilities: {
    buy:  number;
    sell: number;
    hold: number;
  };
  weight:  number;  // ensemble weight, e.g. 0.75
  version: string;  // model artefact version string
}

/** Ensemble prediction when models are loaded and data is available. */
export interface MLEnsemblePredictionData {
  available: true;
  unavailable_reason: null;
  direction: 'BUY' | 'SELL' | 'HOLD';
  confidence:       number;  // 0–1
  conviction_scale: number;  // 0–1
  threshold:        number;  // regime-adaptive threshold
  probabilities: {
    buy:  number;
    sell: number;
    hold: number;
  };
  entry_price: number;
  stop_loss:   number;
  tp1: number;
  tp2: number;
  tp3: number;
  volatility: number;  // annualised
  models: {
    xgboost: ModelPrediction | null;
    gru:     ModelPrediction | null;
  };
  xgboost_version: string;
  gru_version:     string | null;
  xgboost_weight:  number;
  gru_weight:      number;
  timeframe:    string;
  predicted_at: string;  // ISO 8601
}

/** Returned when no model is deployed or data is insufficient. */
export interface MLEnsemblePredictionUnavailable {
  available: false;
  unavailable_reason: 'no_model' | 'insufficient_data' | string;
}

/** Discriminated union — narrow with `if (pred.available)` */
export type MLEnsemblePrediction =
  | MLEnsemblePredictionData
  | MLEnsemblePredictionUnavailable;

// ─── LLM Explanation ──────────────────────────────────────────────────────────

/** A single news source cited by the LLM in an explanation. */
export interface ExplanationSource {
  /** Publication name, e.g. "Economic Times Markets" */
  source_name: string;
  /** ISO-8601 UTC timestamp of the original article */
  as_of: string;
  /** Original article URL */
  source_url: string;
}

/**
 * LLM-generated plain-English explanation for a trade suggestion or instrument.
 *
 * ``available`` distinguishes two pending states:
 *   false → explanation worker has not finished (show skeleton)
 *   true  → explanation text is ready for display
 *
 * ``sources`` is only populated on the real-time push path.  On the
 * periodic poll fallback, sources will be an empty array — inline citations
 * within ``full_explanation`` still provide attribution.
 *
 * ``context_type`` determines the display mode in AIExplanationPanel:
 *   'suggestion_explanation' → explanation for a specific (possibly recent but
 *                               non-active) ML signal; renders staleness banner
 *   'instrument_context'     → market context for a Watchlist item with no
 *                               recent signal; no staleness banner
 *
 * ``signal_direction`` and ``signal_generated_at`` are populated only when
 * ``context_type === 'suggestion_explanation'``.
 */
export interface ExplanationData {
  available:        boolean;
  /**
   * True when the explanation pipeline failed permanently after MAX_ATTEMPTS
   * (Gemini quota exhausted, repeated timeouts, or DLQ promotion).  The panel
   * renders a static "Analysis unavailable" state instead of an eternal skeleton.
   * Mutually exclusive with ``available: true`` — a failed explanation is never
   * available.  A skeleton (available=false, failed=false) can still transition
   * to available=true; a failed one cannot without a server-side requeue.
   */
  failed?:          boolean;
  /**
   * True while the worker is streaming the explanation token-by-token:
   * ``available`` is still false but ``full_explanation`` holds the partial text
   * so the panel renders it progressively instead of showing the skeleton.
   */
  streaming?:       boolean;
  summary:          string | null;
  full_explanation: string | null;
  model:            string | null;
  /** ISO-8601 UTC timestamp when the explanation was written */
  generated_at:     string | null;
  sources:          ExplanationSource[];

  /**
   * Discriminates the content type so the panel renders appropriate copy.
   * null only during the very brief window before the first SSE poll resolves.
   */
  context_type:       'suggestion_explanation' | 'instrument_context' | null;

  /** Signal direction — populated only for 'suggestion_explanation' context. */
  signal_direction:   'BUY' | 'SELL' | null;

  /** ISO-8601 UTC timestamp when the underlying signal was created. */
  signal_generated_at: string | null;

  /**
   * True when the suggestion's consensus_score is below EXPLANATION_CONSENSUS_THRESHOLD.
   * The correlation engine intentionally skipped the explanation job; this state
   * persists until the user requests an on-demand explanation via the bypass endpoint.
   *
   * Mutually exclusive with ``available: true`` and ``failed: true``.
   * The panel renders a "confidence below threshold" card with a CTA button.
   */
  weak_signal?: boolean;

  /**
   * UUID of the backing TradeSuggestion.  Populated only when ``weak_signal === true``
   * so the panel can construct the bypass endpoint URL without an extra lookup.
   */
  suggestion_id?: string;

  /**
   * Composite consensus score (0–100) for the underlying suggestion.
   * Populated when ``weak_signal === true`` so the panel can display the score
   * as context ("Consensus score: 68/100").
   */
  consensus_score?: number;
}

/** SSE `analysis_update` event payload */
export interface AnalysisStreamEvent {
  prediction:  MLEnsemblePrediction | null;
  pattern:     PatternAnalysisCard   | null;
  sentiment:   SentimentAnalysisCard | null;
  /**
   * LLM explanation for the latest active suggestion on this instrument.
   * null  → no active suggestion (hide the panel).
   * obj   → active suggestion exists; check ``available`` for readiness.
   */
  explanation: ExplanationData       | null;
  instrument_key: string;
  emitted_at:     string;
}
