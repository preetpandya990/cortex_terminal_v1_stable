"""
ML Training Operator Console — Phase 1 REST + WebSocket API.
============================================================
Admin-only endpoints for pre-flight validation, training dispatch, live run
streaming, history, and cancellation.

REST routes (router — prefix: /api/v1/admin/training):
    GET  /preflight               — run all 6 pre-flight probes
    POST /launch                  — acquire lock + launch orchestrator subprocess
    GET  /runs                    — list runs from checkpoint dir + MLflow
    GET  /runs/active             — current active run info (if any)
    POST /runs/{run_id}/cancel    — graceful SIGTERM to active orchestrator

WebSocket route (ws_router — same prefix):
    WS   /runs/{run_id}/stream    — tail run_log.ndjson for an active run

Auth: all endpoints require admin role (require_admin_role dependency).
WS:  in-band auth ({"type": "auth", "token": "..."} as first frame, 10 s timeout).
Lock: fcntl.flock on .scheduled_retrain.lock — prevents concurrent runs with the
      systemd weekly timer; the API process holds the fd until the subprocess exits.
"""
import asyncio
import fcntl
import json
import logging
import os
import re
import shutil
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.api.deps import get_db
from app.core.auth import AdminUserID
from app.core.limiter import limiter
from app.schemas.admin_training import (
    ActiveRunResponse,
    CheckpointStatus,
    FeedbackBundleInfo,
    FeedbackBundlesListResponse,
    LaunchMode,
    LaunchRequest,
    LaunchResponse,
    PreflightReport,
    ProbeResult,
    ProbeStatus,
    RunStatus,
    RunSummary,
    RunsListResponse,
)

logger = logging.getLogger(__name__)

router    = APIRouter()
ws_router = APIRouter()

# ── Path constants ─────────────────────────────────────────────────────────────

_PROJECT_ROOT      = Path(__file__).parent.parent.parent.parent  # backend/
_CHECKPOINT_DIR    = _PROJECT_ROOT / "models" / "production" / "checkpoints"
_RUN_LOG_PATH      = _CHECKPOINT_DIR / "run_log.ndjson"
_CHECKPOINT_FILE   = _CHECKPOINT_DIR / "checkpoint.json"
_ERROR_STATE_DIR   = _PROJECT_ROOT / "models" / "production"
_MLRUNS_DIR        = _PROJECT_ROOT / "mlruns"
_REQUIREMENTS_FILE = _PROJECT_ROOT / "requirements.txt"
_LOGS_DIR          = _PROJECT_ROOT / "logs" / "training_ui"

# Shared lock file (same one used by scheduled_retrain.py + systemd timer).
_LOCK_PATH: Path | None = None
try:
    from app.ml.config import SCHEDULED_RETRAIN
    _LOCK_PATH = _PROJECT_ROOT / SCHEDULED_RETRAIN["lock_file"]
except Exception:
    _LOCK_PATH = _PROJECT_ROOT / ".scheduled_retrain.lock"

# Critical packages whose version must match requirements.txt exactly.
_CRITICAL_PACKAGES = {"numpy", "scikit-learn", "tensorflow", "torch", "onnx", "xgboost"}

# ── In-process active run registry ────────────────────────────────────────────
# Maps API-level run_id (UUID str) → _RunRecord.
# All mutations are serialised by _registry_lock (asyncio.Lock).

@dataclass
class _RunRecord:
    api_run_id: str
    launched_at: datetime
    proc: asyncio.subprocess.Process
    lock_fh: IO[str]
    log_fh: IO[str]
    reason: str
    user_id: str
    pid: int

_registry: dict[str, _RunRecord] = {}
_registry_lock: asyncio.Lock = asyncio.Lock()

# ── WS auth constant ───────────────────────────────────────────────────────────
_WS_AUTH_TIMEOUT = 10.0


# ══════════════════════════════════════════════════════════════════════════════
# Preflight probes
# ══════════════════════════════════════════════════════════════════════════════

async def _probe_gpu() -> ProbeResult:
    """
    VRAM availability check with stale-context-aware severity classification.

    Severity logic
    --------------
    PASS  — free VRAM ≥ 2500 MiB.
    WARN  — free VRAM < 2500 MiB but zero active CUDA compute processes.
              No competing workload; low VRAM is caused by a stale driver
              context (WSL2/WDDM residue from a previously killed process)
              or by the display subsystem.  The CUDA runtime reclaims stale
              allocations on its next context initialisation.  GRU training
              falls back to CPU automatically if VRAM remains insufficient.
    WARN  — free VRAM 1500–2499 MiB with active compute processes present.
    FAIL  — free VRAM < 1500 MiB AND active CUDA compute processes exist.
              A live GPU workload is actively competing for VRAM; operator
              action is required before launching.

    Rationale: conflating "stale driver context" with "competing ML job"
    produces false-positive FAIL gates that block training unnecessarily.
    The compute-process check is the correct discriminator.
    """
    try:
        # ── 1. VRAM totals ─────────────────────────────────────────────────────
        vram_proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.free,memory.total",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        vram_out, _ = await asyncio.wait_for(vram_proc.communicate(), timeout=10.0)
        lines = vram_out.decode().strip().splitlines()
        if not lines:
            raise RuntimeError("No GPU output")

        free_mib, total_mib = (int(x.strip()) for x in lines[0].split(","))
        free_pct = 100.0 * free_mib / total_mib if total_mib else 0.0
        msg = f"{free_mib} MiB free of {total_mib} MiB ({free_pct:.0f}%)"

        if free_mib >= 2500:
            return ProbeResult(
                name="gpu", label="GPU Memory", status=ProbeStatus.PASS,
                message=msg, value=free_mib,
            )

        # ── 2. Active CUDA compute processes ───────────────────────────────────
        # This query excludes the Windows display driver; it only returns
        # processes with live CUDA contexts (ML jobs, CUDA benchmarks, etc.).
        compute_proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        compute_out, _ = await asyncio.wait_for(compute_proc.communicate(), timeout=10.0)
        compute_lines = [
            ln.strip() for ln in compute_out.decode().strip().splitlines() if ln.strip()
        ]
        has_compute_processes = len(compute_lines) > 0

        # ── 3. Severity classification ─────────────────────────────────────────
        if free_mib < 1500 and has_compute_processes:
            return ProbeResult(
                name="gpu", label="GPU Memory", status=ProbeStatus.FAIL,
                message=(
                    f"Low VRAM with active GPU workload — {msg} "
                    f"({len(compute_lines)} active CUDA process(es))"
                ),
                value=free_mib,
                remediation=(
                    "Stop the competing GPU process(es) before launching. "
                    f"Active processes: {compute_lines[:5]}"
                ),
            )

        # Low VRAM but no active compute processes — stale driver context
        # or display driver.  Not a real blocker; training proceeds normally.
        context = (
            "Stale CUDA context (WSL2/WDDM residue from a previous run) — "
            "the CUDA runtime will reclaim this memory on context initialisation. "
            "GRU will use CPU fallback if VRAM remains insufficient after reclaim."
        ) if not has_compute_processes else (
            "Low VRAM with active compute processes — training may be slower."
        )
        return ProbeResult(
            name="gpu", label="GPU Memory", status=ProbeStatus.WARN,
            message=f"Low VRAM — {msg}. {context}",
            value=free_mib,
            remediation=(
                "No action required. Training will launch normally; the GRU "
                "trainer falls back to CPU when GPU VRAM is insufficient. "
                "XGBoost training is GPU-independent."
            ),
        )

    except FileNotFoundError:
        return ProbeResult(
            name="gpu", label="GPU Memory", status=ProbeStatus.WARN,
            message="nvidia-smi not found — GPU unavailable (CPU training only).",
            remediation="Install NVIDIA drivers if GPU training is required.",
        )
    except asyncio.TimeoutError:
        return ProbeResult(
            name="gpu", label="GPU Memory", status=ProbeStatus.WARN,
            message="nvidia-smi timed out.",
        )
    except Exception as exc:
        return ProbeResult(
            name="gpu", label="GPU Memory", status=ProbeStatus.WARN,
            message=f"GPU probe error: {exc}",
        )


