"""
A8 — Registry Consolidation acceptance suite.

Validates that:

  1. The orphan `app/ai/governance/model_registry.py` no longer exists and is
     not imported anywhere in the codebase (single-authority grep-guard).

  2. ModelRegistry.promote_to_production() raises RegistryDeprecatedError —
     the ungated bypass path is closed.

  3. ModelRegistry.rollback_model() raises RegistryDeprecatedError.

  4. UnifiedModelRegistry.promote_model() raises RegistryDeprecatedError.

  5. UnifiedModelRegistry.demote_model() raises RegistryDeprecatedError.

  6. ModelPromoter.promote_to_production() calls _project_to_ai_ml_models
     *before* the commit (atomic dual-write): if the commit fails, both writes
     are rolled back together.

  7. State mapping is correct: production→live, staging→paper, development→shadow.

  8. DriftDetector._handle_drift_action() writes an advisory flag
     (governance_metadata["challenger_recommended"] = True + drift_recommendation)
     and does NOT mutate deployment_state.

  9. RegistryDeprecatedError is a RuntimeError subclass with a clear message.

 10. promote_to_staging() also projects atomically.

Tests are RED on main@HEAD, GREEN after A8.
"""
from __future__ import annotations

import ast
import importlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest


# ══════════════════════════════════════════════════════════════════════════════
# 1. Single-authority grep-guard
# ══════════════════════════════════════════════════════════════════════════════

class TestSingleAuthority:
    """The orphan governance registry file must not exist or be importable."""

    _DELETED_PATH = (
        Path(__file__).parent.parent.parent
        / "app" / "ai" / "governance" / "model_registry.py"
    )
    _BACKEND_ROOT = Path(__file__).parent.parent.parent

    def test_orphan_file_deleted(self) -> None:
        assert not self._DELETED_PATH.exists(), (
            f"{self._DELETED_PATH} still exists — it must be deleted in A8"
        )

    def test_no_import_of_orphan_module(self) -> None:
        """AST-based static-import check: the deleted module must not be imported.

        Substring grep is the wrong tool for parsing Python source — it cannot
        distinguish a real import from a string literal (this test file mentions
        the module path as data and would self-flag under grep). AST parsing
        walks the parsed tree and only inspects actual ``Import`` / ``ImportFrom``
        nodes, so it is both robust and semantically correct.

        Detected forms:
            import app.ai.governance.model_registry [as Y]
            from   app.ai.governance.model_registry import X
            from   app.ai.governance import model_registry [as Y]

        A relative import from inside ``app/ai/governance/`` (``from .model_registry
        import X``) would raise ``ImportError`` at module load — a louder failure
        than this static check would catch — so it is intentionally not covered.
        """
        _PARENT = "app.ai.governance"
        _ORPHAN = "model_registry"
        _FQN    = f"{_PARENT}.{_ORPHAN}"

        offenders: list[str] = []
        for py in self._BACKEND_ROOT.rglob("*.py"):
            if "__pycache__" in py.parts:
                continue
            text = py.read_text(errors="replace")
            # Fast pre-filter — every detected form contains ``model_registry``
            # as a substring, so files lacking it cannot possibly be importers
            # and skip the (pytest-instrumented and therefore expensive) AST
            # parse entirely. Shrinks the candidate set ~20×.
            if _ORPHAN not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(py))
            except SyntaxError:
                # An unparseable .py file is a separate failure mode (tooling),
                # not an A8 violation. Skip so this check stays single-purpose.
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    if any(alias.name == _FQN for alias in node.names):
                        offenders.append(str(py.relative_to(self._BACKEND_ROOT)))
                        break
                elif isinstance(node, ast.ImportFrom):
                    if node.level == 0 and node.module == _FQN:
                        offenders.append(str(py.relative_to(self._BACKEND_ROOT)))
                        break
                    if (
                        node.level == 0
                        and node.module == _PARENT
                        and any(alias.name == _ORPHAN for alias in node.names)
                    ):
                        offenders.append(str(py.relative_to(self._BACKEND_ROOT)))
                        break

        assert not offenders, (
            f"These files still import the deleted A8 registry: {offenders}. "
            "Route through ModelPromoter (app.ml.model_registry) instead."
        )

    def test_module_not_importable(self) -> None:
        with pytest.raises((ImportError, ModuleNotFoundError)):
            importlib.import_module("app.ai.governance.model_registry")


