"""
Pydantic schemas for the ML Training Operator Console — Phase 1.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

_NO_NAMESPACE = ConfigDict(protected_namespaces=())


class ProbeStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class ProbeResult(BaseModel):
    name: str
    label: str
    status: ProbeStatus
    message: str
    value: Any = None
    remediation: str | None = None


class PreflightReport(BaseModel):
    can_launch: bool
    probes: list[ProbeResult]
    checked_at: str


class TrainingConfigOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_version: str | None = Field(None, pattern=r"^\d+\.\d+\.\d+$")
    n_symbols: int | None = Field(None, ge=10, le=5000)
    lookback_years: int | None = Field(None, ge=1, le=20)
    xgboost_trials: int | None = Field(None, ge=1, le=500)
    gru_trials: int | None = Field(None, ge=1, le=100)


class LaunchMode(str, Enum):
    FRESH  = "fresh"   # --fresh: discard any existing checkpoint, start from scratch
    RESUME = "resume"  # no --fresh: auto-resume from the existing checkpoint


class LaunchRequest(BaseModel):
    reason: str = Field(min_length=5, max_length=500)
    mode: LaunchMode = LaunchMode.FRESH
    override_schedule_warning: bool = False
    # config overrides and feedback_weights_path are only honoured in FRESH mode;
    # RESUME ignores them — the checkpoint's locked config governs the resumed run.
    config: TrainingConfigOverride = Field(default_factory=TrainingConfigOverride)
    feedback_weights_path: str | None = None


class CheckpointStatus(BaseModel):
    """Current on-disk checkpoint state — used by the Launch form to decide mode."""

    model_config = ConfigDict(protected_namespaces=())

    exists: bool
    resumable: bool            # exists AND incomplete AND schema-compatible
    run_id: str | None = None
    started_at: str | None = None
    model_version: str | None = None
    schema_version: int | None = None
    completed_steps: list[str] = Field(default_factory=list)
    next_step: str | None = None
    # GRU sub-C state — populated when step_6_gru is the next step and a
    # per-epoch training_state.json exists (i.e. GRU was interrupted mid-run).
    gru_last_epoch: int | None = None


class LaunchResponse(BaseModel):
    run_id: str
    launched_at: str
    checkpoint_dir: str
    message: str


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class RunSummary(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    run_id: str
    status: RunStatus
    started_at: str | None = None
    completed_at: str | None = None
    duration_s: float | None = None
    mlflow_run_id: str | None = None
    model_version: str | None = None
    steps_completed: list[str] = Field(default_factory=list)
    source: str = "unknown"
    error_summary: str | None = None


class RunsListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class ActiveRunResponse(BaseModel):
    active: bool
    run_id: str | None = None
    launched_at: str | None = None
    pid: int | None = None


# ── Phase 2: Feedback weight bundle schemas ────────────────────────────────────

class FeedbackBundleInfo(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    bundle_path:        str
    sha256:             str
    row_count:          int   # unique (instrument_key, signal_date) pairs in the parquet
    total_raw_outcomes: int   # sum of outcome_count — total closed trades aggregated
    window_start:       str
    window_end:         str
    created_at:         str
    weight_mean:        float
    weight_std:         float
    weight_p5:          float
    weight_p50:         float
    weight_p95:         float
    histogram_bins:     list[float]
    histogram_counts:   list[int]
    top_symbols:        list[dict[str, Any]]


class FeedbackBundlesListResponse(BaseModel):
    bundles: list[FeedbackBundleInfo]
    total: int