def _parse_requirement_version(raw: str) -> str:
    """
    Extract the bare version string from a requirements.txt line fragment that
    already had the package name split off (i.e. everything after "==").

    Handles two common forms that would otherwise cause false-positive drift:

    1. Inline comments:
         "2.21.0 # Bundles CUDA 12.9 + cuDNN 9"  →  "2.21.0"
       The '#' character starts a comment in requirements.txt; everything
       after it is documentation, not part of the version specifier.

    2. PEP 440 local version identifiers:
         "2.11.0+cpu"  →  "2.11.0"
       A '+' introduces a local label (build tags such as +cpu, +cu118,
       +rocm5.4.2).  Local labels are non-canonical from pip's perspective:
       a package pinned as "torch==2.11.0" in requirements.txt will install
       as "torch==2.11.0+cpu" on CPU-only hosts.  The base version is what
       we care about for ABI/API compatibility.
    """
    ver = raw.split("#")[0].strip()   # drop inline comment
    ver = ver.split("+")[0].strip()   # drop local version label (e.g. +cpu)
    return ver


async def _probe_env() -> ProbeResult:
    """Compare pip freeze against requirements.txt for the critical ML package set."""
    try:
        if not _REQUIREMENTS_FILE.exists():
            return ProbeResult(
                name="env", label="Package Env", status=ProbeStatus.WARN,
                message="requirements.txt not found — skipping env check.",
            )

        req_text = _REQUIREMENTS_FILE.read_text()
        locked: dict[str, str] = {}
        for line in req_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "==" in line:
                pkg, ver_raw = line.split("==", 1)
                pkg_name = pkg.lower().replace("_", "-").split("[")[0].strip()
                if pkg_name in _CRITICAL_PACKAGES:
                    locked[pkg_name] = _parse_requirement_version(ver_raw)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "freeze",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
        installed: dict[str, str] = {}
        for line in stdout.decode().splitlines():
            if "==" in line:
                pkg, ver_raw = line.split("==", 1)
                pkg_name = pkg.lower().replace("_", "-").split("[")[0].strip()
                if pkg_name in _CRITICAL_PACKAGES:
                    installed[pkg_name] = _parse_requirement_version(ver_raw)

        drifted = []
        for pkg, locked_ver in locked.items():
            inst_ver = installed.get(pkg)
            if inst_ver is None:
                drifted.append(f"{pkg} not installed (expected {locked_ver})")
            elif inst_ver != locked_ver:
                drifted.append(f"{pkg}: {inst_ver} ≠ {locked_ver}")

        if drifted:
            return ProbeResult(
                name="env", label="Package Env", status=ProbeStatus.FAIL,
                message=f"Critical package drift: {', '.join(drifted)}",
                value=drifted,
                remediation=(
                    "Run: pip install -r requirements.txt -r requirements-ml-training.txt "
                    "in a clean venv before training."
                ),
            )
        checked = list(locked.keys())
        return ProbeResult(
            name="env", label="Package Env", status=ProbeStatus.PASS,
            message=f"All {len(checked)} critical packages match requirements.txt",
            value=checked,
        )

    except asyncio.TimeoutError:
        return ProbeResult(
            name="env", label="Package Env", status=ProbeStatus.WARN,
            message="pip freeze timed out — skipping env check.",
        )
    except Exception as exc:
        return ProbeResult(
            name="env", label="Package Env", status=ProbeStatus.WARN,
            message=f"Env probe error: {exc}",
        )


async def _probe_data(session: AsyncSession) -> ProbeResult:
    """Check OHLCV symbol coverage vs. the min_symbol_coverage gate (0.60 × 2557 ≈ 1534)."""
    try:
        from app.ml.config import FEATURE_DEFINITIONS  # noqa: F401 (ensure config importable)

        result = await session.execute(text(
            "SELECT COUNT(DISTINCT instrument_key) FROM upstox_ohlcv "
            "WHERE timeframe = '1D' AND timestamp >= NOW() - INTERVAL '730 days'"
        ))
        symbol_count: int = result.scalar() or 0

        min_symbols = int(0.60 * 2557)
        coverage_pct = 100.0 * symbol_count / 2557
        msg = f"{symbol_count} symbols with 1D bars in last 2 years ({coverage_pct:.1f}%)"

        if symbol_count < min_symbols:
            return ProbeResult(
                name="data", label="Data Coverage", status=ProbeStatus.FAIL,
                message=f"Insufficient coverage — {msg} (need ≥{min_symbols})",
                value=symbol_count,
                remediation=(
                    "Run the Upstox ingestion backfill script to populate "
                    "missing OHLCV data before training."
                ),
            )
        warn_threshold = int(0.75 * 2557)
        if symbol_count < warn_threshold:
            return ProbeResult(
                name="data", label="Data Coverage", status=ProbeStatus.WARN,
                message=f"Low coverage — {msg}",
                value=symbol_count,
                remediation="Coverage is borderline. Consider running backfill first.",
            )
        return ProbeResult(
            name="data", label="Data Coverage", status=ProbeStatus.PASS,
            message=msg, value=symbol_count,
        )

    except Exception as exc:
        return ProbeResult(
            name="data", label="Data Coverage", status=ProbeStatus.WARN,
            message=f"DB probe error: {exc}",
            remediation="Verify database connectivity.",
        )