# ══════════════════════════════════════════════════════════════════════════════
# 2 & 3. ModelRegistry deprecated promotion methods
# ══════════════════════════════════════════════════════════════════════════════

class TestModelRegistryDeprecated:
    """ModelRegistry storage methods are unchanged; promotion methods are retired."""

    def _make_registry(self, tmp_path: Path) -> Any:
        from app.ml.model_registry import ModelRegistry
        db = AsyncMock()
        return ModelRegistry(session=db, model_storage_path=tmp_path)

    @pytest.mark.asyncio
    async def test_promote_to_production_raises(self, tmp_path: Path) -> None:
        from app.ml.model_registry import RegistryDeprecatedError
        registry = self._make_registry(tmp_path)
        with pytest.raises(RegistryDeprecatedError) as exc_info:
            await registry.promote_to_production("1.0.0_xgboost")
        assert "ModelPromoter" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_rollback_model_raises(self, tmp_path: Path) -> None:
        from app.ml.model_registry import RegistryDeprecatedError
        registry = self._make_registry(tmp_path)
        with pytest.raises(RegistryDeprecatedError) as exc_info:
            await registry.rollback_model()
        assert "ModelPromoter" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_register_model_still_works(self, tmp_path: Path) -> None:
        """Registration (non-promotion) must be unaffected by A8."""
        from app.ml.model_registry import ModelRegistry
        # We only test the method exists and is callable; actual DB/filesystem
        # interaction is integration-tested elsewhere.
        assert hasattr(ModelRegistry, "register_model")
        assert callable(ModelRegistry.register_model)


# ══════════════════════════════════════════════════════════════════════════════
# 4 & 5. UnifiedModelRegistry deprecated mutation methods
# ══════════════════════════════════════════════════════════════════════════════

class TestUnifiedModelRegistryDeprecated:

    @pytest.mark.asyncio
    async def test_promote_model_raises(self) -> None:
        from app.ai.governance.unified_model_registry import UnifiedModelRegistry
        from app.ml.model_registry import RegistryDeprecatedError
        registry = UnifiedModelRegistry()
        with pytest.raises(RegistryDeprecatedError) as exc_info:
            await registry.promote_model(
                db=AsyncMock(), pubsub=AsyncMock(),
                model_name="cortex_xgboost_1d", target_state="live",
            )
        msg = str(exc_info.value)
        assert "deprecated" in msg.lower()
        assert "ModelPromoter" in msg

    @pytest.mark.asyncio
    async def test_demote_model_raises(self) -> None:
        from app.ai.governance.unified_model_registry import UnifiedModelRegistry
        from app.ml.model_registry import RegistryDeprecatedError
        registry = UnifiedModelRegistry()
        with pytest.raises(RegistryDeprecatedError) as exc_info:
            await registry.demote_model(
                db=AsyncMock(), pubsub=AsyncMock(),
                model_name="cortex_xgboost_1d", reason="test",
            )
        assert "deprecated" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_read_methods_still_work(self) -> None:
        """Query/read methods are unaffected — governance API reads from the projection."""
        from app.ai.governance.unified_model_registry import UnifiedModelRegistry
        registry = UnifiedModelRegistry()
        # These methods must exist and be callable (not deprecated).
        assert callable(registry.get_active_models)
        assert callable(registry.get_model)
        assert callable(registry.register_model)


# ══════════════════════════════════════════════════════════════════════════════
# 6. Atomic dual-write — induced commit failure rolls back both tables
# ══════════════════════════════════════════════════════════════════════════════

