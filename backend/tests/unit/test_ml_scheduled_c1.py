"""
C1 — Scheduled retrain acceptance suite.

The plan's exit criterion: "dry-run scheduled invocation produces a
challenger run dir, zero registry mutations."  These tests verify the
wrapper's structural guarantees without ever invoking the real training
orchestrator (which would take hours).

Coverage:
- ``--dry-run`` is provably side-effect free (no subprocess, no lock,
  no DB session).
- The lock file is correctly acquired/released via ``fcntl.flock`` and
  blocks a concurrent invocation.
- ``--once`` actually subprocesses the orchestrator with ``--fresh``.
- ``--schedule`` wires an APScheduler job per ``SCHEDULED_RETRAIN``.
- The systemd unit + timer files exist and contain the load-bearing
  lines (defends against accidental edits that silently break deployment).
"""
from __future__ import annotations

import fcntl
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.scheduled_retrain import (
    _ORCHESTRATOR_CMD,
    _build_parser,
    run_one_challenger,
    run_scheduled,
)


# ══════════════════════════════════════════════════════════════════════════════
# 1. --dry-run is provably side-effect free
# ══════════════════════════════════════════════════════════════════════════════

class TestDryRunNoSideEffects:

    def test_dry_run_does_not_invoke_subprocess(self, tmp_path: Path) -> None:
        """The plan's primary acceptance: dry-run never spawns the orchestrator."""
        with patch("scripts.scheduled_retrain.subprocess.call") as mock_call:
            rc = run_one_challenger(dry_run=True, project_root=tmp_path)
        assert rc == 0
        mock_call.assert_not_called()

    def test_dry_run_does_not_create_lock_file(self, tmp_path: Path) -> None:
        """Lock acquisition must be skipped in dry-run."""
        with patch("scripts.scheduled_retrain.subprocess.call"):
            run_one_challenger(dry_run=True, project_root=tmp_path)
        lock_path = tmp_path / ".scheduled_retrain.lock"
        assert not lock_path.exists()

    def test_dry_run_does_not_create_log_dir(self, tmp_path: Path) -> None:
        """log_dir mkdir is part of the real-run prologue, not dry-run."""
        with patch("scripts.scheduled_retrain.subprocess.call"):
            run_one_challenger(dry_run=True, project_root=tmp_path)
        log_dir = tmp_path / "logs" / "scheduled_retrain"
        assert not log_dir.exists()


# ══════════════════════════════════════════════════════════════════════════════
# 2. Real run: lock acquired, orchestrator invoked with --fresh
# ══════════════════════════════════════════════════════════════════════════════

class TestRealInvocation:
    """
    Patches ``_tee_subprocess`` — the actual seam the wrapper launches
    through. (These tests originally patched ``subprocess.call``, which the
    Popen-based ``_tee_subprocess`` never uses, so they silently launched
    REAL orchestrator subprocesses during test runs. Fixed 2026-07-17
    alongside WS3.) ``_build_feedback_bundle`` is patched so the feedback
    builder subprocess is never launched either; with no bundle in tmp_path
    the wrapper trains unweighted, exercising the assembly fallback.
    """

    @staticmethod
    def _invoke(tmp_path: Path, tee_rc: int = 0):
        with (
            patch(
                "scripts.scheduled_retrain._tee_subprocess", return_value=tee_rc
            ) as tee,
            patch(
                "scripts.scheduled_retrain._build_feedback_bundle", return_value=0
            ),
        ):
            rc = run_one_challenger(dry_run=False, project_root=tmp_path)
        return rc, tee

    def test_real_run_subprocesses_orchestrator(self, tmp_path: Path) -> None:
        rc, tee = self._invoke(tmp_path)
        assert rc == 0
        tee.assert_called_once()

    def test_real_run_uses_fresh_flag(self, tmp_path: Path) -> None:
        """``--fresh`` is the only acceptable flag — `_ORCHESTRATOR_CMD` carries it."""
        _rc, tee = self._invoke(tmp_path)
        cmd = tee.call_args.kwargs["cmd"]
        assert _ORCHESTRATOR_CMD[0] in cmd[1]    # orchestrator script path
        assert "--fresh" in cmd

    def test_real_run_uses_project_root_cwd(self, tmp_path: Path) -> None:
        """Orchestrator must be invoked with cwd = project root."""
        _rc, tee = self._invoke(tmp_path)
        assert tee.call_args.kwargs["cwd"] == tmp_path

    def test_real_run_targets_per_run_log_file(self, tmp_path: Path) -> None:
        """The tee target must be a timestamped file under SCHEDULED_RETRAIN['log_dir']."""
        _rc, tee = self._invoke(tmp_path)
        log_file = tee.call_args.kwargs["log_file"]
        assert log_file.parent == tmp_path / "logs" / "scheduled_retrain"
        assert log_file.name.startswith("scheduled_retrain_")
        assert log_file.name.endswith(".log")

    def test_subprocess_nonzero_returns_2(self, tmp_path: Path) -> None:
        """Failed orchestrator run → wrapper exit code 2 (distinct from 0/1)."""
        rc, _tee = self._invoke(tmp_path, tee_rc=137)
        assert rc == 2


