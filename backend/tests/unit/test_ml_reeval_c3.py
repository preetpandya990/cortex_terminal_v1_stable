"""
C3 — Honest re-evaluation acceptance suite.

Validates the analytical re-eval pipeline that audits live production models
against the post-A6 hard gates and synthesises structural findings.  The
script is **report-only**; these tests verify both the per-model audit logic
and the end-to-end orchestration (DB-mocked) without ever mutating registry
state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.reeval_production_model import (
    EXPECTED_CURRENT_FEATURE_COUNT,
    PRODUCTION_MEMBERS,
    _audit_one_model,
    _build_recommendation,
    _expected_calibrator_path,
    run_reeval,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _make_model(
    *,
    name:       str  = "xgboost",
    version:    str  = "1.0.0_xgboost",
    status:     str  = "production",
    is_active:  bool = True,
    metrics:    dict | None = None,
    features:   list | None = None,
    onnx_path:  str  | None = "models/production/models/1.0.0_xgboost_inference.so",
    samples:    int  = 2_950_200,
) -> MagicMock:
    """Synthesise an MLModelMetadata-shaped object for audit testing."""
    m = MagicMock()
    m.model_id          = f"{name}_{version}"
    m.model_name        = name
    m.model_version     = version
    m.status            = status
    m.is_active         = is_active
    m.training_metrics  = metrics  or {}
    m.training_features = features if features is not None else []
    m.onnx_path         = onnx_path
    m.trained_at        = datetime(2026, 5, 10, tzinfo=timezone.utc)
    m.deployed_at       = datetime(2026, 5, 10, tzinfo=timezone.utc)
    m.training_samples  = samples
    return m


def _passing_metrics() -> dict:
    """A metrics dict that passes every A6 hard gate."""
    return {
        "accuracy":          0.65,
        "f1_score":          0.62,
        "auc_pr":            0.60,
        "deflated_sharpe":   1.20,
        "ece_after":         0.03,
        "symbol_coverage":   0.92,
        "precision":         0.61,
        "recall":            0.59,
    }


# ══════════════════════════════════════════════════════════════════════════════
# _audit_one_model — A6 gate verdict
# ══════════════════════════════════════════════════════════════════════════════

class TestA6GateVerdict:

    def test_passing_model_records_gate_pass(self) -> None:
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics=_passing_metrics(),
            features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        assert audit["a6_gate"]["passed"] is True
        assert audit["a6_gate"]["n_failed"] == 0
        assert audit["a6_gate"]["failed_gates"] == {}

    def test_1_0_0_style_model_fails_3_metric_gates(self) -> None:
        """The exact shipped-1.0.0 shape: stored Sharpe=0.0 + missing A6 metrics."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics={
                "accuracy":     0.65,
                "f1_score":     0.62,
                "sharpe_ratio": 0.0,
                "n_trades":     0,
                # auc_pr, deflated_sharpe, ece_after all absent — gates 1/2/3 fire
            },
            features=["f" + str(i) for i in range(49)],   # legacy 49-feat schema
        )
        audit = _audit_one_model(model, qg)
        assert audit["a6_gate"]["passed"] is False
        failed = audit["a6_gate"]["failed_gates"]
        assert "auc_pr"          in failed   # gate 1 → None → 0.0 < 0.55
        assert "deflated_sharpe" in failed   # gate 2 → None → -1.0 not > 0
        assert "calibration_ece" in failed   # gate 3 → None → 1.0 > 0.05
        assert audit["a6_gate"]["n_failed"] >= 3


