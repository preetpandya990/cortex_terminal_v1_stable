"""
WS3 — feedback weights in the scheduled retrain path.

Verifies ``_select_newest_bundle`` (orphan rejection, staleness guard,
newest-wins) and the command assembly in ``_assemble_orchestrator_cmd``
(builder failure → unweighted; usable bundle → --feedback-weights; disabled
by config → untouched base command; builder crash can never fail the run).
"""

import os
import time
from pathlib import Path
from unittest.mock import patch

from app.ml.config import SCHEDULED_RETRAIN
from scripts.scheduled_retrain import (
    _ORCHESTRATOR_CMD,
    _assemble_orchestrator_cmd,
    _select_newest_bundle,
)

MODULE = "scripts.scheduled_retrain"


def _make_bundle(dir_: Path, stamp: str, *, sidecar: bool = True, age_days: float = 0.0) -> Path:
    parquet = dir_ / f"feedback_weights_{stamp}.parquet"
    parquet.write_bytes(b"parquet")
    if sidecar:
        (dir_ / f"feedback_weights_{stamp}.meta.json").write_text("{}")
    if age_days:
        mtime = time.time() - age_days * 86400
        os.utime(parquet, (mtime, mtime))
    return parquet


# ─── _select_newest_bundle ───────────────────────────────────────────────────

class TestSelectNewestBundle:
    def test_newest_complete_bundle_wins(self, tmp_path):
        _make_bundle(tmp_path, "20260710T000000Z")
        newest = _make_bundle(tmp_path, "20260717T000000Z")
        assert _select_newest_bundle(tmp_path) == newest

    def test_orphan_parquet_rejected_falls_back_to_older_complete(self, tmp_path):
        older = _make_bundle(tmp_path, "20260716T000000Z")
        _make_bundle(tmp_path, "20260717T000000Z", sidecar=False)  # orphan
        assert _select_newest_bundle(tmp_path) == older

    def test_stale_newest_returns_none_not_an_older_bundle(self, tmp_path):
        """Staleness must not fall back — anything older is staler still."""
        _make_bundle(tmp_path, "20260701T000000Z", age_days=16)
        _make_bundle(tmp_path, "20260709T000000Z", age_days=8)
        assert _select_newest_bundle(tmp_path, max_age_days=7) is None

    def test_fresh_bundle_within_max_age_accepted(self, tmp_path):
        fresh = _make_bundle(tmp_path, "20260717T000000Z", age_days=2)
        assert _select_newest_bundle(tmp_path, max_age_days=7) == fresh

    def test_empty_and_missing_dirs_return_none(self, tmp_path):
        assert _select_newest_bundle(tmp_path) is None
        assert _select_newest_bundle(tmp_path / "nope") is None


# ─── Command assembly ────────────────────────────────────────────────────────