def _probe_lock() -> ProbeResult:
    """
    Non-blocking flock attempt on .scheduled_retrain.lock.

    A PASS is a point-in-time snapshot, not a reservation: the probe releases
    the lock before returning, so a retrain can start between this check and
    any subsequent launch (TOCTOU).  The authoritative concurrency guard is
    the flock *held* for the duration of the run by scheduled_retrain.py —
    treat this probe as advisory UI signal only, never as a launch gate.
    """
    if _LOCK_PATH is None:
        return ProbeResult(
            name="lock", label="Run Lock", status=ProbeStatus.WARN,
            message="Lock path not configured.",
        )
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(_LOCK_PATH, "a")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()
            return ProbeResult(
                name="lock", label="Run Lock", status=ProbeStatus.PASS,
                message="Lock available — no concurrent run in progress.",
            )
        except BlockingIOError:
            fh.close()
            pid_info = ""
            try:
                content = _LOCK_PATH.read_text()
                pid_info = f" ({content.strip().splitlines()[0]})"
            except Exception:
                pass
            return ProbeResult(
                name="lock", label="Run Lock", status=ProbeStatus.FAIL,
                message=f"Lock held by another process{pid_info}",
                remediation=(
                    "A training run is already in progress. Wait for it to complete "
                    "or cancel it via the API before launching a new run."
                ),
            )
    except Exception as exc:
        return ProbeResult(
            name="lock", label="Run Lock", status=ProbeStatus.WARN,
            message=f"Lock probe error: {exc}",
        )


def _probe_schedule() -> ProbeResult:
    """Check time until next systemd-timer / APScheduler retrain fire."""
    try:
        from app.ml.config import SCHEDULED_RETRAIN
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        tz        = ZoneInfo(SCHEDULED_RETRAIN["cron_timezone"])
        now_local = datetime.now(tz)

        _DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        target_dow  = _DOW.get(SCHEDULED_RETRAIN["cron_day_of_week"].lower(), 5)
        target_hour = SCHEDULED_RETRAIN["cron_hour"]

        days_ahead = (target_dow - now_local.weekday()) % 7
        next_fire  = (now_local + timedelta(days=days_ahead)).replace(
            hour=target_hour, minute=0, second=0, microsecond=0,
        )
        if next_fire <= now_local:
            next_fire += timedelta(weeks=1)

        hours_until = (next_fire - now_local).total_seconds() / 3600
        msg = (
            f"Next scheduled retrain in {hours_until:.1f} h "
            f"({next_fire.strftime('%a %Y-%m-%d %H:%M %Z')})"
        )

        if hours_until < 4:
            return ProbeResult(
                name="schedule", label="Schedule Gap", status=ProbeStatus.WARN,
                message=f"Close to scheduled retrain — {msg}",
                value=round(hours_until, 2),
                remediation=(
                    "The systemd timer fires in less than 4 hours. Enable "
                    "'Override schedule warning' to launch anyway."
                ),
            )
        return ProbeResult(
            name="schedule", label="Schedule Gap", status=ProbeStatus.PASS,
            message=msg, value=round(hours_until, 2),
        )

    except Exception as exc:
        return ProbeResult(
            name="schedule", label="Schedule Gap", status=ProbeStatus.WARN,
            message=f"Schedule probe error: {exc}",
        )


def _probe_disk() -> ProbeResult:
    """Check free disk space in the checkpoint output directory."""
    try:
        target = _ERROR_STATE_DIR
        target.mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(target)
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        msg = f"{free_gb:.1f} GB free of {total_gb:.1f} GB"

        if free_gb < 5.0:
            return ProbeResult(
                name="disk", label="Disk Space", status=ProbeStatus.FAIL,
                message=f"Critically low disk space — {msg}",
                value=round(free_gb, 2),
                remediation=(
                    f"Free at least 5 GB in {target} before launching. "
                    "Old checkpoints and error_state files can be archived."
                ),
            )
        if free_gb < 20.0:
            return ProbeResult(
                name="disk", label="Disk Space", status=ProbeStatus.WARN,
                message=f"Low disk space — {msg}",
                value=round(free_gb, 2),
                remediation="Consider clearing old checkpoint artifacts to free space.",
            )
        return ProbeResult(
            name="disk", label="Disk Space", status=ProbeStatus.PASS,
            message=msg, value=round(free_gb, 2),
        )

    except Exception as exc:
        return ProbeResult(
            name="disk", label="Disk Space", status=ProbeStatus.WARN,
            message=f"Disk probe error: {exc}",
        )


# ══════════════════════════════════════════════════════════════════════════════
# REST endpoints
# ══════════════════════════════════════════════════════════════════════════════

@router.get(
    "/preflight",
    response_model=PreflightReport,
    summary="Run all pre-flight checks",
    description=(
        "Execute all 6 pre-flight probes and return a per-gate pass/warn/fail report. "
        "A single FAIL gate means `can_launch` is False — the Launch tab must be blocked."
    ),
)
@limiter.limit("30/minute")
async def get_preflight(
    request: Request,
    user_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
) -> PreflightReport:
    gpu_p, env_p, data_p = await asyncio.gather(
        _probe_gpu(),
        _probe_env(),
        _probe_data(db),
    )
    lock_p     = _probe_lock()
    schedule_p = _probe_schedule()
    disk_p     = _probe_disk()

    probes = [gpu_p, env_p, data_p, lock_p, schedule_p, disk_p]
    can_launch = all(p.status != ProbeStatus.FAIL for p in probes)

    return PreflightReport(
        can_launch=can_launch,
        probes=probes,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )


