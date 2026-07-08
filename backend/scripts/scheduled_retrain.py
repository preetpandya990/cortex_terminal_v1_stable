"""
C1 — Scheduled retrain wrapper.

Hands-off generator of challenger training runs.  Wraps (never replaces) the
existing checkpointed `ProductionTrainingOrchestrator` and invokes it on a
fixed off-market cadence.  Produces a challenger run dir + log artefact;
**never promotes** — an operator reviews the run and promotes manually via
`scripts/promote_model.py`.  This is the C2 hand-off boundary.

Two scheduling modes share the same in-process core (`run_one_challenger`):

  --once       Run one challenger immediately and exit.  This is the
               systemd-timer entrypoint (see deploy/cortex-retrain.timer).

  --schedule   Start an APScheduler `BlockingScheduler` and run forever,
               firing per the cron expression in `SCHEDULED_RETRAIN`.
               Intended for environments without systemd (e.g. macOS dev).

  --dry-run    Print the plan + paths and exit without invoking the
               orchestrator subprocess or acquiring the lock.

A `fcntl.flock` on the configured lock file makes concurrent runs impossible
(atomic at the OS level, auto-released on process exit).  If the lock is held
by another process the second invocation logs a warning and exits non-zero —
the orchestrator is never invoked twice in parallel.

The lock file is runtime state and is deliberately .gitignore'd, never
committed: flock is scoped to the file's inode, so correctness depends on all
participants opening the same on-disk file — git managing (and potentially
recreating) it would both churn the working tree on every run and invite
inode swaps.  The file's pid/started_at content is informational only; the
held flock is the actual mutual-exclusion mechanism.

Exit codes:
    0 — challenger run completed successfully (or dry-run printed plan)
    1 — lock held (concurrent invocation); orchestrator NOT invoked
    2 — orchestrator subprocess returned non-zero (see per-run log file)
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make ``app.*`` importable when this script is run directly (matches the
# convention used by every other script in backend/scripts/).
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.ml.config import SCHEDULED_RETRAIN  # noqa: E402

LOG = logging.getLogger("scheduled_retrain")

# Subprocess command to invoke the checkpointed orchestrator with a fresh run.
# `--fresh` forces a new run dir (no resume) so each scheduled invocation
# produces a clean challenger independent of any abandoned in-progress state.
_ORCHESTRATOR_CMD: tuple[str, ...] = (
    "scripts/production_training_orchestrator.py",
    "--fresh",
)


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tee_subprocess(cmd: list[str], cwd: Path, log_file: Path) -> int:
    """
    Run *cmd* as a subprocess, streaming stdout + stderr simultaneously to
    both *log_file* and the calling process's stdout in real time.

    Design invariants
    -----------------
    Buffering
        ``PYTHONUNBUFFERED=1`` is injected into the subprocess environment so
        Python's internal block-buffering does not hold lines in a 64 KB chunk
        before flushing them through the pipe.  ``bufsize=1`` (line-buffered)
        on the Popen reader ensures the same on the Python side of the pipe.

    Dual-write reliability
        Each line is written and flushed to *log_file* before the terminal
        write is attempted.  A terminal write failure (``OSError`` — e.g. the
        operator detached the session via ``nohup``) is suppressed silently; the
        log file continues to receive every line and remains the authoritative
        record.

    Interrupt handling
        Ctrl-C delivers ``SIGINT`` to the foreground process group, so both
        the wrapper and the orchestrator subprocess receive it simultaneously.
        The ``except KeyboardInterrupt`` block drains any lines the subprocess
        emits during its shutdown sequence into *log_file*, then re-raises so
        the caller can release the file lock cleanly.

    Returns:
        Subprocess exit code (0 = success, non-zero = failure).
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    with open(log_file, "w", encoding="utf-8", buffering=1) as logf:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        try:
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
                try:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                except OSError:
                    pass  # terminal closed; log file keeps receiving every line
        except KeyboardInterrupt:
            # Drain shutdown output into the log (operator pressed Ctrl-C;
            # no need to mirror those lines back to a terminal being closed).
            for line in proc.stdout:
                logf.write(line)
                logf.flush()
            raise
        finally:
            proc.stdout.close()
            proc.wait()

    return proc.returncode


def _resolve_project_root() -> Path:
    """`backend/` directory — script runs from `backend/` per convention."""
    return Path(__file__).parent.parent