class TestAtomicDualWrite:
    """
    If session.commit() raises after both table writes are staged,
    the rollback must cover BOTH ml_model_metadata and ai_ml_models.

    We verify this by confirming that _project_to_ai_ml_models is called
    BEFORE commit, and that rollback() is called when commit raises.
    """

    def _make_staging_model(self) -> MagicMock:
        m = MagicMock()
        m.model_version = "1.0.0_xgboost"
        m.model_name    = "xgboost"
        m.status        = "staging"
        m.is_active     = False
        m.deployed_at   = None
        m.updated_at    = None
        return m

    @pytest.mark.asyncio
    async def test_project_called_before_commit(self) -> None:
        """_project_to_ai_ml_models must be awaited before session.commit()."""
        from app.ml.model_registry import ModelPromoter, QualityGate

        call_order: list[str] = []

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_staging_model())
        )

        async def _track_commit() -> None:
            call_order.append("commit")

        session.commit.side_effect = _track_commit

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        promoter = ModelPromoter(session, quality_gate=gate)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
        ) as mock_project:
            async def _track_project(*args: Any, **kwargs: Any) -> int:
                call_order.append("project")
                return 1
            mock_project.side_effect = _track_project

            await promoter.promote_to_production("1.0.0_xgboost", "xgboost")

        # project must come before commit
        assert call_order.index("project") < call_order.index("commit"), (
            f"Expected project before commit, got order: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_commit_failure_triggers_rollback(self) -> None:
        """If commit raises, rollback() must be called (both writes discarded)."""
        from app.ml.model_registry import ModelPromoter, QualityGate

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_staging_model())
        )
        session.commit.side_effect = RuntimeError("DB connection lost")

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        promoter = ModelPromoter(session, quality_gate=gate)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ):
            with pytest.raises(RuntimeError, match="DB connection lost"):
                await promoter.promote_to_production("1.0.0_xgboost", "xgboost")

        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_project_failure_triggers_rollback(self) -> None:
        """If _project_to_ai_ml_models raises, rollback() must be called."""
        from app.ml.model_registry import ModelPromoter, QualityGate

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_staging_model())
        )

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        promoter = ModelPromoter(session, quality_gate=gate)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            side_effect=RuntimeError("governance table unreachable"),
        ):
            with pytest.raises(RuntimeError, match="governance table unreachable"):
                await promoter.promote_to_production("1.0.0_xgboost", "xgboost")

        session.rollback.assert_awaited_once()
        # commit must NOT have been called
        session.commit.assert_not_awaited()


# ══════════════════════════════════════════════════════════════════════════════
# 7. State mapping — production→live, staging→paper, development→shadow
# ══════════════════════════════════════════════════════════════════════════════

class TestStateMapping:
    def test_production_maps_to_live(self) -> None:
        from app.ml.model_registry import _ML_TO_GOVERNANCE_STATE
        assert _ML_TO_GOVERNANCE_STATE["production"] == "live"

    def test_staging_maps_to_paper(self) -> None:
        from app.ml.model_registry import _ML_TO_GOVERNANCE_STATE
        assert _ML_TO_GOVERNANCE_STATE["staging"] == "paper"

    def test_development_maps_to_shadow(self) -> None:
        from app.ml.model_registry import _ML_TO_GOVERNANCE_STATE
        assert _ML_TO_GOVERNANCE_STATE["development"] == "shadow"

    def test_all_ml_statuses_are_mapped(self) -> None:
        from app.ml.model_registry import _ML_TO_GOVERNANCE_STATE
        for ml_status in ("production", "staging", "development"):
            assert ml_status in _ML_TO_GOVERNANCE_STATE, (
                f"Missing mapping for ml_model_metadata status '{ml_status}'"
            )


# ══════════════════════════════════════════════════════════════════════════════
# 8. DriftDetector — advisory flag, no autonomous demotion
# ══════════════════════════════════════════════════════════════════════════════