@router.get(
    "/checkpoint",
    response_model=CheckpointStatus,
    summary="Get current checkpoint state",
    description=(
        "Read the on-disk checkpoint.json and return its state so the Launch form "
        "can decide whether to default to Resume or Fresh mode.  "
        "Returns exists=False when no checkpoint is present."
    ),
)
@limiter.limit("60/minute")
async def get_checkpoint_status(
    request: Request,
    user_id: AdminUserID,
) -> CheckpointStatus:
    _ALL_STEPS = [
        "step_1_symbols", "step_2_features", "step_3_targets", "step_4_splits",
        "step_5_xgboost", "step_6_gru", "step_7_ensemble", "step_8_evaluation",
        "step_9_onnx", "step_10_registry",
    ]

    if not _CHECKPOINT_FILE.exists():
        return CheckpointStatus(exists=False, resumable=False)

    try:
        state = json.loads(_CHECKPOINT_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return CheckpointStatus(exists=True, resumable=False)

    completed: list[str] = state.get("completed_steps", [])
    schema_version: int | None = state.get("schema_version")
    run_id: str | None = state.get("run_id")
    started_at: str | None = state.get("started_at")
    model_version: str | None = state.get("config", {}).get("model_version")

    # A checkpoint is resumable when it is incomplete and the schema version
    # matches what the current code expects (schema drift → incompatible resume).
    # Import lazily to avoid startup cost.
    try:
        from app.ml.training.checkpoint_manager import SCHEMA_VERSION
        schema_ok = (schema_version == SCHEMA_VERSION)
    except Exception:
        schema_ok = True  # unknown version — let the orchestrator decide

    is_complete  = set(completed) >= set(_ALL_STEPS)
    is_resumable = bool(completed) and not is_complete and schema_ok

    next_step = next((s for s in _ALL_STEPS if s not in completed), None)

    # GRU sub-C state: last completed epoch from training_state.json
    gru_last_epoch: int | None = None
    gru_training_state = _CHECKPOINT_DIR / "step_6_gru" / "training_state.json"
    if next_step == "step_6_gru" and gru_training_state.exists():
        try:
            gru_state = json.loads(gru_training_state.read_text())
            gru_last_epoch = gru_state.get("last_epoch")
        except Exception:
            pass

    return CheckpointStatus(
        exists=True,
        resumable=is_resumable,
        run_id=run_id,
        started_at=started_at,
        model_version=model_version,
        schema_version=schema_version,
        completed_steps=completed,
        next_step=next_step,
        gru_last_epoch=gru_last_epoch,
    )


@router.post(
    "/launch",
    response_model=LaunchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Launch a new training run",
    description=(
        "Acquire the shared retrain lock and launch the production orchestrator. "
        "When mode=fresh (default) the orchestrator runs with --fresh, discarding any "
        "existing checkpoint.  When mode=resume the checkpoint is preserved and the "
        "orchestrator resumes from the furthest completed step."
    ),
)
@limiter.limit("5/minute")
async def launch_training(
    request: Request,
    body: LaunchRequest,
    user_id: AdminUserID,
) -> LaunchResponse:
    import uuid

    # ── Guard: resume requires an existing resumable checkpoint ──────────────
    if body.mode == LaunchMode.RESUME:
        if not _CHECKPOINT_FILE.exists():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="No checkpoint found to resume. Use mode=fresh to start a new run.",
            )
        try:
            cp_state = json.loads(_CHECKPOINT_FILE.read_text())
            completed = set(cp_state.get("completed_steps", []))
            _ALL_STEPS = {
                "step_1_symbols", "step_2_features", "step_3_targets", "step_4_splits",
                "step_5_xgboost", "step_6_gru", "step_7_ensemble", "step_8_evaluation",
                "step_9_onnx", "step_10_registry",
            }
            if completed >= _ALL_STEPS:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Checkpoint is already complete. Use mode=fresh to start a new run.",
                )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Checkpoint file is corrupt or unreadable. Use mode=fresh.",
            )

    # ── Guard: check if schedule warning needs override ───────────────────────
    if not body.override_schedule_warning:
        schedule_probe = _probe_schedule()
        if schedule_probe.status == ProbeStatus.WARN and "Close to scheduled" in schedule_probe.message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Systemd timer fires soon: {schedule_probe.message}. "
                    "Set override_schedule_warning=true to proceed anyway."
                ),
            )

    # ── Guard: check if another run is already active ─────────────────────────
    async with _registry_lock:
        if _registry:
            run_id = next(iter(_registry))
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Training run {run_id} is already in progress.",
            )

    # ── Acquire the retrain lock (non-blocking) ────────────────────────────────
    if _LOCK_PATH is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Lock path not configured.",
        )
    lock_fh: IO[str]
    try:
        _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_fh = open(_LOCK_PATH, "w")
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Retrain lock is held — a concurrent training run (possibly the "
                "systemd weekly timer) is in progress. Try again later."
            ),
        )

    launched_at = datetime.now(timezone.utc)
    lock_fh.write(
        f"pid={os.getpid()}\n"
        f"started_at={launched_at.isoformat()}\n"
        f"user_id={user_id}\n"
        f"reason={body.reason[:100]}\n"
    )
    lock_fh.flush()

    # ── Build command ─────────────────────────────────────────────────────────
    # FRESH:  --fresh forces a clean slate, discarding any existing checkpoint.
    # RESUME: no --fresh — the orchestrator detects the checkpoint and resumes.
    #         config overrides and feedback_weights are intentionally ignored on
    #         resume; the checkpoint's locked config governs the continued run.
    cmd = [sys.executable, "scripts/production_training_orchestrator.py"]
    if body.mode == LaunchMode.FRESH:
        cmd.append("--fresh")
        if body.feedback_weights_path:
            from pathlib import Path as _Path
            _fb_path = _Path(body.feedback_weights_path)
            if not _fb_path.exists():
                lock_fh.close()
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Feedback bundle not found: {body.feedback_weights_path}",
                )
            cmd += ["--feedback-weights", str(_fb_path.resolve())]

    # ── Prepare per-run log file for stdout/stderr capture ───────────────────
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = launched_at.strftime("%Y%m%dT%H%M%SZ")
    log_file = _LOGS_DIR / f"training_ui_{stamp}.log"

    try:
        log_fh = open(log_file, "w", buffering=1, encoding="utf-8")
    except OSError as exc:
        lock_fh.close()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Could not create run log file: {exc}",
        )

    # ── Launch orchestrator subprocess ────────────────────────────────────────
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(_PROJECT_ROOT),
            stdout=log_fh,
            stderr=log_fh,
            env=env,
        )
    except Exception as exc:
        lock_fh.close()
        log_fh.close()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to launch orchestrator: {exc}",
        )

    api_run_id = str(uuid.uuid4())
    record = _RunRecord(
        api_run_id=api_run_id,
        launched_at=launched_at,
        proc=proc,
        lock_fh=lock_fh,
        log_fh=log_fh,
        reason=body.reason,
        user_id=user_id,
        pid=proc.pid,
    )

    async with _registry_lock:
        _registry[api_run_id] = record

    # Background task: wait for subprocess → release lock → remove from registry
    asyncio.create_task(
        _monitor_run(api_run_id),
        name=f"training-monitor-{api_run_id[:8]}",
    )

    logger.info(
        "Training run launched  api_run_id=%s  pid=%d  user_id=%s  mode=%s  reason=%.60s",
        api_run_id, proc.pid, user_id, body.mode.value, body.reason,
    )

    return LaunchResponse(
        run_id=api_run_id,
        launched_at=launched_at.isoformat(),
        checkpoint_dir=str(_CHECKPOINT_DIR),
        message=f"Orchestrator launched (pid={proc.pid}). Connect to WS to stream progress.",
    )


