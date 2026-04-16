/**
 * Cortex Terminal V1 - ML Prediction Types
 *
 * Type definitions for ML model registry and prediction APIs.
 */

export type PredictionSignal = "BUY" | "SELL" | "HOLD";

export interface MLModel {
  id: number;
  name: string;
  version: string;
  model_type: string;
  algorithm: string;
  target_description: string | null;
  feature_count: number | null;
  training_samples: number | null;
  training_date_from: string | null;
  training_date_to: string | null;
  metrics: Record<string, number> | null;
  is_active: boolean;
  created_at: string;
}

export interface MLPrediction {
  prediction_time: string;
  symbol: string;
  model_id: number;
  horizon: string;
  signal: PredictionSignal;
  direction: "long" | "short" | "neutral";
  confidence: number;
  probability_long: number;
  probability_short: number;
  expected_move_pct?: number | null;
  entry_price?: number | null;
  target_price?: number | null;
  stop_loss?: number | null;
  risk_reward_ratio?: number | null;
  risk_score?: number | null;
  reasoning?: string | null;
  reasons?: string[] | null;
  feature_contributions?: Record<string, number> | null;
  created_at: string;
}

export interface MLPredictionListResponse {
  symbol: string;
  horizon?: string | null;
  count: number;
  predictions: MLPrediction[];
}

export interface PredictResponse {
  status: string;
  predictions_generated: number;
  errors: Array<Record<string, string>>;
}

export interface CachedPrediction {
  symbol: string;
  horizon: string;
  direction: "long" | "short" | "neutral";
  confidence: number;
  probability_long: number;
  probability_short: number;
  target_price: number;
  stop_loss: number;
  risk_reward_ratio: number;
  expected_move_pct: number;
  reasoning: string | null;
  entry_price: number;
  prediction_time: string;
  risk_score: number;
}

export interface MLAlert {
  symbol: string;
  horizon: string;
  confidence: number;
  sharpe: number | null;
  reason: string;
  timestamp: string;
}

export interface MLRollingPredictionResponse {
  symbol: string;
  horizon: string;
  latest: CachedPrediction | null;
  count: number;
  predictions: MLPrediction[];
}