class TestDriftAdvisoryFlag:
    """_handle_drift_action must write governance_metadata flags, not mutate state."""

    def _make_ai_model(self, state: str = "live") -> MagicMock:
        m                       = MagicMock()
        m.model_name            = "cortex_xgboost_1d"
        m.deployment_state      = state
        m.governance_metadata   = {}
        m.updated_at            = None
        return m

    @pytest.mark.asyncio
    async def test_does_not_mutate_deployment_state(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        # deployment_state must be unchanged
        assert model.deployment_state == "live"

    @pytest.mark.asyncio
    async def test_sets_challenger_recommended_flag(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")
        model.governance_metadata = {}

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        assert model.governance_metadata.get("challenger_recommended") is True

    @pytest.mark.asyncio
    async def test_writes_drift_recommendation_struct(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        rec = model.governance_metadata.get("drift_recommendation", {})
        assert rec["current_state"]     == "live"
        assert rec["recommended_state"] == "paper"
        assert rec["reason"]            == "drift_threshold_exceeded"
        assert "flagged_at" in rec

    @pytest.mark.asyncio
    async def test_returns_drift_flagged(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")

        detector = DriftDetector()
        action = await detector._handle_drift_action(db, model)

        assert action == "drift_flagged"

    @pytest.mark.asyncio
    async def test_paper_model_recommends_shadow(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="paper")

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        rec = model.governance_metadata["drift_recommendation"]
        assert rec["recommended_state"] == "shadow"

    @pytest.mark.asyncio
    async def test_existing_metadata_is_preserved(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")
        model.governance_metadata = {"existing_key": "existing_value"}

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        assert model.governance_metadata.get("existing_key") == "existing_value"

    @pytest.mark.asyncio
    async def test_commits_advisory_flag(self) -> None:
        from app.ai.governance.drift_detector import DriftDetector
        db    = AsyncMock()
        model = self._make_ai_model(state="live")

        detector = DriftDetector()
        await detector._handle_drift_action(db, model)

        db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════════════
# 9. RegistryDeprecatedError contract
# ══════════════════════════════════════════════════════════════════════════════

class TestRegistryDeprecatedError:
    def test_is_runtime_error_subclass(self) -> None:
        from app.ml.model_registry import RegistryDeprecatedError
        assert issubclass(RegistryDeprecatedError, RuntimeError)

    def test_message_is_preserved(self) -> None:
        from app.ml.model_registry import RegistryDeprecatedError
        err = RegistryDeprecatedError("use ModelPromoter")
        assert "ModelPromoter" in str(err)

    def test_can_be_caught_as_runtime_error(self) -> None:
        from app.ml.model_registry import RegistryDeprecatedError
        with pytest.raises(RuntimeError):
            raise RegistryDeprecatedError("test")


# ══════════════════════════════════════════════════════════════════════════════
# 10. promote_to_staging also projects atomically
# ══════════════════════════════════════════════════════════════════════════════

class TestStagingAtomicProjection:

    def _make_development_model(self) -> MagicMock:
        m = MagicMock()
        m.model_version = "1.0.0_xgboost"
        m.model_name    = "xgboost"
        m.status        = "development"
        m.updated_at    = None
        return m

    @pytest.mark.asyncio
    async def test_promote_to_staging_calls_project_before_commit(self) -> None:
        from app.ml.model_registry import ModelPromoter, QualityGate

        call_order: list[str] = []

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_development_model())
        )

        async def _track_commit() -> None:
            call_order.append("commit")

        session.commit.side_effect = _track_commit

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        promoter = ModelPromoter(session, quality_gate=gate)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
        ) as mock_project:
            async def _track_project(*args: Any, **kwargs: Any) -> int:
                call_order.append("project")
                return 1
            mock_project.side_effect = _track_project

            await promoter.promote_to_staging("1.0.0_xgboost")

        assert call_order.index("project") < call_order.index("commit")

    @pytest.mark.asyncio
    async def test_staging_projection_uses_correct_state(self) -> None:
        from app.ml.model_registry import ModelPromoter, QualityGate

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_development_model())
        )

        gate = MagicMock(spec=QualityGate)
        gate.validate.return_value = {"passed": True, "checks": {}}

        promoter = ModelPromoter(session, quality_gate=gate)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_project:
            await promoter.promote_to_staging("1.0.0_xgboost")

        # Must be called with ml_status="staging" → projects to "paper"
        _, call_args = mock_project.call_args[0], mock_project.call_args
        assert call_args[0][2] == "staging"  # positional: session, model, ml_status