def run_one_challenger(
    *,
    dry_run:      bool = False,
    project_root: Path | None = None,
) -> int:
    """Acquire the lock, subprocess-invoke the orchestrator, return its exit code.

    Returns:
        0 on success (or dry-run);
        1 when the lock is held by another process (skipped);
        2 when the orchestrator subprocess returned non-zero.

    The lock is held via ``fcntl.flock(LOCK_EX | LOCK_NB)`` so a concurrent
    invocation gets ``BlockingIOError`` instantly — the OS releases the
    lock when this process exits (clean or crashed) so no stale locks.
    """
    project_root = project_root or _resolve_project_root()
    lock_path = project_root / SCHEDULED_RETRAIN["lock_file"]
    log_dir   = project_root / SCHEDULED_RETRAIN["log_dir"]

    LOG.info(
        "Scheduled retrain  cadence=%s %02d:00 %s  window=%s  lookback=%dy",
        SCHEDULED_RETRAIN["cron_day_of_week"],
        SCHEDULED_RETRAIN["cron_hour"],
        SCHEDULED_RETRAIN["cron_timezone"],
        SCHEDULED_RETRAIN["window_mode"],
        SCHEDULED_RETRAIN["lookback_years"],
    )
    LOG.info("lock_file: %s", lock_path)
    LOG.info("log_dir:   %s", log_dir)

    if dry_run:
        LOG.info("[DRY-RUN] would acquire lock then exec: %s", " ".join(_ORCHESTRATOR_CMD))
        LOG.info("[DRY-RUN] no subprocess invoked; lock NOT acquired; no state mutated.")
        return 0

    # ── Acquire the lock (fcntl is POSIX-atomic, auto-released on exit) ───────
    lock_fh = open(lock_path, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        LOG.warning(
            "Lock %s held by another process — previous retrain still running.  "
            "Skipping this invocation.",
            lock_path,
        )
        lock_fh.close()
        return 1

    lock_fh.write(
        f"pid={os.getpid()}\n"
        f"started_at={datetime.now(timezone.utc).isoformat()}\n"
    )
    lock_fh.flush()

    # ── Invoke the orchestrator subprocess with per-run log capture ───────────
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"scheduled_retrain_{_utc_stamp()}.log"
    LOG.info("Per-run log: %s", log_file)
    LOG.info(
        "Launching: %s  (cwd=%s)",
        " ".join(_ORCHESTRATOR_CMD), project_root,
    )
    LOG.info("Orchestrator output streamed live below ↓")

    try:
        rc = _tee_subprocess(
            cmd=[sys.executable, *_ORCHESTRATOR_CMD],
            cwd=project_root,
            log_file=log_file,
        )
    finally:
        # Lock auto-releases on lock_fh.close() (fcntl semantics).
        lock_fh.close()

    if rc != 0:
        LOG.error("Challenger run FAILED  exit_code=%d  see %s", rc, log_file)
        return 2
    LOG.info("Challenger run completed successfully  exit_code=0")
    return 0


def run_scheduled(*, dry_run: bool, project_root: Path | None = None) -> None:
    """Long-running APScheduler `BlockingScheduler` — alternative to systemd.

    Fires per the cron expression in ``SCHEDULED_RETRAIN``.  Use this when
    the host has no systemd (macOS dev box, Docker entrypoint, etc.).
    """
    from apscheduler.schedulers.blocking import BlockingScheduler

    project_root = project_root or _resolve_project_root()
    sched = BlockingScheduler(timezone=SCHEDULED_RETRAIN["cron_timezone"])
    sched.add_job(
        lambda: run_one_challenger(dry_run=dry_run, project_root=project_root),
        "cron",
        day_of_week=SCHEDULED_RETRAIN["cron_day_of_week"],
        hour=SCHEDULED_RETRAIN["cron_hour"],
        id="cortex_retrain",
        coalesce=True,        # if multiple fires queued (system was off), run once
        max_instances=1,      # belt-and-braces with the file lock
    )
    LOG.info(
        "Scheduler armed: cron day_of_week=%s hour=%02d tz=%s.  Ctrl-C to exit.",
        SCHEDULED_RETRAIN["cron_day_of_week"],
        SCHEDULED_RETRAIN["cron_hour"],
        SCHEDULED_RETRAIN["cron_timezone"],
    )
    sched.start()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="C1 — Scheduled retrain wrapper for the production orchestrator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modes (mutually exclusive — defaults to --once):\n"
            "  --once      Run one challenger immediately and exit "
            "(systemd-timer entrypoint).\n"
            "  --schedule  BlockingScheduler — runs forever on the configured cron.\n"
            "  --dry-run   Print plan, no subprocess invocation, no lock acquired.\n"
            "\n"
            "This wrapper NEVER promotes.  Operator reviews the challenger run "
            "via `scripts/promote_model.py status` and promotes manually.\n"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run one challenger now and exit (default mode; systemd-timer entrypoint).",
    )
    mode.add_argument(
        "--schedule",
        action="store_true",
        help="Long-running APScheduler BlockingScheduler (fallback for non-systemd hosts).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan + paths; no subprocess invocation; no lock acquired.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    )

    project_root = _resolve_project_root()

    if args.schedule:
        run_scheduled(dry_run=args.dry_run, project_root=project_root)
        return 0  # only reached on Ctrl-C
    # default mode = --once
    return run_one_challenger(dry_run=args.dry_run, project_root=project_root)


if __name__ == "__main__":
    sys.exit(main())
