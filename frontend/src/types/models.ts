export enum ModelState {
  SHADOW  = "shadow",
  PAPER   = "paper",
  LIVE    = "live",
  RETIRED = "retired",
}

/** Valid state transitions — mirrors the backend state machine. */
export const VALID_TRANSITIONS: Record<ModelState, ModelState[]> = {
  [ModelState.SHADOW]:  [ModelState.PAPER],
  [ModelState.PAPER]:   [ModelState.LIVE, ModelState.SHADOW],
  [ModelState.LIVE]:    [ModelState.SHADOW],
  [ModelState.RETIRED]: [ModelState.SHADOW],
};

/** Quality gate thresholds — mirrors unified_model_registry.py */
export const QUALITY_GATES: Record<ModelState, Array<{ metric: keyof ModelMetrics; label: string; threshold: number }>> = {
  [ModelState.PAPER]: [
    { metric: "accuracy", label: "Accuracy", threshold: 0.55 },
  ],
  [ModelState.LIVE]: [
    { metric: "accuracy",  label: "Accuracy",  threshold: 0.58 },
    { metric: "precision", label: "Precision", threshold: 0.53 },
    { metric: "recall",    label: "Recall",    threshold: 0.50 },
  ],
  [ModelState.SHADOW]:  [],
  [ModelState.RETIRED]: [],
};

export interface ModelMetrics {
  accuracy:  number | null;
  precision: number | null;
  recall:    number | null;
  f1_score:  number | null;
}

export interface MLModel {
  model_id:         number;
  model_name:       string;
  model_type:       string;
  version:          string;
  deployment_state: ModelState;
  timeframe:        string | null;
  training_date:    string | null;
  metrics:          ModelMetrics;
  registered_at:    string;
  updated_at:       string;
}

export interface DriftReport {
  id:                   number;
  model_id:             number;
  model_name:           string;
  report_timestamp:     string;
  drift_detected:       boolean;
  drift_score:          number | null;
  accuracy_drop:        number | null;
  distribution_metrics: Record<string, unknown> | null;
  action_taken:         string | null;
  created_at:           string;
}

export interface GovernanceSummary {
  states: {
    live:    number;
    paper:   number;
    shadow:  number;
    retired: number;
  };
  drift_alerts_24h: number;
  total_models:     number;
}

export interface ModelsResponse {
  models: MLModel[];
  total:  number;
  page:   number;
  limit:  number;
}

export interface DriftReportsResponse {
  reports: DriftReport[];
  total:   number;
}

export interface ModelFilters {
  state?: string;
  page?:  number;
  limit?: number;
}

export interface UpdateModelStateRequest {
  new_state: ModelState;
  reason:    string;
}