@router.get(
    "/runs/active",
    response_model=ActiveRunResponse,
    summary="Check if a training run is currently active",
)
@limiter.limit("60/minute")
async def get_active_run(
    request: Request,
    user_id: AdminUserID,
) -> ActiveRunResponse:
    async with _registry_lock:
        if not _registry:
            return ActiveRunResponse(active=False)
        run_id, record = next(iter(_registry.items()))
        # Check if subprocess is still running
        if record.proc.returncode is not None:
            return ActiveRunResponse(active=False)
        return ActiveRunResponse(
            active=True,
            run_id=run_id,
            launched_at=record.launched_at.isoformat(),
            pid=record.pid,
        )


@router.post(
    "/runs/{run_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel an active training run",
    description=(
        "Send SIGTERM to the active orchestrator subprocess. "
        "The orchestrator will write its current checkpoint before exiting, "
        "allowing the next launch to resume from that step."
    ),
)
@limiter.limit("10/minute")
async def cancel_run(
    run_id: str,
    request: Request,
    user_id: AdminUserID,
) -> dict[str, str]:
    async with _registry_lock:
        record = _registry.get(run_id)

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active run {run_id!r} not found. It may have already completed.",
        )

    if record.proc.returncode is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id!r} has already exited (code={record.proc.returncode}).",
        )

    try:
        record.proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        pass  # Process already gone — monitor task will clean up

    logger.info(
        "Training run cancel requested  api_run_id=%s  pid=%d  by_user=%s",
        run_id, record.pid, user_id,
    )
    return {
        "run_id": run_id,
        "status": "cancel_requested",
        "message": "SIGTERM sent to orchestrator. The checkpoint will be preserved.",
    }


@router.get(
    "/runs",
    response_model=RunsListResponse,
    summary="List training runs",
    description=(
        "Returns a combined list of: the current checkpoint state, "
        "MLflow experiment runs, and error_state_*.json files."
    ),
)
@limiter.limit("30/minute")
async def list_runs(
    request: Request,
    user_id: AdminUserID,
    limit: int = 20,
) -> RunsListResponse:
    runs: list[RunSummary] = []
    seen_ids: set[str] = set()

    # ── 1. Active run from in-process registry ────────────────────────────────
    async with _registry_lock:
        for api_run_id, record in _registry.items():
            if record.proc.returncode is None:
                runs.append(RunSummary(
                    run_id=api_run_id,
                    status=RunStatus.RUNNING,
                    started_at=record.launched_at.isoformat(),
                    source="api_registry",
                ))
                seen_ids.add(api_run_id)

    # ── 2. Current checkpoint state ───────────────────────────────────────────
    try:
        if _CHECKPOINT_FILE.exists():
            cp_state = json.loads(_CHECKPOINT_FILE.read_text())
            cp_run_id = cp_state.get("run_id", "unknown")
            if cp_run_id not in seen_ids:
                steps_done: list[str] = cp_state.get("completed_steps", [])
                is_complete = len(steps_done) == 10
                runs.append(RunSummary(
                    run_id=cp_run_id,
                    status=RunStatus.COMPLETED if is_complete else RunStatus.UNKNOWN,
                    started_at=cp_state.get("started_at"),
                    model_version=cp_state.get("config", {}).get("model_version"),
                    steps_completed=steps_done,
                    source="checkpoint",
                ))
                seen_ids.add(cp_run_id)
    except Exception as exc:
        logger.warning("Could not read checkpoint state: %s", exc)

    # ── 3. MLflow runs ────────────────────────────────────────────────────────
    try:
        import mlflow
        from mlflow.tracking import MlflowClient

        client = MlflowClient(tracking_uri=f"file://{_MLRUNS_DIR}")
        experiment = client.get_experiment_by_name("cortex_ml_training")
        if experiment:
            mlflow_runs = client.search_runs(
                experiment_ids=[experiment.experiment_id],
                order_by=["start_time DESC"],
                max_results=limit,
            )
            for r in mlflow_runs:
                cp_run_id = r.data.tags.get("checkpoint.run_id", r.info.run_id)
                if cp_run_id in seen_ids:
                    continue
                mlflow_status_map = {
                    "FINISHED": RunStatus.COMPLETED,
                    "FAILED": RunStatus.FAILED,
                    "RUNNING": RunStatus.RUNNING,
                }
                start_ms = r.info.start_time
                end_ms   = r.info.end_time
                runs.append(RunSummary(
                    run_id=cp_run_id,
                    status=mlflow_status_map.get(r.info.status, RunStatus.UNKNOWN),
                    started_at=(
                        datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat()
                        if start_ms else None
                    ),
                    completed_at=(
                        datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat()
                        if end_ms else None
                    ),
                    duration_s=(
                        (end_ms - start_ms) / 1000.0 if (start_ms and end_ms) else None
                    ),
                    mlflow_run_id=r.info.run_id,
                    model_version=r.data.tags.get("model.version"),
                    source="mlflow",
                ))
                seen_ids.add(cp_run_id)
    except Exception as exc:
        logger.warning("Could not query MLflow runs: %s", exc)

    # ── 4. Error state files ──────────────────────────────────────────────────
    try:
        error_files = sorted(
            _ERROR_STATE_DIR.glob("error_state_*.json"), reverse=True
        )
        for ef in error_files[:10]:
            stem = ef.stem.replace("error_state_", "")
            if stem in seen_ids:
                continue
            try:
                es = json.loads(ef.read_text())
                # Parse timestamp from filename: error_state_YYYYMMDD_HHMMSS.json
                ts_str = ef.stem[len("error_state_"):]
                started = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(
                    tzinfo=timezone.utc
                )
                runs.append(RunSummary(
                    run_id=stem,
                    status=RunStatus.FAILED,
                    started_at=started.isoformat(),
                    model_version=es.get("config", {}).get("model_version"),
                    error_summary=str(es.get("error", ""))[:200],
                    source="error_state",
                ))
                seen_ids.add(stem)
            except Exception:
                continue
    except Exception as exc:
        logger.warning("Could not read error_state files: %s", exc)

    # Sort by started_at descending, unknown last
    def _sort_key(r: RunSummary) -> tuple:
        ts = r.started_at or "0000"
        return (r.status == RunStatus.RUNNING, ts)

    runs.sort(key=_sort_key, reverse=True)
    runs = runs[:limit]
    return RunsListResponse(runs=runs, total=len(runs))


