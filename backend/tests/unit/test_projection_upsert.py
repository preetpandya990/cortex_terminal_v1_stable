"""
WS4 — tests for the hardened ai_ml_models projection and the repair script.

The warning-only rowcount==0 path in ``_project_to_ai_ml_models`` is how the
governance table went dark; it now self-heals with an INSERT .. ON CONFLICT
upsert. The repair script replays that same projection for every
production/active metadata row.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.dialects.postgresql import Insert

from app.ml.model_registry import _metric_scalar, _project_to_ai_ml_models
from scripts.repair_ai_ml_models_projection import run as repair_run

REPAIR_MODULE = "scripts.repair_ai_ml_models_projection"


def _metadata_row(
    id_: int = 164,
    model_name: str = "xgboost",
    version: str = "1.1.1_xgboost",
    status: str = "production",
) -> MagicMock:
    m = MagicMock()
    m.id = id_
    m.model_id = f"{model_name}_{version}"
    m.model_name = model_name
    m.model_version = version
    m.model_path = f"models/production/{model_name}.onnx"
    m.status = status
    m.is_active = status == "production"
    m.trained_at = datetime(2026, 5, 31, tzinfo=timezone.utc)
    m.training_metrics = {
        "accuracy": 0.6395,
        "precision": {"up": 0.62, "down": 0.66},
        "recall": {"up": 0.61, "down": 0.67},
        "f1_score": {"up": 0.61, "down": 0.66},
    }
    return m


def _session_with_update_rowcount(rowcount: int) -> AsyncMock:
    session = AsyncMock()
    update_result = MagicMock()
    update_result.rowcount = rowcount
    session.execute.return_value = update_result
    return session


# ─── _metric_scalar ──────────────────────────────────────────────────────────

class TestMetricScalar:
    def test_scalar_passes_through(self):
        assert _metric_scalar(0.6395) == pytest.approx(0.6395)

    def test_per_class_dict_collapses_to_macro_mean(self):
        assert _metric_scalar({"up": 0.62, "down": 0.66}) == pytest.approx(0.64)

    def test_missing_or_invalid_becomes_none(self):
        assert _metric_scalar(None) is None
        assert _metric_scalar({}) is None
        assert _metric_scalar({"up": "n/a"}) is None
        assert _metric_scalar(True) is None


# ─── _project_to_ai_ml_models upsert branch ──────────────────────────────────

class TestProjectionUpsert:
    async def test_existing_row_updates_without_insert(self):
        session = _session_with_update_rowcount(1)
        rows = await _project_to_ai_ml_models(session, _metadata_row(), "production")

        assert rows == 1
        session.execute.assert_awaited_once()  # UPDATE only, no INSERT

    async def test_missing_row_inserts_with_on_conflict(self):
        session = _session_with_update_rowcount(0)
        rows = await _project_to_ai_ml_models(session, _metadata_row(), "production")

        assert rows == 1
        assert session.execute.await_count == 2  # UPDATE (miss) then INSERT
        insert_stmt = session.execute.await_args_list[1].args[0]
        assert isinstance(insert_stmt, Insert)
        # ON CONFLICT clause present → concurrent projection cannot raise.
        assert insert_stmt._post_values_clause is not None

        values = {c.key: v.value for c, v in insert_stmt._values.items()}
        assert values["model_name"] == "cortex_xgboost_1d"
        assert values["model_type"] == "xgboost"
        assert values["deployment_state"] == "live"
        assert values["model_version"] == "1.1.1_xgboost"
        assert values["ml_model_metadata_id"] == 164
        assert values["timeframe"] == "1d"
        assert values["accuracy"] == pytest.approx(0.6395)
        assert values["precision"] == pytest.approx(0.64)
        assert values["governance_metadata"]["auto_created_by"] == "_project_to_ai_ml_models"

    async def test_insert_maps_ml_status_to_governance_state(self):
        session = _session_with_update_rowcount(0)
        await _project_to_ai_ml_models(session, _metadata_row(status="staging"), "staging")

        insert_stmt = session.execute.await_args_list[1].args[0]
        values = {c.key: v.value for c, v in insert_stmt._values.items()}
        assert values["deployment_state"] == "paper"

    async def test_insert_survives_absent_metrics(self):
        model = _metadata_row()
        model.training_metrics = None
        session = _session_with_update_rowcount(0)
        rows = await _project_to_ai_ml_models(session, model, "production")

        assert rows == 1
        insert_stmt = session.execute.await_args_list[1].args[0]
        values = {c.key: v.value for c, v in insert_stmt._values.items()}
        assert values["accuracy"] is None
        assert values["f1_score"] is None


# ─── Repair script: dry-run vs execute ───────────────────────────────────────

def _patched_session() -> tuple[AsyncMock, MagicMock]:
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return session, factory


class TestRepairScript:
    async def test_dry_run_reports_but_never_projects(self):
        session, factory = _patched_session()
        rows = [_metadata_row(), _metadata_row(165, "gru", "1.1.1_gru")]
        with (
            patch(f"{REPAIR_MODULE}.AsyncSessionLocal", factory),
            patch(f"{REPAIR_MODULE}.find_authoritative_rows", new_callable=AsyncMock) as find,
            patch(f"{REPAIR_MODULE}.load_governance_row", new_callable=AsyncMock) as load,
            patch(f"{REPAIR_MODULE}._project_to_ai_ml_models", new_callable=AsyncMock) as project,
        ):
            find.return_value = rows
            load.return_value = None
            exit_code = await repair_run(dry_run=True)

        assert exit_code == 0
        project.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_execute_projects_each_row_and_commits_once(self):
        session, factory = _patched_session()
        xgb, gru = _metadata_row(), _metadata_row(165, "gru", "1.1.1_gru")
        with (
            patch(f"{REPAIR_MODULE}.AsyncSessionLocal", factory),
            patch(f"{REPAIR_MODULE}.find_authoritative_rows", new_callable=AsyncMock) as find,
            patch(f"{REPAIR_MODULE}.load_governance_row", new_callable=AsyncMock) as load,
            patch(f"{REPAIR_MODULE}._project_to_ai_ml_models", new_callable=AsyncMock) as project,
        ):
            find.return_value = [xgb, gru]
            load.return_value = None
            exit_code = await repair_run(dry_run=False)

        assert exit_code == 0
        assert project.await_count == 2
        assert project.await_args_list[0].args == (session, xgb, "production")
        assert project.await_args_list[1].args == (session, gru, "production")
        session.commit.assert_awaited_once()

    async def test_no_authoritative_rows_is_success_noop(self):
        session, factory = _patched_session()
        with (
            patch(f"{REPAIR_MODULE}.AsyncSessionLocal", factory),
            patch(f"{REPAIR_MODULE}.find_authoritative_rows", new_callable=AsyncMock) as find,
        ):
            find.return_value = []
            exit_code = await repair_run(dry_run=False)

        assert exit_code == 0
        session.commit.assert_not_awaited()

    async def test_projection_failure_returns_error_and_no_commit(self):
        session, factory = _patched_session()
        with (
            patch(f"{REPAIR_MODULE}.AsyncSessionLocal", factory),
            patch(f"{REPAIR_MODULE}.find_authoritative_rows", new_callable=AsyncMock) as find,
            patch(f"{REPAIR_MODULE}.load_governance_row", new_callable=AsyncMock) as load,
            patch(f"{REPAIR_MODULE}._project_to_ai_ml_models", new_callable=AsyncMock) as project,
        ):
            find.return_value = [_metadata_row()]
            load.return_value = None
            project.side_effect = RuntimeError("db unreachable")
            exit_code = await repair_run(dry_run=False)

        assert exit_code == 2
        session.commit.assert_not_awaited()
