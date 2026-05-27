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

// ── Ensemble Status (Single-Active-Member pattern) ────────────────────────────

export interface EnsembleGateCheck {
  metric:   string;
  required: number;
  current:  number;
  gap:      number;
  pass:     boolean;
}

export interface EnsembleActivationGate {
  checks:   EnsembleGateCheck[];
  all_pass: boolean;
}

export interface EnsembleMemberMetrics {
  auc_pr:          number | null;
  deflated_sharpe: number | null;
  ece_after:       number | null;
  accuracy:        number | null;
  symbol_coverage: number | null;
}

export interface EnsembleMember {
  model_name:             string;
  model_version:          string;
  status:                 string;
  is_active:              boolean;
  role:                   "active" | "dormant";
  effective_weight:       number;
  /** Raw A5 optimizer recommendation from training — may differ from effective_weight. */
  training_weight:        number | null;
  /** False when EnsembleNotAccretiveError was raised during training (GRU solo beat every blend). */
  is_ensemble_accretive:  boolean | null;
  deployed_at:            string | null;
  metrics:                EnsembleMemberMetrics;
  activation_gate:        EnsembleActivationGate | null;
}

export interface EnsembleStatus {
  mode:    "full_ensemble" | "xgboost_only" | "degraded";
  members: EnsembleMember[];
}

export interface UpdateModelStateRequest {
  new_state: ModelState;
  reason:    string;
}