# ══════════════════════════════════════════════════════════════════════════════
# WebSocket — live run log stream
# ══════════════════════════════════════════════════════════════════════════════

@ws_router.websocket("/runs/{run_id}/stream")
async def stream_run_log(websocket: WebSocket, run_id: str) -> None:
    """
    Stream run_log.ndjson events for an active training run.

    Auth (in-band — token never in URL):
      Client sends {"type": "auth", "token": "<admin-jwt>"} as first frame.
      Server responds {"type": "connected", "run_id": "<run_id>"} on success.

    Close codes:
      4001 — auth failed or not admin
      4004 — run_id not found in active registry

    Event frames (server → client):
      {"type": "run_event", "data": {<ndjson entry>}}   — structured log event
      {"type": "run_complete", "exit_code": <int>}       — subprocess exited
      {"type": "pong", "ts": "<iso>"}                    — heartbeat reply
      {"type": "error", "code": "<string>"}              — error notification
    """
    from app.core.security import decode_token
    from app.exceptions import InvalidTokenError as CortexInvalidTokenError

    await websocket.accept()

    # ── In-band admin auth ────────────────────────────────────────────────────
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=_WS_AUTH_TIMEOUT)
        frame = json.loads(raw)
        if frame.get("type") != "auth" or not frame.get("token"):
            raise ValueError("expected {type: 'auth', token: '...'}")
        payload = decode_token(frame["token"], expected_type="access")
        if getattr(payload, "role", None) != "admin":
            await websocket.send_json({"type": "error", "code": "FORBIDDEN"})
            await websocket.close(code=4001, reason="Admin privileges required")
            return
        uid = payload.sub
    except asyncio.TimeoutError:
        await websocket.send_json({"type": "error", "code": "AUTH_TIMEOUT"})
        await websocket.close(code=4001, reason="Auth timeout")
        return
    except (CortexInvalidTokenError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Training WS auth failed: %s", exc)
        await websocket.send_json({"type": "error", "code": "AUTH_FAILED"})
        await websocket.close(code=4001, reason="Invalid token")
        return

    # ── Resolve run ───────────────────────────────────────────────────────────
    async with _registry_lock:
        record = _registry.get(run_id)

    if record is None:
        await websocket.send_json({"type": "error", "code": "RUN_NOT_FOUND"})
        await websocket.close(code=4004, reason="Run not found in active registry")
        return

    launched_at = record.launched_at

    await websocket.send_json({"type": "connected", "run_id": run_id})
    logger.info("Training WS connected: run_id=%s uid=%s", run_id, uid)

    # ── Replay checkpoint events for resume runs ──────────────────────────────
    # Emits synthetic step_complete frames for steps that completed before
    # launched_at (i.e. in a previous session).  No-op for fresh runs.
    await _replay_checkpoint_events(websocket, run_id, launched_at)

    # ── Stream existing + new events from run_log.ndjson ─────────────────────
    try:
        await _stream_run_log(websocket, run_id, launched_at, record)
    except WebSocketDisconnect:
        logger.info("Training WS disconnected: run_id=%s uid=%s", run_id, uid)
    except Exception:
        logger.exception("Training WS error: run_id=%s", run_id)
    finally:
        logger.info("Training WS cleaned up: run_id=%s uid=%s", run_id, uid)


async def _stream_run_log(
    websocket: WebSocket,
    api_run_id: str,
    launched_at: datetime,
    record: _RunRecord,
) -> None:
    """
    Tail run_log.ndjson from `launched_at`, emitting structured events.
    Terminates when:
      - subprocess exits (run_complete frame sent)
      - run_finished / run_failed event seen in log
      - client disconnects (WebSocketDisconnect propagates)
    """
    POLL_INTERVAL = 0.5   # seconds between file-tail attempts
    SEND_TIMEOUT  = 5.0   # max wait for a send to complete

    # Wait up to 10 s for the log file to appear (orchestrator starts asynchronously)
    wait_elapsed = 0.0
    while not _RUN_LOG_PATH.exists() and wait_elapsed < 10.0:
        await asyncio.sleep(0.5)
        wait_elapsed += 0.5

    if not _RUN_LOG_PATH.exists():
        await websocket.send_json({
            "type": "error",
            "code": "LOG_NOT_FOUND",
            "message": "run_log.ndjson has not appeared yet — orchestrator may still be initialising.",
        })

    # Open file in text mode; we poll the file position for new lines.
    log_file = open(_RUN_LOG_PATH, "r", encoding="utf-8") if _RUN_LOG_PATH.exists() else None
    sent_terminal = False

    try:
        # Seek to the position of the first relevant event (ts >= launched_at).
        if log_file:
            await asyncio.to_thread(_seek_to_launch, log_file, launched_at)

        while True:
            # ── Read available lines ──────────────────────────────────────────
            new_lines: list[str] = []
            if log_file:
                def _read_lines(fh: Any) -> list[str]:
                    lines = []
                    while True:
                        ln = fh.readline()
                        if not ln:
                            break
                        lines.append(ln.strip())
                    return lines
                new_lines = await asyncio.to_thread(_read_lines, log_file)

            for raw in new_lines:
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                await websocket.send_json({"type": "run_event", "data": entry})

                # Stop streaming on terminal events
                if entry.get("event") in ("run_finished", "run_failed"):
                    sent_terminal = True

            if sent_terminal:
                break

            # ── Check if subprocess has exited ────────────────────────────────
            async with _registry_lock:
                live_record = _registry.get(api_run_id)

            exit_code = record.proc.returncode
            if exit_code is not None:
                # Give log file one final read pass before closing
                await asyncio.sleep(0.5)
                if log_file:
                    final_lines = await asyncio.to_thread(_read_lines, log_file)
                    for raw in final_lines:
                        if raw:
                            try:
                                await websocket.send_json(
                                    {"type": "run_event", "data": json.loads(raw)}
                                )
                            except Exception:
                                pass
                await websocket.send_json({
                    "type": "run_complete",
                    "exit_code": exit_code,
                    "success": exit_code == 0,
                })
                break

            # ── Client ping handling (non-blocking) ───────────────────────────
            try:
                raw_msg = await asyncio.wait_for(
                    websocket.receive_text(), timeout=POLL_INTERVAL
                )
                msg = json.loads(raw_msg)
                if msg.get("type") == "ping":
                    await websocket.send_json(
                        {"type": "pong", "ts": datetime.now(timezone.utc).isoformat()}
                    )
                elif msg.get("type") == "reauth":
                    pass  # Token rotation — we don't need the token here after initial auth
            except asyncio.TimeoutError:
                pass  # Normal — no client message in this poll window
            except json.JSONDecodeError:
                pass

    finally:
        if log_file:
            try:
                log_file.close()
            except Exception:
                pass


def _seek_to_launch(fh: Any, launched_at: datetime) -> None:
    """
    Advance file position past all entries whose `ts` predates `launched_at`.
    Leaves the file positioned at the first entry at or after the launch time.
    """
    launched_ts = launched_at.isoformat()
    last_good_pos = 0
    # Use an epsilon: the orchestrator writes `run_start` a few seconds after
    # the API launches the subprocess, so we seek to 5 s before launched_at.
    from datetime import timedelta
    cutoff = (launched_at - timedelta(seconds=5)).isoformat()

    while True:
        pos = fh.tell()
        line = fh.readline()
        if not line:
            # EOF — reset to last known good position
            fh.seek(last_good_pos)
            return
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            if entry.get("ts", "") >= cutoff:
                # This entry is recent enough — rewind to start of this line
                fh.seek(pos)
                return
            last_good_pos = fh.tell()
        except json.JSONDecodeError:
            last_good_pos = fh.tell()


# ══════════════════════════════════════════════════════════════════════════════
# Checkpoint replay helper
# ══════════════════════════════════════════════════════════════════════════════

_PIPELINE_STEP_KEYS: list[str] = [
    "step_1_symbols", "step_2_features", "step_3_targets", "step_4_splits",
    "step_5_xgboost", "step_6_gru", "step_7_ensemble", "step_8_evaluation",
    "step_9_onnx", "step_10_registry",
]


async def _replay_checkpoint_events(
    websocket: WebSocket,
    api_run_id: str,
    launched_at: datetime,
) -> None:
    """
    Emit synthetic ``step_complete`` frames for all steps that completed before
    ``launched_at`` (i.e. in a prior session of the same training run).

    This makes the frontend's progress state immediately correct on resume:
    ``completedSteps``, ``stepDurations``, and ``currentStep`` are all populated
    from real checkpoint data without waiting for those events to arrive from the
    live log stream (they never would — ``_seek_to_launch`` skips them).

    Guard: if the checkpoint's ``started_at`` is within 5 minutes of
    ``launched_at`` the run is fresh and no replay is needed.  This prevents
    double-firing events that the live stream will deliver naturally.

    The final completed step's timestamp is set to ``launched_at`` so the
    frontend correctly computes elapsed time for the *current* (resumed) step
    from the start of this session rather than from the end of the previous one.
    """
    if not _CHECKPOINT_FILE.exists():
        return

    try:
        state = json.loads(_CHECKPOINT_FILE.read_text())
    except Exception:
        return

    completed: list[str] = state.get("completed_steps", [])
    if not completed:
        return

    # Fresh-run guard: skip replay if checkpoint is from this same launch session.
    started_at_str: str = state.get("started_at", "")
    try:
        cp_started = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
        if (launched_at - cp_started).total_seconds() < 300:
            return
    except Exception:
        return  # unparseable started_at — don't risk double-firing

    durations: dict[str, float] = state.get("step_durations_s", {})

    # Reconstruct historical timestamps using cumulative step durations.
    try:
        run_start_dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
    except Exception:
        run_start_dt = launched_at

    cumulative_s = 0.0
    n_completed   = len(completed)

    for idx, step_key in enumerate(_PIPELINE_STEP_KEYS, start=1):
        if step_key not in completed:
            break

        duration_s = durations.get(step_key) or 0.0
        cumulative_s += duration_s

        # Use launched_at for the final completed step so the frontend derives
        # the resumed step's start time from the current session, not from hours ago.
        is_last = (idx == n_completed)
        ts = launched_at if is_last else (run_start_dt + timedelta(seconds=cumulative_s))

        try:
            await websocket.send_json({"type": "run_event", "data": {
                "ts":         ts.isoformat(),
                "event":      "step_complete",
                "step":       step_key,
                "step_num":   idx,
                "duration_s": round(duration_s, 3) if duration_s else None,
            }})
        except Exception:
            logger.warning("Checkpoint replay interrupted at step %s", step_key)
            return

    logger.info(
        "Checkpoint replay: %d step_complete events sent  api_run_id=%s",
        n_completed, api_run_id,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Background monitoring task
# ══════════════════════════════════════════════════════════════════════════════

async def _monitor_run(api_run_id: str) -> None:
    """
    Wait for the orchestrator subprocess to exit, then release the flock and
    remove the run from the in-process registry.
    """
    async with _registry_lock:
        record = _registry.get(api_run_id)
    if record is None:
        return

    try:
        rc = await record.proc.wait()
        logger.info(
            "Training run exited  api_run_id=%s  pid=%d  exit_code=%d",
            api_run_id, record.pid, rc,
        )
    except Exception as exc:
        logger.exception("Training monitor error for run %s: %s", api_run_id, exc)
    finally:
        for fh in (record.lock_fh, record.log_fh):
            try:
                fh.close()
            except Exception:
                pass
        async with _registry_lock:
            _registry.pop(api_run_id, None)
        logger.info("Training run cleaned up  api_run_id=%s", api_run_id)


# ══════════════════════════════════════════════════════════════════════════════
# Phase 2 — Feedback weight bundle endpoints
# ══════════════════════════════════════════════════════════════════════════════

def _stats_to_bundle_info(stats: Any) -> FeedbackBundleInfo:
    return FeedbackBundleInfo(
        bundle_path        = stats.bundle_path,
        sha256             = stats.sha256,
        row_count          = stats.row_count,
        total_raw_outcomes = stats.total_raw_outcomes,
        window_start       = stats.window_start,
        window_end         = stats.window_end,
        created_at         = stats.created_at,
        weight_mean        = stats.weight_mean,
        weight_std         = stats.weight_std,
        weight_p5          = stats.weight_p5,
        weight_p50         = stats.weight_p50,
        weight_p95         = stats.weight_p95,
        histogram_bins     = stats.histogram_bins,
        histogram_counts   = stats.histogram_counts,
        top_symbols        = stats.top_symbols,
    )


@router.post(
    "/feedback/build",
    response_model=FeedbackBundleInfo,
    status_code=status.HTTP_201_CREATED,
    summary="Build a new feedback weight bundle",
    description=(
        "Query matured paper-trade outcomes (closed_at < NOW()-1d AND ml fields computed), "
        "apply the B1×B2 weight formula, write a parquet bundle + meta sidecar to "
        "feedback_bundles/, and return the bundle stats. Idempotent — each call produces "
        "a new timestamped bundle; old bundles are not deleted."
    ),
)
@limiter.limit("10/minute")
async def build_feedback_bundle(
    request: Request,
    user_id: AdminUserID,
    db: AsyncSession = Depends(get_db),
) -> FeedbackBundleInfo:
    from app.ml.training.feedback_loader import build_feedback_weights_df, write_bundle

    df = await build_feedback_weights_df(db)
    if df.empty:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "No matured outcomes found. "
                "Paper trades need at least 1 day of closed positions with ML fields "
                "populated (ml_direction_correct IS NOT NULL) before a bundle can be built."
            ),
        )

    stats = await asyncio.to_thread(write_bundle, df)
    logger.info(
        "Feedback bundle built by user=%s  rows=%d  sha256=%s…",
        user_id, stats.row_count, stats.sha256[:12],
    )
    return _stats_to_bundle_info(stats)


