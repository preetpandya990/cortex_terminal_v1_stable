"""
A6 — QualityGate redesign acceptance suite.

Validates that:

  1. AUC-PR is the primary classification gate (per-type floors: xgboost 0.55,
     gru 0.50, tft 0.52, unknown 0.50).
  2. Deflated Sharpe is a hard gate — zero or negative DSR blocks promotion.
  3. Calibration ECE (ece_after) is a hard gate — > 0.05 blocks promotion.
  4. Symbol coverage is a hard gate *when present* — absent key skips with
     a warning (A7 not yet wired).
  5. Acceptance test: a GRU model with 53 % accuracy and zero DSR that
     previously passed the old accuracy-only gate now fails ≥ 3 hard gates.
  6. Break-glass requires a non-empty reason — skip_quality_gates=True with
     no reason raises BreakGlassError.
  7. Per-type floors are applied correctly for xgboost / gru / tft.
  8. AUC-PR regression vs champion is a hard gate.
  9. Absent metrics use fail-safe defaults (never silently pass).
 10. A well-formed model with all A-series metrics passes all gates.

Tests in classes 1-8 are RED on main@HEAD, GREEN after A6.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ml.model_registry import (
    BreakGlassError,
    ModelPromoter,
    QualityGate,
    QualityGateError,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_model(
    version:          str = "1.0.0",
    model_type:       str = "xgboost",
    training_samples: int = 200_000,
    metrics:          dict[str, Any] | None = None,
    status:           str = "staging",
) -> MagicMock:
    m                  = MagicMock()
    m.model_version    = version
    m.model_name       = model_type
    m.status           = status
    m.training_samples = training_samples
    m.training_metrics = metrics or {}
    return m


def _good_metrics(
    *,
    auc_pr:           float = 0.60,
    deflated_sharpe:  float = 0.15,
    ece_after:        float = 0.03,
    accuracy:         float = 0.58,
    symbol_coverage:  float | None = 0.90,
) -> dict[str, Any]:
    """Returns a metrics dict that passes all A6 gates."""
    m: dict[str, Any] = {
        "accuracy":         accuracy,
        "f1_score":         {"DOWN": 0.55, "UP": 0.61},
        "auc_pr":           auc_pr,
        "deflated_sharpe":  deflated_sharpe,
        "ece_after":        ece_after,
    }
    if symbol_coverage is not None:
        m["symbol_coverage"] = symbol_coverage
    return m


_gate = QualityGate()


# ── 1. AUC-PR primary gate ────────────────────────────────────────────────────

class TestA6AucPrPrimaryGate:
    def test_auc_pr_below_xgboost_floor_blocks(self) -> None:
        model = _make_model(metrics={
            **_good_metrics(auc_pr=0.54),   # below xgboost floor (0.55)
        })
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "auc_pr" in exc_info.value.failed_checks

    def test_auc_pr_at_floor_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(auc_pr=0.55))
        result = _gate.validate(model)
        assert result["passed"] is True
        assert result["checks"]["auc_pr"]["passed"] is True

    def test_auc_pr_absent_fails_with_zero_default(self) -> None:
        metrics = _good_metrics()
        del metrics["auc_pr"]
        model = _make_model(metrics=metrics)
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "auc_pr" in exc_info.value.failed_checks
        assert exc_info.value.failed_checks["auc_pr"].startswith("auc_pr 0.0000")

    def test_auc_pr_reported_in_checks_on_pass(self) -> None:
        model = _make_model(metrics=_good_metrics(auc_pr=0.65))
        result = _gate.validate(model)
        assert result["checks"]["auc_pr"]["value"] == pytest.approx(0.65)
        assert result["checks"]["auc_pr"]["threshold"] == pytest.approx(0.55)


# ── 2. Deflated Sharpe hard gate ──────────────────────────────────────────────

class TestA6DeflatedSharpeGate:
    def test_zero_dsr_blocks(self) -> None:
        model = _make_model(metrics=_good_metrics(deflated_sharpe=0.0))
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "deflated_sharpe" in exc_info.value.failed_checks

    def test_negative_dsr_blocks(self) -> None:
        model = _make_model(metrics=_good_metrics(deflated_sharpe=-0.30))
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "deflated_sharpe" in exc_info.value.failed_checks

    def test_absent_dsr_fails_with_failsafe_default(self) -> None:
        metrics = _good_metrics()
        del metrics["deflated_sharpe"]
        model = _make_model(metrics=metrics)
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "deflated_sharpe" in exc_info.value.failed_checks

    def test_positive_dsr_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(deflated_sharpe=0.01))
        result = _gate.validate(model)
        assert result["checks"]["deflated_sharpe"]["passed"] is True

    def test_dsr_gate_is_strict_greater_than(self) -> None:
        # Exactly MIN_DSR (0.0) must fail — the gate is strictly positive.
        assert QualityGate.MIN_DSR == 0.0
        model = _make_model(metrics=_good_metrics(deflated_sharpe=0.0))
        with pytest.raises(QualityGateError):
            _gate.validate(model)


# ── 3. Calibration ECE hard gate ──────────────────────────────────────────────

class TestA6CalibrationEceGate:
    def test_high_ece_blocks(self) -> None:
        model = _make_model(metrics=_good_metrics(ece_after=0.06))
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "calibration_ece" in exc_info.value.failed_checks

    def test_absent_ece_fails_with_failsafe_default(self) -> None:
        metrics = _good_metrics()
        del metrics["ece_after"]
        model = _make_model(metrics=metrics)
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "calibration_ece" in exc_info.value.failed_checks

    def test_ece_at_threshold_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(ece_after=0.05))
        result = _gate.validate(model)
        assert result["checks"]["calibration_ece"]["passed"] is True

    def test_ece_below_threshold_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(ece_after=0.02))
        result = _gate.validate(model)
        assert result["checks"]["calibration_ece"]["passed"] is True

    def test_max_ece_constant_is_five_percent(self) -> None:
        assert QualityGate.MAX_ECE == pytest.approx(0.05)


# ── 4. Symbol coverage gate ───────────────────────────────────────────────────

class TestA6SymbolCoverageGate:
    def test_low_coverage_blocks_when_present(self) -> None:
        model = _make_model(metrics=_good_metrics(symbol_coverage=0.70))
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        assert "symbol_coverage" in exc_info.value.failed_checks

    def test_absent_coverage_skips_gate_with_warning(self) -> None:
        model = _make_model(metrics=_good_metrics(symbol_coverage=None))
        result = _gate.validate(model)
        assert result["passed"] is True
        checks = result["checks"]["symbol_coverage"]
        assert checks["passed"] is True
        assert "warning" in checks

    def test_sufficient_coverage_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(symbol_coverage=0.95))
        result = _gate.validate(model)
        assert result["checks"]["symbol_coverage"]["passed"] is True

    def test_coverage_exactly_at_floor_passes(self) -> None:
        model = _make_model(metrics=_good_metrics(symbol_coverage=QualityGate.MIN_COVERAGE))
        result = _gate.validate(model)
        assert result["checks"]["symbol_coverage"]["passed"] is True


# ── 5. Acceptance test: 53 % GRU + zero DSR was green, now ≥ 3 gates fail ────

class TestA6Acceptance53PctZeroDsrBlocked:
    """
    A GRU model with 53 % accuracy and zero Deflated Sharpe previously passed
    the old accuracy-only gate (old GRU floor = 0.50).  Under A6 it must fail
    ≥ 3 hard gates.
    """

    def _build_fixture(self) -> MagicMock:
        return _make_model(
            model_type="gru",
            training_samples=200_000,
            metrics={
                "accuracy":        0.53,   # old gate: 0.53 >= 0.50 → passed
                "f1_score":        {"DOWN": 0.51, "UP": 0.55},
                "deflated_sharpe": 0.0,    # zero DSR — was soft warning, now hard gate
                # auc_pr, ece_after deliberately absent (represent pre-A3/A4 models)
            },
        )

    def test_53pct_zero_dsr_blocked(self) -> None:
        model = self._build_fixture()
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        failed = exc_info.value.failed_checks
        assert len(failed) >= 3, (
            f"Expected ≥ 3 gate failures; got {len(failed)}: {list(failed)}"
        )

    def test_specific_gates_that_fail(self) -> None:
        model = self._build_fixture()
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        failed = exc_info.value.failed_checks
        # These three must all be in the failure set.
        assert "auc_pr" in failed,           f"auc_pr should fail; got: {list(failed)}"
        assert "deflated_sharpe" in failed,  f"deflated_sharpe should fail; got: {list(failed)}"
        assert "calibration_ece" in failed,  f"calibration_ece should fail; got: {list(failed)}"

    def test_error_message_lists_failed_gates(self) -> None:
        model = self._build_fixture()
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(model)
        msg = str(exc_info.value)
        assert "auc_pr" in msg
        assert "deflated_sharpe" in msg


# ── 6. Break-glass protocol ───────────────────────────────────────────────────

class TestA6BreakGlass:
    def _make_session(self) -> AsyncMock:
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock()
        session.rollback = AsyncMock()
        return session

    def _make_staging_model(self) -> MagicMock:
        return _make_model(status="staging")

    @pytest.mark.asyncio
    async def test_skip_gates_without_reason_raises_break_glass_error(self) -> None:
        session = self._make_session()
        promoter = ModelPromoter(session)
        with pytest.raises(BreakGlassError):
            await promoter.promote_to_production(
                "1.0.0",
                "xgboost",
                skip_quality_gates=True,
                break_glass_reason=None,
            )

    @pytest.mark.asyncio
    async def test_skip_gates_with_empty_reason_raises_break_glass_error(self) -> None:
        session = self._make_session()
        promoter = ModelPromoter(session)
        with pytest.raises(BreakGlassError):
            await promoter.promote_to_production(
                "1.0.0",
                "xgboost",
                skip_quality_gates=True,
                break_glass_reason="   ",   # whitespace-only
            )

    @pytest.mark.asyncio
    async def test_skip_gates_staging_without_reason_raises_break_glass_error(self) -> None:
        session = self._make_session()
        promoter = ModelPromoter(session)
        with pytest.raises(BreakGlassError):
            await promoter.promote_to_staging(
                "1.0.0",
                skip_quality_gates=True,
                break_glass_reason=None,
            )

    @pytest.mark.asyncio
    async def test_skip_gates_with_reason_proceeds_past_validation(self) -> None:
        session  = self._make_session()
        model    = self._make_staging_model()
        prev     = None

        async def fake_execute(stmt):
            r = MagicMock()
            r.scalar_one_or_none.return_value = model
            return r

        session.execute = fake_execute
        session.refresh = AsyncMock(return_value=model)

        promoter = ModelPromoter(session)
        # Should not raise BreakGlassError or QualityGateError — gates are bypassed.
        result = await promoter.promote_to_production(
            "1.0.0",
            "xgboost",
            skip_quality_gates=True,
            break_glass_reason="Hotfix: champion artifact corrupted",
        )
        # DB commit must have been called (promotion happened).
        session.commit.assert_called_once()

    def test_break_glass_error_is_value_error_subclass(self) -> None:
        assert issubclass(BreakGlassError, ValueError)


# ── 7. Per-type AUC-PR floors ─────────────────────────────────────────────────

class TestA6PerTypeAucPrFloors:
    def test_xgboost_floor_is_55(self) -> None:
        assert QualityGate.MIN_AP_BY_TYPE["xgboost"] == pytest.approx(0.55)

    def test_gru_floor_is_50(self) -> None:
        assert QualityGate.MIN_AP_BY_TYPE["gru"] == pytest.approx(0.50)

    def test_tft_floor_is_52(self) -> None:
        assert QualityGate.MIN_AP_BY_TYPE["tft"] == pytest.approx(0.52)

    def test_unknown_type_uses_default_floor(self) -> None:
        gate = QualityGate()
        assert gate._min_ap_for("future_model") == pytest.approx(QualityGate.MIN_AP_DEFAULT)

    def test_gru_passes_at_its_lower_floor(self) -> None:
        model = _make_model(
            model_type="gru",
            metrics=_good_metrics(auc_pr=0.50, accuracy=0.50),
        )
        result = _gate.validate(model)
        assert result["checks"]["auc_pr"]["passed"] is True

    def test_gru_ap_that_fails_xgboost_floor_passes_gru_floor(self) -> None:
        # 0.52 fails xgboost (≥ 0.55) but passes gru (≥ 0.50).
        gru = _make_model(model_type="gru",     metrics=_good_metrics(auc_pr=0.52, accuracy=0.50))
        xgb = _make_model(model_type="xgboost", metrics=_good_metrics(auc_pr=0.52))

        result_gru = _gate.validate(gru)
        assert result_gru["checks"]["auc_pr"]["passed"] is True

        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(xgb)
        assert "auc_pr" in exc_info.value.failed_checks

    def test_tft_floor_between_gru_and_xgboost(self) -> None:
        # 0.51 is >= gru (0.50) but < tft (0.52).
        tft = _make_model(model_type="tft", metrics=_good_metrics(auc_pr=0.51, accuracy=0.51))
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(tft)
        assert "auc_pr" in exc_info.value.failed_checks


# ── 8. AUC-PR regression vs champion ─────────────────────────────────────────

class TestA6AucPrRegression:
    def test_ap_regression_beyond_drop_threshold_blocks(self) -> None:
        champion  = _make_model("0.9.0", metrics={"accuracy": 0.60, "auc_pr": 0.70, "f1_score": {}})
        challenger = _make_model("1.0.0", metrics=_good_metrics(auc_pr=0.60))  # drop = 0.10 > 0.05
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(challenger, previous_model=champion)
        assert "ap_degradation" in exc_info.value.failed_checks

    def test_ap_regression_within_threshold_passes(self) -> None:
        champion   = _make_model("0.9.0", metrics={"accuracy": 0.58, "auc_pr": 0.62, "f1_score": {}})
        challenger = _make_model("1.0.0", metrics=_good_metrics(auc_pr=0.60))  # drop = 0.02 ≤ 0.05
        result = _gate.validate(challenger, previous_model=champion)
        assert result["checks"]["ap_degradation"]["passed"] is True

    def test_ap_improvement_over_champion_passes(self) -> None:
        champion   = _make_model("0.9.0", metrics={"accuracy": 0.56, "auc_pr": 0.56, "f1_score": {}})
        challenger = _make_model("1.0.0", metrics=_good_metrics(auc_pr=0.62))
        result = _gate.validate(challenger, previous_model=champion)
        assert result["checks"]["ap_degradation"]["passed"] is True

    def test_no_previous_model_skips_regression_check(self) -> None:
        model = _make_model(metrics=_good_metrics())
        result = _gate.validate(model, previous_model=None)
        assert "ap_degradation" not in result["checks"]


# ── 9. Fail-safe defaults for absent metrics ──────────────────────────────────

class TestA6FailSafeDefaults:
    """Every absent A-series metric must use a fail-safe default that fails the gate.
    No absent metric should silently pass."""

    def _absent(self, key: str) -> MagicMock:
        metrics = _good_metrics()
        del metrics[key]
        return _make_model(metrics=metrics)

    def test_absent_auc_pr_fails(self) -> None:
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(self._absent("auc_pr"))
        assert "auc_pr" in exc_info.value.failed_checks

    def test_absent_deflated_sharpe_fails(self) -> None:
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(self._absent("deflated_sharpe"))
        assert "deflated_sharpe" in exc_info.value.failed_checks

    def test_absent_ece_after_fails(self) -> None:
        with pytest.raises(QualityGateError) as exc_info:
            _gate.validate(self._absent("ece_after"))
        assert "calibration_ece" in exc_info.value.failed_checks

    def test_absent_symbol_coverage_warns_not_fails(self) -> None:
        # symbol_coverage is optional until A7 is wired.
        metrics = _good_metrics(symbol_coverage=None)
        model   = _make_model(metrics=metrics)
        result  = _gate.validate(model)
        assert result["passed"] is True


# ── 10. Well-formed model passes all gates ────────────────────────────────────

class TestA6FullPassingModel:
    def test_well_formed_xgboost_passes_all_gates(self) -> None:
        model  = _make_model(metrics=_good_metrics())
        result = _gate.validate(model)

        assert result["passed"] is True
        for gate_name, check in result["checks"].items():
            assert check["passed"] is True, (
                f"Gate '{gate_name}' unexpectedly failed: {check}"
            )

    def test_result_contains_validated_at_timestamp(self) -> None:
        model  = _make_model(metrics=_good_metrics())
        result = _gate.validate(model)
        ts = datetime.fromisoformat(result["validated_at"])
        assert ts.tzinfo is not None   # timezone-aware

    def test_result_contains_model_version(self) -> None:
        model  = _make_model(version="2.1.0", metrics=_good_metrics())
        result = _gate.validate(model)
        assert result["model_version"] == "2.1.0"

    def test_all_expected_check_keys_present(self) -> None:
        model  = _make_model(metrics=_good_metrics())
        result = _gate.validate(model)
        expected = {
            "auc_pr",
            "deflated_sharpe",
            "calibration_ece",
            "symbol_coverage",
            "training_samples",
            "accuracy",
            "metadata_completeness",
        }
        assert expected <= set(result["checks"].keys())