class TestCommandAssembly:
    def _project(self, tmp_path: Path) -> tuple[Path, Path]:
        (tmp_path / "feedback_bundles").mkdir()
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        return tmp_path, log_dir

    def test_usable_bundle_appends_feedback_weights_flag(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        bundle = _make_bundle(root / "feedback_bundles", "20260717T120000Z")

        with patch(f"{MODULE}._build_feedback_bundle", return_value=0) as builder:
            cmd = _assemble_orchestrator_cmd(root, log_dir)

        builder.assert_called_once()
        assert cmd[: len(_ORCHESTRATOR_CMD)] == list(_ORCHESTRATOR_CMD)
        assert cmd[-2:] == ["--feedback-weights", str(bundle)]

    def test_builder_failure_still_uses_existing_bundle(self, tmp_path):
        """Exit code 1/2 from the builder → WARN, but a pre-existing fresh
        bundle is still used (plan: select newest regardless of exit code)."""
        root, log_dir = self._project(tmp_path)
        bundle = _make_bundle(root / "feedback_bundles", "20260716T000000Z", age_days=1)

        with patch(f"{MODULE}._build_feedback_bundle", return_value=2):
            cmd = _assemble_orchestrator_cmd(root, log_dir)

        assert cmd[-2:] == ["--feedback-weights", str(bundle)]

    def test_no_bundle_trains_unweighted(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        with (
            patch.dict(SCHEDULED_RETRAIN, {"model_version_override": None}),
            patch(f"{MODULE}._build_feedback_bundle", return_value=1),
        ):
            cmd = _assemble_orchestrator_cmd(root, log_dir)
        assert cmd == list(_ORCHESTRATOR_CMD)
        assert "--feedback-weights" not in cmd

    def test_disabled_by_config_skips_builder_entirely(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        _make_bundle(root / "feedback_bundles", "20260717T120000Z")
        with (
            patch.dict(SCHEDULED_RETRAIN, {"enable_feedback_weights": False, "model_version_override": None}),
            patch(f"{MODULE}._build_feedback_bundle") as builder,
        ):
            cmd = _assemble_orchestrator_cmd(root, log_dir)
        builder.assert_not_called()
        assert cmd == list(_ORCHESTRATOR_CMD)

    def test_dry_run_never_invokes_builder(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        with (
            patch.dict(SCHEDULED_RETRAIN, {"model_version_override": None}),
            patch(f"{MODULE}._build_feedback_bundle") as builder,
        ):
            cmd = _assemble_orchestrator_cmd(root, log_dir, dry_run=True)
        builder.assert_not_called()
        assert cmd == list(_ORCHESTRATOR_CMD)

    def test_base_command_is_never_mutated(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        _make_bundle(root / "feedback_bundles", "20260717T120000Z")
        with patch(f"{MODULE}._build_feedback_bundle", return_value=0):
            _assemble_orchestrator_cmd(root, log_dir)
        assert _ORCHESTRATOR_CMD == ("scripts/production_training_orchestrator.py", "--fresh")

    def test_model_version_pin_appended_when_configured(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        with (
            patch.dict(SCHEDULED_RETRAIN, {"model_version_override": "1.2.0"}),
            patch(f"{MODULE}._build_feedback_bundle", return_value=1),
        ):
            cmd = _assemble_orchestrator_cmd(root, log_dir)
        idx = cmd.index("--model-version")
        assert cmd[idx + 1] == "1.2.0"

    def test_no_pin_flag_when_override_is_none(self, tmp_path):
        root, log_dir = self._project(tmp_path)
        with (
            patch.dict(SCHEDULED_RETRAIN, {"model_version_override": None}),
            patch(f"{MODULE}._build_feedback_bundle", return_value=1),
        ):
            cmd = _assemble_orchestrator_cmd(root, log_dir)
        assert "--model-version" not in cmd


# ─── Orchestrator version-pin resolution ─────────────────────────────────────

class TestModelVersionPinResolution:
    @staticmethod
    async def _resolve(registry_versions, requested, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        from scripts.production_training_orchestrator import _resolve_model_version

        session = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = registry_versions
        session.execute.return_value = result
        return await _resolve_model_version(
            session, tmp_path, fresh=True, requested=requested
        )

    async def test_pin_honored_when_unregistered(self, tmp_path):
        got = await self._resolve(["1.1.2_xgboost", "1.1.3_gru"], "1.2.0", tmp_path)
        assert got == "1.2.0"

    async def test_taken_pin_self_heals_to_auto_increment(self, tmp_path):
        got = await self._resolve(
            ["1.1.3_gru", "1.2.0_xgboost", "1.2.0_gru"], "1.2.0", tmp_path
        )
        assert got == "1.2.1"  # highest 1.2.0 → patch bump, pin ignored

    async def test_no_pin_auto_increments_patch(self, tmp_path):
        got = await self._resolve(["1.1.2_xgboost", "1.1.3_gru"], None, tmp_path)
        assert got == "1.1.4"

    async def test_invalid_pin_raises(self, tmp_path):
        import pytest as _pytest
        with _pytest.raises(ValueError, match="not a valid semver"):
            await self._resolve(["1.1.3_gru"], "v2-latest", tmp_path)