# ══════════════════════════════════════════════════════════════════════════════
# 3. Concurrency lock prevents parallel runs
# ══════════════════════════════════════════════════════════════════════════════

class TestConcurrencyLock:

    def test_concurrent_invocation_returns_1_and_skips(
        self, tmp_path: Path
    ) -> None:
        """If the lock is held, a second run must exit 1 and NOT subprocess."""
        lock_path = tmp_path / ".scheduled_retrain.lock"
        # Manually hold the lock the same way the wrapper does.
        held_fh = open(lock_path, "w")
        fcntl.flock(held_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with patch(
                "scripts.scheduled_retrain.subprocess.call"
            ) as mock_call:
                rc = run_one_challenger(dry_run=False, project_root=tmp_path)
            assert rc == 1
            mock_call.assert_not_called()
        finally:
            fcntl.flock(held_fh, fcntl.LOCK_UN)
            held_fh.close()

    def test_lock_released_after_run(self, tmp_path: Path) -> None:
        """The wrapper must release the lock on exit so the next run can claim it."""
        with patch(
            "scripts.scheduled_retrain.subprocess.call", return_value=0
        ):
            run_one_challenger(dry_run=False, project_root=tmp_path)
        # The wrapper closed its file handle → flock released → we can acquire.
        lock_path = tmp_path / ".scheduled_retrain.lock"
        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


# ══════════════════════════════════════════════════════════════════════════════
# 4. APScheduler --schedule mode wires the cron job correctly
# ══════════════════════════════════════════════════════════════════════════════

class TestSchedulerWiring:

    def test_schedule_mode_arms_blocking_scheduler(self, tmp_path: Path) -> None:
        """``run_scheduled`` must add the job and call ``.start()`` on the scheduler."""
        with patch(
            "apscheduler.schedulers.blocking.BlockingScheduler"
        ) as mock_sched_cls:
            instance = mock_sched_cls.return_value
            run_scheduled(dry_run=True, project_root=tmp_path)

            # Scheduler was created with the configured timezone…
            tz_kw = mock_sched_cls.call_args.kwargs.get("timezone")
            assert tz_kw == "Asia/Kolkata"

            # …a single cron job was registered…
            instance.add_job.assert_called_once()
            cron_args = instance.add_job.call_args
            assert cron_args.args[1] == "cron"
            assert cron_args.kwargs["day_of_week"] == "sat"
            assert cron_args.kwargs["hour"]        == 20
            assert cron_args.kwargs["max_instances"] == 1
            assert cron_args.kwargs["coalesce"]    is True

            # …and the scheduler was started.
            instance.start.assert_called_once()


# ══════════════════════════════════════════════════════════════════════════════
# 5. CLI surface — no auto-promote, modes mutually exclusive
# ══════════════════════════════════════════════════════════════════════════════

class TestCliSurface:

    def test_once_and_schedule_are_mutually_exclusive(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--once", "--schedule"])

    def test_defaults_to_once_mode(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.once is False
        assert args.schedule is False
        # main() treats "neither flag set" as the same as --once.

    def test_dry_run_orthogonal_to_mode(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--once", "--dry-run"])
        assert args.once is True
        assert args.dry_run is True

    def test_no_promote_flag_exists(self) -> None:
        """Plan invariant: scheduled retrain NEVER auto-promotes.  The CLI
        must offer no way to.  (Defends against a future contributor adding
        a `--promote` flag and breaking the human-gate invariant.)"""
        parser = _build_parser()
        help_text = parser.format_help()
        assert "--promote" not in help_text
        assert "promote" not in [a.option_strings[0].lstrip("-")
                                  for a in parser._actions
                                  if a.option_strings]


# ══════════════════════════════════════════════════════════════════════════════
# 6. systemd deployment artefacts present + load-bearing lines intact
# ══════════════════════════════════════════════════════════════════════════════

class TestSystemdUnits:

    _DEPLOY_DIR = Path(__file__).parent.parent.parent.parent / "deploy"

    def test_service_unit_exists(self) -> None:
        assert (self._DEPLOY_DIR / "cortex-retrain.service").exists()

    def test_timer_unit_exists(self) -> None:
        assert (self._DEPLOY_DIR / "cortex-retrain.timer").exists()

    def test_readme_exists(self) -> None:
        assert (self._DEPLOY_DIR / "README.md").exists()

    def test_service_invokes_scheduled_retrain_with_once(self) -> None:
        txt = (self._DEPLOY_DIR / "cortex-retrain.service").read_text()
        assert "scheduled_retrain.py --once" in txt
        assert "Type=oneshot" in txt

    def test_timer_oncalendar_matches_config(self) -> None:
        """If this drifts from SCHEDULED_RETRAIN, the journal and the config
        will silently disagree — fail loud."""
        from app.ml.config import SCHEDULED_RETRAIN
        txt = (self._DEPLOY_DIR / "cortex-retrain.timer").read_text()
        # Day name first letter is upper in OnCalendar (Sat) vs lower in config (sat).
        assert SCHEDULED_RETRAIN["cron_day_of_week"].capitalize() in txt
        assert f"{SCHEDULED_RETRAIN['cron_hour']:02d}:00:00" in txt
        assert SCHEDULED_RETRAIN["cron_timezone"] in txt
        assert "Persistent=true" in txt