# ══════════════════════════════════════════════════════════════════════════════
# 11. demote_to_staging — C3 honest re-eval support
# ══════════════════════════════════════════════════════════════════════════════
#
# `ModelPromoter.demote_to_staging` is the operator-applied counterpart to
# `promote_to_production`: production → staging in `ml_model_metadata`, live →
# paper in `ai_ml_models`, atomically in one transaction. Used by C3 when the
# honest re-evaluation finds a live model fails the current A6 hard gates and
# no qualified successor exists yet. Reason mandatory; audited at WARNING.

class TestDemoteToStaging:

    def _make_production_model(self) -> MagicMock:
        m = MagicMock()
        m.model_version = "1.0.0_xgboost"
        m.model_name    = "xgboost"
        m.status        = "production"
        m.is_active     = True
        m.updated_at    = None
        return m

    @pytest.mark.asyncio
    async def test_demote_calls_project_before_commit(self) -> None:
        """Atomic dual-write — _project_to_ai_ml_models must precede commit."""
        from app.ml.model_registry import ModelPromoter

        call_order: list[str] = []
        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_production_model())
        )

        async def _track_commit() -> None:
            call_order.append("commit")
        session.commit.side_effect = _track_commit

        promoter = ModelPromoter(session)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
        ) as mock_project:
            async def _track_project(*args: Any, **kwargs: Any) -> int:
                call_order.append("project")
                return 1
            mock_project.side_effect = _track_project

            await promoter.demote_to_staging(
                "1.0.0_xgboost",
                reason="C3 honest re-eval: missing A6 financial metrics",
            )

        assert call_order.index("project") < call_order.index("commit"), (
            f"Expected project before commit, got order: {call_order}"
        )

    @pytest.mark.asyncio
    async def test_demote_projects_with_staging_status(self) -> None:
        """The projection must use ml_status='staging' → governance 'paper'."""
        from app.ml.model_registry import ModelPromoter

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_production_model())
        )

        promoter = ModelPromoter(session)
        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ) as mock_project:
            await promoter.demote_to_staging("1.0.0_xgboost", reason="audit demote")

        # positional args: (session, model, ml_status)
        assert mock_project.call_args[0][2] == "staging"

    @pytest.mark.asyncio
    async def test_demote_commit_failure_triggers_rollback(self) -> None:
        """If commit raises, rollback() must cover both writes."""
        from app.ml.model_registry import ModelPromoter

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_production_model())
        )
        session.commit.side_effect = RuntimeError("DB lost")

        promoter = ModelPromoter(session)
        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ):
            with pytest.raises(RuntimeError, match="DB lost"):
                await promoter.demote_to_staging("1.0.0_xgboost", reason="x" * 10)

        session.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_demote_project_failure_triggers_rollback(self) -> None:
        """If projection raises, commit must not fire and rollback() must run."""
        from app.ml.model_registry import ModelPromoter

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_production_model())
        )
        promoter = ModelPromoter(session)
        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            side_effect=RuntimeError("governance unreachable"),
        ):
            with pytest.raises(RuntimeError, match="governance unreachable"):
                await promoter.demote_to_staging("1.0.0_xgboost", reason="x" * 10)

        session.rollback.assert_awaited_once()
        session.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_demote_requires_non_empty_reason(self) -> None:
        from app.ml.model_registry import ModelPromoter
        promoter = ModelPromoter(AsyncMock())
        for bad in (None, "", "   ", "\t\n  "):
            with pytest.raises(ValueError, match="non-empty reason"):
                await promoter.demote_to_staging("1.0.0_xgboost", reason=bad)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_demote_rejects_non_production_status(self) -> None:
        """Demote only valid from production."""
        from app.ml.model_registry import ModelPromoter

        m = self._make_production_model()
        m.status = "staging"  # already demoted
        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=m)
        )
        promoter = ModelPromoter(session)
        with pytest.raises(ValueError, match="can only demote from production"):
            await promoter.demote_to_staging("1.0.0_xgboost", reason="x" * 10)

    @pytest.mark.asyncio
    async def test_demote_clears_is_active(self) -> None:
        """After demote, is_active must be False and status='staging'."""
        from app.ml.model_registry import ModelPromoter

        m = self._make_production_model()
        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=m)
        )
        promoter = ModelPromoter(session)
        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ):
            result = await promoter.demote_to_staging(
                "1.0.0_xgboost", reason="C3 re-eval audit"
            )
        assert result.status == "staging"
        assert result.is_active is False

    @pytest.mark.asyncio
    async def test_demote_emits_audit_warning(self) -> None:
        """A structured WARNING entry must be emitted on every demote."""
        from app.ml.model_registry import ModelPromoter

        session = AsyncMock()
        session.execute.return_value = MagicMock(
            scalar_one_or_none=MagicMock(return_value=self._make_production_model())
        )
        promoter = ModelPromoter(session)

        with patch(
            "app.ml.model_registry._project_to_ai_ml_models",
            new_callable=AsyncMock,
            return_value=1,
        ), patch(
            "app.ml.model_registry._audit_logger"
        ) as mock_audit:
            await promoter.demote_to_staging(
                "1.0.0_xgboost",
                reason="C3 honest re-eval — missing A6 financial metrics",
            )

        mock_audit.warning.assert_called_once()
        # The format string + args should include the reason
        args, _ = mock_audit.warning.call_args
        assert "DEMOTE" in args[0]
        assert "C3 honest re-eval" in str(args)