@router.get(
    "/feedback/bundles",
    response_model=FeedbackBundlesListResponse,
    summary="List available feedback weight bundles",
)
@limiter.limit("30/minute")
async def list_feedback_bundles(
    request: Request,
    user_id: AdminUserID,
) -> FeedbackBundlesListResponse:
    from app.ml.training.feedback_loader import list_bundles

    bundles = await asyncio.to_thread(list_bundles)
    return FeedbackBundlesListResponse(
        bundles=[_stats_to_bundle_info(s) for s in bundles],
        total=len(bundles),
    )


@router.delete(
    "/feedback/bundles/{bundle_name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a feedback weight bundle",
    description=(
        "Permanently delete a feedback bundle (parquet + meta sidecar). "
        "The bundle_name is the filename stem without extension, e.g. "
        "'feedback_weights_20260528T160638Z'. "
        "Returns 409 if a training run is currently active."
    ),
)
@limiter.limit("10/minute")
async def delete_feedback_bundle(
    bundle_name: str,
    request: Request,
    user_id: AdminUserID,
) -> None:
    # ── Validate name — reject anything that could escape the bundles directory ──
    _SAFE_NAME_RE = re.compile(r"^feedback_weights_\d{8}T\d{6}Z$")
    if not _SAFE_NAME_RE.match(bundle_name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid bundle name {bundle_name!r}. "
                "Expected pattern: feedback_weights_YYYYMMDDTHHMMSSz"
            ),
        )

    # ── Block if a training run is active — conservative safety guard ──────────
    async with _registry_lock:
        active_runs = [r for r in _registry.values() if r.proc.returncode is None]

    if active_runs:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Cannot delete a feedback bundle while a training run is active. "
                "Wait for the run to complete or cancel it first."
            ),
        )

    # ── Delete ─────────────────────────────────────────────────────────────────
    try:
        from app.ml.training.feedback_loader import delete_bundle

        deleted = await asyncio.to_thread(delete_bundle, bundle_name)
        logger.info(
            "Feedback bundle deleted  name=%s  sha256=%s…  rows=%d  user=%s",
            bundle_name,
            deleted.sha256[:12] if deleted.sha256 else "n/a",
            deleted.row_count,
            user_id,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Feedback bundle not found: {bundle_name}",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        )


@router.get(
    "/feedback/bundles/latest",
    response_model=FeedbackBundleInfo,
    summary="Get the most recent feedback weight bundle",
    description=(
        "Returns the latest bundle's stats and preview data (histogram, top symbols). "
        "Returns 404 if no bundles exist yet."
    ),
)
@limiter.limit("30/minute")
async def get_latest_feedback_bundle(
    request: Request,
    user_id: AdminUserID,
) -> FeedbackBundleInfo:
    from app.ml.training.feedback_loader import get_latest_bundle_stats

    stats = await asyncio.to_thread(get_latest_bundle_stats)
    if stats is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No feedback bundles found. Build one first via POST /feedback/build.",
        )
    return _stats_to_bundle_info(stats)
