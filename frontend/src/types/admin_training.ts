// Types for the ML Training Operator Console — Phase 1

export type ProbeStatus = 'pass' | 'warn' | 'fail';

export interface ProbeResult {
  name: string;
  label: string;
  status: ProbeStatus;
  message: string;
  value?: unknown;
  remediation?: string | null;
}

export interface PreflightReport {
  can_launch: boolean;
  probes: ProbeResult[];
  checked_at: string;
}

export interface TrainingConfigOverride {
  model_version?: string | null;
  n_symbols?: number | null;
  lookback_years?: number | null;
  xgboost_trials?: number | null;
  gru_trials?: number | null;
}

export interface LaunchRequest {
  reason: string;
  override_schedule_warning?: boolean;
  config?: TrainingConfigOverride;
  feedback_weights_path?: string | null;
}

export interface FeedbackBundleInfo {
  bundle_path:        string;
  sha256:             string;
  /** Number of unique (instrument_key, signal_date) rows in the parquet. */
  row_count:          number;
  /** Total closed trades aggregated into this bundle (≥ row_count). */
  total_raw_outcomes: number;
  window_start:       string;
  window_end:         string;
  created_at:         string;
  weight_mean:        number;
  weight_std:         number;
  weight_p5:          number;
  weight_p50:         number;
  weight_p95:         number;
  histogram_bins:     number[];
  histogram_counts:   number[];
  top_symbols:        Array<{ symbol: string; mean_weight: number; n_outcomes: number }>;
}

export interface FeedbackBundlesListResponse {
  bundles: FeedbackBundleInfo[];
  total: number;
}

export interface LaunchResponse {
  run_id: string;
  launched_at: string;
  checkpoint_dir: string;
  message: string;
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'unknown';

export interface RunSummary {
  run_id: string;
  status: RunStatus;
  started_at?: string | null;
  completed_at?: string | null;
  duration_s?: number | null;
  mlflow_run_id?: string | null;
  model_version?: string | null;
  steps_completed: string[];
  source: string;
  error_summary?: string | null;
}

export interface RunsListResponse {
  runs: RunSummary[];
  total: number;
}

export interface ActiveRunResponse {
  active: boolean;
  run_id?: string | null;
  launched_at?: string | null;
  pid?: number | null;
}

// WebSocket frame types (server → client)

export interface RunEventFrame {
  type: 'run_event';
  data: RunLogEntry;
}

export interface RunCompleteFrame {
  type: 'run_complete';
  exit_code: number;
  success: boolean;
}

export interface ConnectedFrame {
  type: 'connected';
  run_id: string;
}

export interface ErrorFrame {
  type: 'error';
  code: string;
  message?: string;
}

export interface PongFrame {
  type: 'pong';
  ts: string;
}

export type TrainingWsFrame =
  | RunEventFrame
  | RunCompleteFrame
  | ConnectedFrame
  | ErrorFrame
  | PongFrame;

// Structured log entry from run_log.ndjson

export interface RunLogEntry {
  ts: string;
  event: string;
  // run_start
  run_id?: string;
  mlflow_run_id?: string;
  model_version?: string;
  resumed?: boolean;
  // step_complete
  step?: string;
  step_num?: number;
  duration_s?: number;
  metrics?: Record<string, number>;
  // run_failed / run_finished
  promotion_report_path?: string | null;
  // c2_report_generated
  status?: string;
  report_path?: string | null;
  // f1_event_backtest_complete
  agreement_status?: string;
  divergence_pct?: number | null;
  mean_ann_sharpe?: number | null;
  total_fills?: number | null;
}

// UI state

export type TrainingTab = 'preflight' | 'launch' | 'live' | 'history' | 'feedback';

export const PIPELINE_STEPS: Array<{ key: string; label: string }> = [
  { key: 'step_1_symbols',   label: 'Symbol Selection'       },
  { key: 'step_2_features',  label: 'Feature Computation'    },
  { key: 'step_3_targets',   label: 'Target Generation'      },
  { key: 'step_4_splits',    label: 'CV Plan (CPCV)'         },
  { key: 'step_5_xgboost',   label: 'XGBoost Training'       },
  { key: 'step_6_gru',       label: 'GRU Training'           },
  { key: 'step_7_ensemble',  label: 'Ensemble Optimisation'  },
  { key: 'step_8_evaluation','label': 'Evaluation'           },
  { key: 'step_9_onnx',      label: 'ONNX Export'            },
  { key: 'step_10_registry', label: 'Model Registry'         },
];