# ══════════════════════════════════════════════════════════════════════════════
# R6 — Registry consolidation (migration 0040)
# ══════════════════════════════════════════════════════════════════════════════
#
# Verifies the three deliverables of the 2026-06-02 R6 consolidation:
#
#   R6-1  Migration 0040 exists and targets the correct tables.
#   R6-2  _project_to_ai_ml_models() passes ml_model_metadata_id in the
#         UPDATE so ai_ml_models.ml_model_metadata_id tracks the live FK on
#         every lifecycle transition.
#   R6-3  governance.py no longer imports UnifiedModelRegistry (the import was
#         dead — the class was never instantiated there — and has been removed).
#   R6-4  AIMLModel ORM has the ml_model_metadata_id column mapped.
#   R6-5  unified_model_registry.py has a tombstone docstring so future
#         maintainers know the module is deprecated.

class TestR6RegistryConsolidation:
    _BACKEND_ROOT = Path(__file__).parent.parent.parent

    # ── R6-1: Migration 0040 targets the correct tables ───────────────────────

    def test_migration_0040_exists(self) -> None:
        migration = self._BACKEND_ROOT / "alembic" / "versions" / "0040_registry_consolidation.py"
        assert migration.exists(), "Migration 0040 not found — run the R6 schema work"

    def test_migration_0040_adds_fk_column(self) -> None:
        migration = self._BACKEND_ROOT / "alembic" / "versions" / "0040_registry_consolidation.py"
        text = migration.read_text()
        assert "ml_model_metadata_id" in text, "Migration must add ml_model_metadata_id to ai_ml_models"
        assert "ai_ml_models" in text

    def test_migration_0040_drops_dead_table(self) -> None:
        migration = self._BACKEND_ROOT / "alembic" / "versions" / "0040_registry_consolidation.py"
        text = migration.read_text()
        assert "drop_table" in text
        assert "unified_model_registry" in text

    def test_migration_0040_revises_0039(self) -> None:
        migration = self._BACKEND_ROOT / "alembic" / "versions" / "0040_registry_consolidation.py"
        text = migration.read_text()
        assert 'down_revision' in text and '"0039"' in text, (
            "Migration 0040 must chain from 0039"
        )

    # ── R6-2: _project_to_ai_ml_models sets ml_model_metadata_id ─────────────

    @pytest.mark.asyncio
    async def test_project_sets_ml_model_metadata_id(self) -> None:
        """_project_to_ai_ml_models must include ml_model_metadata_id in .values()."""
        from unittest.mock import AsyncMock, MagicMock, call
        from app.ml.model_registry import _project_to_ai_ml_models

        session = AsyncMock()
        mock_result = MagicMock()
        mock_result.rowcount = 1
        session.execute.return_value = mock_result

        model = MagicMock()
        model.model_name = "xgboost"
        model.id = 42
        model.model_version = "1.1.0_xgboost"

        rows = await _project_to_ai_ml_models(session, model, "production")

        assert rows == 1
        # Inspect the UPDATE statement that was executed.
        session.execute.assert_awaited_once()
        update_stmt = session.execute.call_args[0][0]
        # The compiled _values dict on the ClauseElement carries the column names.
        # We extract it via the internal _values structure on the Update object.
        compiled_values = {
            str(col.key): val
            for col, val in update_stmt._values.items()
        }
        assert "ml_model_metadata_id" in compiled_values, (
            "_project_to_ai_ml_models must include ml_model_metadata_id in the UPDATE "
            "so ai_ml_models always reflects the current ml_model_metadata FK"
        )
        # SQLAlchemy wraps bound values in BindParameter; unwrap for comparison.
        raw = compiled_values["ml_model_metadata_id"]
        actual = raw.value if hasattr(raw, "value") else raw
        assert actual == 42

    # ── R6-3: governance.py no longer imports UnifiedModelRegistry ─────────────

    def test_governance_api_no_longer_imports_unified_model_registry(self) -> None:
        """The dead UnifiedModelRegistry import must be removed from governance.py."""
        gov_path = (
            self._BACKEND_ROOT / "app" / "api" / "v1" / "governance.py"
        )
        text = gov_path.read_text()
        try:
            tree = ast.parse(text, filename=str(gov_path))
        except SyntaxError:
            pytest.fail("governance.py has a syntax error — fix before running R6 tests")

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "unified_model_registry" in module:
                    names = [alias.name for alias in node.names]
                    if "UnifiedModelRegistry" in names:
                        pytest.fail(
                            "governance.py still imports UnifiedModelRegistry — "
                            "this import was dead (never instantiated) and must be removed"
                        )

    # ── R6-4: AIMLModel ORM has the FK column ─────────────────────────────────

    def test_ai_ml_model_orm_has_fk_column(self) -> None:
        from app.ai.fusion.models import AIMLModel
        assert hasattr(AIMLModel, "ml_model_metadata_id"), (
            "AIMLModel must have ml_model_metadata_id mapped column (migration 0040)"
        )

    def test_ai_ml_model_fk_column_is_nullable(self) -> None:
        """FK must be nullable — a governance row must survive ml_model_metadata deletion."""
        from app.ai.fusion.models import AIMLModel
        from sqlalchemy import inspect as sa_inspect
        mapper = sa_inspect(AIMLModel)
        col = mapper.columns["ml_model_metadata_id"]
        assert col.nullable is True, (
            "ml_model_metadata_id must be nullable (ON DELETE SET NULL semantics)"
        )

    # ── R6-5: unified_model_registry.py has a tombstone ───────────────────────

    def test_unified_model_registry_module_has_tombstone(self) -> None:
        umr_path = (
            self._BACKEND_ROOT
            / "app" / "ai" / "governance" / "unified_model_registry.py"
        )
        text = umr_path.read_text()
        assert "TOMBSTONE" in text or "tombstone" in text.lower(), (
            "unified_model_registry.py must carry a TOMBSTONE marker so future "
            "maintainers know the module is deprecated and must not be used in new code"
        )
