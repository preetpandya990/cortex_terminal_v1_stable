/**
 * Types for ML models
 */

export enum ModelState {
  SHADOW = "shadow",
  PAPER = "paper",
  LIVE = "live",
  RETIRED = "retired",
}

export interface MLModel {
  model_id: number;
  model_name: string;
  model_type: string;
  version: string;
  deployment_state: ModelState;
  accuracy_metrics: Record<string, number>;
  registered_at: string;
  last_prediction_at?: string;
}

export interface DriftReport {
  report_id: number;
  model_id: number;
  model_name: string;
  drift_score: number;
  drift_detected: boolean;
  metrics: Record<string, any>;
  detected_at: string;
}

export interface ModelsResponse {
  models: MLModel[];
  total: number;
}

export interface DriftReportsResponse {
  reports: DriftReport[];
  total: number;
}

export interface ModelFilters {
  model_type?: string;
  deployment_state?: ModelState;
  page?: number;
  limit?: number;
}

export interface UpdateModelStateRequest {
  new_state: ModelState;
  reason: string;
}