# ══════════════════════════════════════════════════════════════════════════════
# _audit_one_model — Structural findings
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuralFindings:

    def _findings_by_kind(self, audit: dict[str, Any]) -> dict[str, dict]:
        return {f["kind"]: f for f in audit["structural_findings"]}

    def test_obsolete_schema_flagged(self) -> None:
        """49 features vs current 69 → obsolete_feature_schema finding."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics=_passing_metrics(),
            features=["f" + str(i) for i in range(49)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        finding = self._findings_by_kind(audit).get("obsolete_feature_schema")
        assert finding is not None
        assert finding["stored"] == 49
        assert finding["current"] == EXPECTED_CURRENT_FEATURE_COUNT
        assert finding["missing_count"] == EXPECTED_CURRENT_FEATURE_COUNT - 49

    def test_current_schema_not_flagged(self) -> None:
        """A 69-feature model must NOT raise the obsolete-schema finding."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics=_passing_metrics(),
            features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        assert "obsolete_feature_schema" not in self._findings_by_kind(audit)

    def test_calibrator_missing_when_file_absent(self) -> None:
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(metrics=_passing_metrics())
        with patch("pathlib.Path.exists", return_value=False):
            audit = _audit_one_model(model, qg)
        finding = self._findings_by_kind(audit).get("calibrator_missing")
        assert finding is not None
        assert finding["expected_path"] is not None

    def test_calibrator_missing_when_onnx_path_unset(self) -> None:
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(metrics=_passing_metrics(), onnx_path=None)
        audit = _audit_one_model(model, qg)
        finding = self._findings_by_kind(audit).get("calibrator_missing")
        assert finding is not None
        assert finding["expected_path"] is None

    def test_calibrator_present_not_flagged(self) -> None:
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics=_passing_metrics(),
            features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        assert "calibrator_missing" not in self._findings_by_kind(audit)

    def test_fabricated_zero_sharpe_flagged(self) -> None:
        """sharpe_ratio=0.0 + n_trades=0 must be flagged as the A1 fabricated zero."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(metrics={**_passing_metrics(),
                                     "sharpe_ratio": 0.0, "n_trades": 0})
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        finding = self._findings_by_kind(audit).get("fabricated_zero_sharpe")
        assert finding is not None
        assert finding["stored_sharpe"]   == 0.0
        assert finding["stored_n_trades"] == 0

    def test_real_sharpe_not_flagged_as_fabricated(self) -> None:
        """A genuine non-zero Sharpe must NOT be flagged as fabricated."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics={**_passing_metrics(), "sharpe_ratio": 1.42, "n_trades": 250},
            features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        assert "fabricated_zero_sharpe" not in self._findings_by_kind(audit)

    def test_zero_sharpe_with_real_trades_not_flagged(self) -> None:
        """Only the (0.0, 0) tuple is suspicious — a real zero-Sharpe-with-trades
        result is legitimate (rare but possible)."""
        from app.ml.model_registry import QualityGate
        qg = QualityGate()
        model = _make_model(
            metrics={**_passing_metrics(), "sharpe_ratio": 0.0, "n_trades": 187},
            features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
        )
        with patch("pathlib.Path.exists", return_value=True):
            audit = _audit_one_model(model, qg)
        assert "fabricated_zero_sharpe" not in self._findings_by_kind(audit)


# ══════════════════════════════════════════════════════════════════════════════
# _expected_calibrator_path — A4 serving-path convention
# ══════════════════════════════════════════════════════════════════════════════

class TestExpectedCalibratorPath:

    def test_xgboost_path_uses_first_three_letters(self) -> None:
        m = _make_model(
            name="xgboost",
            onnx_path="models/production/models/1.0.0_xgboost_inference.so",
        )
        assert _expected_calibrator_path(m) == Path(
            "models/production/models/calibrator_xgb.pkl"
        )

    def test_gru_path_uses_first_three_letters(self) -> None:
        m = _make_model(
            name="gru",
            onnx_path="models/production/models/1.0.0_gru_inference.onnx",
        )
        assert _expected_calibrator_path(m) == Path(
            "models/production/models/calibrator_gru.pkl"
        )

    def test_none_onnx_path_returns_none(self) -> None:
        assert _expected_calibrator_path(_make_model(onnx_path=None)) is None


# ══════════════════════════════════════════════════════════════════════════════
# _build_recommendation — concrete operator instructions
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildRecommendation:

    def test_meets_bar_returns_no_action(self) -> None:
        msg = _build_recommendation([], "MEETS_BAR")
        assert "No action required" in msg

    def test_demote_recommended_includes_concrete_cli_invocation(self) -> None:
        audited = [{
            "model_name":    "xgboost",
            "model_version": "1.0.0_xgboost",
            "a6_gate": {"passed": False, "n_failed": 3,
                        "failed_gates": {"auc_pr": "x", "deflated_sharpe": "y",
                                         "calibration_ece": "z"}},
            "structural_findings": [{"kind": "obsolete_feature_schema"}],
        }]
        msg = _build_recommendation(audited, "DEMOTE_RECOMMENDED")
        assert "promote_model.py demote" in msg
        assert "--version 1.0.0_xgboost" in msg
        assert "--reason" in msg
        assert "atomic" in msg.lower()


# ══════════════════════════════════════════════════════════════════════════════
# run_reeval — end-to-end orchestration (DB mocked)
# ══════════════════════════════════════════════════════════════════════════════

class TestRunReevalE2E:

    @pytest.mark.asyncio
    async def test_no_production_models_returns_1(self, tmp_path: Path) -> None:
        """Operational error path: no active prod records → exit 1."""
        with patch(
            "scripts.reeval_production_model._fetch_active_production",
            new_callable=AsyncMock,
            return_value=None,
        ), patch(
            "scripts.reeval_production_model.create_async_engine"
        ) as mock_engine:
            mock_engine.return_value.dispose = AsyncMock()
            rc = await run_reeval(tmp_path)
        assert rc == 1
        assert list(tmp_path.glob("reeval_1.0.0_*.json")) == []

    @pytest.mark.asyncio
    async def test_subthreshold_models_return_2_and_write_report(
        self, tmp_path: Path
    ) -> None:
        """Both 1.0.0 models in fabricated-metric shape → exit 2, report written."""
        model = _make_model(
            metrics={
                "accuracy": 0.65, "f1_score": 0.62,
                "sharpe_ratio": 0.0, "n_trades": 0,
            },
            features=["f" + str(i) for i in range(49)],
        )

        async def _fetch_stub(session, name):
            m = _make_model(
                name=name,
                version=f"1.0.0_{name}",
                metrics=model.training_metrics,
                features=model.training_features,
            )
            return m

        with patch(
            "scripts.reeval_production_model._fetch_active_production",
            new=_fetch_stub,
        ), patch(
            "scripts.reeval_production_model.create_async_engine"
        ) as mock_engine, patch(
            "pathlib.Path.exists", return_value=False
        ):
            mock_engine.return_value.dispose = AsyncMock()
            rc = await run_reeval(tmp_path)

        assert rc == 2
        reports = list(tmp_path.glob("reeval_1.0.0_*.json"))
        assert len(reports) == 1

        report = json.loads(reports[0].read_text())
        assert report["report_kind"] == "c3_honest_reeval"
        assert report["report_only"] is True
        assert report["verdict"] == "DEMOTE_RECOMMENDED"
        assert len(report["audited_models"]) == len(PRODUCTION_MEMBERS)
        for am in report["audited_models"]:
            assert am["a6_gate"]["passed"] is False
            assert am["a6_gate"]["n_failed"] >= 3
            kinds = {f["kind"] for f in am["structural_findings"]}
            assert "obsolete_feature_schema" in kinds
            assert "calibrator_missing"      in kinds
            assert "fabricated_zero_sharpe"  in kinds

    @pytest.mark.asyncio
    async def test_passing_models_return_0_and_meets_bar(
        self, tmp_path: Path
    ) -> None:
        """All members pass every gate → exit 0, verdict=MEETS_BAR."""
        async def _fetch_stub(session, name):
            return _make_model(
                name=name,
                version=f"1.0.0_{name}",
                metrics=_passing_metrics(),
                features=["f" + str(i) for i in range(EXPECTED_CURRENT_FEATURE_COUNT)],
            )

        with patch(
            "scripts.reeval_production_model._fetch_active_production",
            new=_fetch_stub,
        ), patch(
            "scripts.reeval_production_model.create_async_engine"
        ) as mock_engine, patch(
            "pathlib.Path.exists", return_value=True
        ):
            mock_engine.return_value.dispose = AsyncMock()
            rc = await run_reeval(tmp_path)

        assert rc == 0
        report = json.loads(next(tmp_path.glob("reeval_1.0.0_*.json")).read_text())
        assert report["verdict"] == "MEETS_BAR"
        assert all(am["a6_gate"]["passed"] for am in report["audited_models"])
