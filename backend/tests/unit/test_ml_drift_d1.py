"""
D1 — Drift Informs the Gate: acceptance suite.

Validates:
  1. DriftDetector._handle_drift_action() never mutates deployment_state
     (the plan's primary acceptance test — test_drift_flags_not_demotes).
  2. Distribution-shift signal (z-score) computation is correct and gated.
  3. Realised directional accuracy is correctly derived from PaperTradeOutcome.
  4. Realised net information ratio is correctly derived from PaperTradeOutcome.
  5. Any single triggered signal sets drift_detected; all-clear keeps it False.
  6. Prometheus gauges are emitted on every check.
  7. build_promotion_report() includes drift_advisory in the signed body; the
     body hash changes when drift_advisory changes (tamper detection).
  8. Baseline fallback produces a structurally correct report with no fake drift.

Design notes
------------
* All DB interactions are mocked via AsyncMock — no Postgres required.
* Prometheus calls are patched so tests do not require prometheus_client
  to be importable in every CI environment.
* PaperTradeOutcome rows are built as MagicMock objects matching the exact
  column types that _compute_realised_metrics reads.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.ai.governance.drift_detector import (
    DriftDetector,
    _ACCURACY_DROP_THRESHOLD_PP,
    _IR_DETERIORATION_THRESHOLD,
    _MIN_TRADES_FOR_REALISED_METRICS,
)
from app.ml.evaluation.promotion_report import (
    _extract_body_for_hash,
    _sha256_canonical,
    build_promotion_report,
    load_and_verify_report,
    PromotionStatus,
)


# ══════════════════════════════════════════════════════════════════════════════
# Shared fixtures & helpers
# ══════════════════════════════════════════════════════════════════════════════

_UTC = timezone.utc


def _ai_model(
    *,
    model_id: int = 1,
    model_name: str = "cortex_xgboost_1d",
    deployment_state: str = "live",
    governance_metadata: dict | None = None,
) -> MagicMock:
    m = MagicMock()
    m.id                  = model_id
    m.model_name          = model_name
    m.deployment_state    = deployment_state
    m.governance_metadata = governance_metadata or {}
    m.created_at          = datetime(2026, 5, 1, tzinfo=_UTC)
    m.updated_at          = datetime(2026, 5, 20, tzinfo=_UTC)
    return m


def _ml_meta(
    *,
    model_id: str = "xgb_1.0.0",
    model_name: str = "xgboost",
    model_version: str = "1.0.0_xgboost",
    training_metrics: dict | None = None,
    training_prediction_stats: dict | None = None,
    deployed_at: datetime | None = None,
) -> MagicMock:
    m = MagicMock()
    m.model_id                  = model_id
    m.model_name                = model_name
    m.model_version             = model_version
    m.training_metrics          = training_metrics or {"accuracy": 0.60}
    m.training_prediction_stats = training_prediction_stats or {"mean": 0.50, "std": 0.10}
    m.deployed_at               = deployed_at
    return m


def _ml_prediction(value: float) -> MagicMock:
    p = MagicMock()
    p.prediction = value
    return p


def _outcome(
    *,
    net_pnl: float,
    entry_price: float,
    quantity: int,
    ml_direction_correct: bool,
) -> MagicMock:
    o = MagicMock()
    o.net_pnl              = Decimal(str(net_pnl))
    o.entry_price          = Decimal(str(entry_price))
    o.quantity             = quantity
    o.ml_direction_correct = ml_direction_correct
    return o


def _make_db(
    *,
    ai_model: MagicMock | None = None,
    production_ml_records: list | None = None,
    ml_predictions: list | None = None,
    outcomes: list | None = None,
) -> AsyncMock:
    """
    Build a mock AsyncSession whose execute() returns the right scalars
    depending on call order: ai_model → production_ml_records → predictions
    → outcomes.
    """
    db = AsyncMock()

    async def _execute(stmt):  # noqa: ANN001
        r = MagicMock()
        return r

    # Build a side-effect list — each call to db.execute() pops one item.
    results: list[MagicMock] = []

    def _scalar_one_or_none(val):
        r = MagicMock()
        r.scalar_one_or_none.return_value = val
        return r

    def _scalars_all(vals):
        r = MagicMock()
        inner = MagicMock()
        inner.all.return_value = vals or []
        r.scalars.return_value = inner
        return r

    call_results: list[MagicMock] = []
    if ai_model is not None:
        call_results.append(_scalar_one_or_none(ai_model))  # SELECT AIMLModel
    call_results.append(_scalars_all(production_ml_records or []))  # SELECT MLModelMetadata
    call_results.append(_scalars_all(ml_predictions or []))          # SELECT MLPrediction
    call_results.append(_scalars_all(outcomes or []))                # SELECT PaperTradeOutcome

    db.execute = AsyncMock(side_effect=call_results)
    db.commit  = AsyncMock()
    db.refresh = AsyncMock()
    db.add     = MagicMock()

    return db


def _make_pubsub() -> AsyncMock:
    ps = AsyncMock()
    ps.publish_json = AsyncMock()
    return ps


# ══════════════════════════════════════════════════════════════════════════════
# 1 — Primary acceptance test: drift flag, zero state mutation
# ══════════════════════════════════════════════════════════════════════════════

class TestDriftFlagsNotDemotes:
    """
    Plan acceptance test: injected drift → alert + report flag, zero state mutation.

    Verifies the A8 invariant that DriftDetector._handle_drift_action() writes
    the advisory flag to governance_metadata but never touches deployment_state.
    """

    @pytest.mark.asyncio
    async def test_drift_sets_challenger_recommended(self) -> None:
        """governance_metadata["challenger_recommended"] becomes True on drift."""
        ai  = _ai_model(deployment_state="live")
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        # High-z predictions (mean = 0.80 vs baseline mean = 0.50, std = 0.10 → z = 3.0)
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            report = await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        assert report.drift_detected is True
        assert ai.governance_metadata["challenger_recommended"] is True
        assert "drift_recommendation" in ai.governance_metadata

    @pytest.mark.asyncio
    async def test_deployment_state_never_mutated(self) -> None:
        """deployment_state must be identical before and after a drift check."""
        ai  = _ai_model(deployment_state="live")
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        original_state = ai.deployment_state
        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        assert ai.deployment_state == original_state, (
            "DriftDetector must never mutate deployment_state (A8 invariant)"
        )

    @pytest.mark.asyncio
    async def test_action_taken_is_drift_flagged(self) -> None:
        """AIDriftReport.action_taken must be 'drift_flagged', never a state name."""
        ai  = _ai_model(deployment_state="live")
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            report = await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        assert report.action_taken == "drift_flagged"
        assert report.action_taken not in ("paper", "shadow", "retired", "live")

    @pytest.mark.asyncio
    async def test_redis_alert_published_on_drift(self) -> None:
        """Redis MODELS_DRIFT_ALERTS channel receives a structured alert on drift."""
        ai  = _ai_model(deployment_state="live")
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        ps.publish_json.assert_called_once()
        channel, payload = ps.publish_json.call_args[0]
        from app.core.redis import RedisChannels
        assert channel == RedisChannels.MODELS_DRIFT_ALERTS
        assert payload["model_id"]    == ai.id
        assert payload["model_name"]  == ai.model_name
        assert "triggered_signals"    in payload

    @pytest.mark.asyncio
    async def test_no_alert_when_no_drift(self) -> None:
        """Redis alert must NOT be published when no signal triggers."""
        ai  = _ai_model(deployment_state="live")
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        # On-baseline predictions — z_score ≈ 0
        preds = [_ml_prediction(0.51)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            report = await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        assert report.drift_detected is False
        ps.publish_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_existing_governance_metadata_preserved(self) -> None:
        """Pre-existing governance_metadata keys are not clobbered by the flag write."""
        existing_meta = {"admin_overrides": [{"forced_at": "2026-05-01T00:00:00Z"}]}
        ai  = _ai_model(deployment_state="live", governance_metadata=existing_meta)
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()

        detector = DriftDetector()
        with patch.object(detector, "_emit_prometheus"):
            await detector.check_drift(db=db, pubsub=ps, model_id=ai.id)

        # Existing key must survive the flag write.
        assert "admin_overrides" in ai.governance_metadata


# ══════════════════════════════════════════════════════════════════════════════
# 2 — Distribution shift signal
# ══════════════════════════════════════════════════════════════════════════════

class TestDistributionShiftSignal:

    def _detector(self) -> DriftDetector:
        return DriftDetector()

    def test_high_zscore_triggers(self) -> None:
        ml    = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50  # z_score = (0.80-0.50)/0.10 = 3.0
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] == pytest.approx(3.0, abs=0.01)

    def test_on_baseline_no_trigger(self) -> None:
        ml    = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.51)] * 50  # z_score ≈ 0.1
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] < 2.0

    def test_zscore_capped_at_10(self) -> None:
        ml    = _ml_meta(training_prediction_stats={"mean": 0.0, "std": 0.01})
        # Extremely shifted — (1.0 - 0.0) / 0.01 = 100 → capped at 10
        preds = [_ml_prediction(1.0)] * 50
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] == pytest.approx(10.0, abs=0.01)

    def test_insufficient_data_returns_zero_zscore(self) -> None:
        ml    = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 5  # < 10 → no signal
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] == 0.0
        assert result["current_mean"] is None

    def test_zero_baseline_std_returns_zero_zscore(self) -> None:
        ml    = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.0})
        preds = [_ml_prediction(0.80)] * 50
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] == 0.0

    def test_legacy_nested_stats_format(self) -> None:
        """Backward compat: training_prediction_stats may use nested 'raw_predictions' key."""
        ml    = _ml_meta(training_prediction_stats={
            "raw_predictions": {"mean": 0.50, "std": 0.10}
        })
        preds = [_ml_prediction(0.80)] * 50
        det   = self._detector()
        result = det._compute_distribution_shift(preds, ml)
        assert result["z_score"] == pytest.approx(3.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# 3 — Realised directional accuracy signal
# ══════════════════════════════════════════════════════════════════════════════

class TestRealisedDirectionalAccuracy:

    @pytest.mark.asyncio
    async def test_accuracy_computed_from_outcomes(self) -> None:
        """Fraction correct matches the mock outcome data exactly."""
        ml = _ml_meta()
        outcomes = (
            [_outcome(net_pnl=100, entry_price=1000, quantity=10, ml_direction_correct=True)]  * 7
            + [_outcome(net_pnl=-50, entry_price=1000, quantity=10, ml_direction_correct=False)] * 3
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["realised_accuracy"] == pytest.approx(0.70, abs=0.001)
        assert result["realised_trade_count"] == 10

    @pytest.mark.asyncio
    async def test_no_outcomes_returns_none_accuracy(self) -> None:
        ml = _ml_meta()
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result([]))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["realised_accuracy"] is None
        assert result["realised_trade_count"] == 0
        assert result["min_samples_met"] is False

    @pytest.mark.asyncio
    async def test_min_samples_gate(self) -> None:
        """min_samples_met is False when trade count < _MIN_TRADES_FOR_REALISED_METRICS."""
        ml = _ml_meta()
        outcomes = [_outcome(net_pnl=10, entry_price=100, quantity=1, ml_direction_correct=True)] * (
            _MIN_TRADES_FOR_REALISED_METRICS - 1
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["min_samples_met"] is False

    @pytest.mark.asyncio
    async def test_min_samples_exactly_met(self) -> None:
        ml = _ml_meta()
        outcomes = [_outcome(net_pnl=10, entry_price=100, quantity=1, ml_direction_correct=True)] * (
            _MIN_TRADES_FOR_REALISED_METRICS
        )
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["min_samples_met"] is True

    @pytest.mark.asyncio
    async def test_deployed_at_filter_sent_in_query(self) -> None:
        """When deployed_at is set, 'since' label reflects it."""
        dep = datetime(2026, 5, 1, tzinfo=_UTC)
        ml  = _ml_meta(deployed_at=dep)
        db  = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result([]))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["since"] == dep.isoformat()

    @pytest.mark.asyncio
    async def test_no_deployed_at_returns_all_time(self) -> None:
        ml  = _ml_meta(deployed_at=None)
        db  = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result([]))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["since"] == "all_time"


# ══════════════════════════════════════════════════════════════════════════════
# 4 — Realised net information ratio signal
# ══════════════════════════════════════════════════════════════════════════════

class TestRealisedNetInformationRatio:

    @pytest.mark.asyncio
    async def test_positive_ir_computed_correctly(self) -> None:
        """IR = mean/std of per-trade returns, verified analytically."""
        import statistics as _st
        ml = _ml_meta()
        # Returns: [+1%, +1%, +1%] → mean=0.01, std=0, IR=None (zero variance)
        # Use varied returns to get a non-trivial IR.
        outcomes = [
            _outcome(net_pnl=20,  entry_price=1000, quantity=1, ml_direction_correct=True),
            _outcome(net_pnl=10,  entry_price=1000, quantity=1, ml_direction_correct=True),
            _outcome(net_pnl=15,  entry_price=1000, quantity=1, ml_direction_correct=True),
            _outcome(net_pnl=5,   entry_price=1000, quantity=1, ml_direction_correct=False),
        ]
        rets = [20/1000, 10/1000, 15/1000, 5/1000]
        expected_ir = _st.mean(rets) / _st.stdev(rets)
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["realised_information_ratio"] == pytest.approx(expected_ir, rel=1e-4)

    @pytest.mark.asyncio
    async def test_negative_ir_triggers_threshold(self) -> None:
        """Information ratio below _IR_DETERIORATION_THRESHOLD must trigger."""
        ml = _ml_meta()
        # All losing trades → negative mean, small std → very negative IR
        outcomes = [
            _outcome(net_pnl=-50, entry_price=1000, quantity=1, ml_direction_correct=False),
            _outcome(net_pnl=-60, entry_price=1000, quantity=1, ml_direction_correct=False),
            _outcome(net_pnl=-40, entry_price=1000, quantity=1, ml_direction_correct=False),
        ] * 10  # 30 trades to meet _MIN_TRADES_FOR_REALISED_METRICS
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["realised_information_ratio"] is not None
        assert result["realised_information_ratio"] <= _IR_DETERIORATION_THRESHOLD

    @pytest.mark.asyncio
    async def test_zero_variance_returns_none_ir(self) -> None:
        """Identical returns produce std=0; IR must be None, not a division error."""
        ml = _ml_meta()
        outcomes = [
            _outcome(net_pnl=10, entry_price=1000, quantity=1, ml_direction_correct=True)
        ] * 5
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result(outcomes))
        det = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        assert result["realised_information_ratio"] is None  # single-element stdev = 0

    @pytest.mark.asyncio
    async def test_zero_notional_rows_skipped(self) -> None:
        """Rows with zero notional are excluded from return computation (no ZeroDivisionError)."""
        ml = _ml_meta()
        bad  = _outcome(net_pnl=10, entry_price=0, quantity=1, ml_direction_correct=True)
        good = _outcome(net_pnl=10, entry_price=1000, quantity=1, ml_direction_correct=True)
        db   = AsyncMock()
        db.execute = AsyncMock(return_value=_rows_result([bad, good]))
        det  = DriftDetector()
        result = await det._compute_realised_metrics(db, ml)
        # Only 1 valid return → stdev undefined → IR = None, but no crash
        assert result["realised_information_ratio"] is None


# ══════════════════════════════════════════════════════════════════════════════
# 5 — Composite signal: any trigger suffices
# ══════════════════════════════════════════════════════════════════════════════

class TestCompositeSignalLogic:

    @pytest.mark.asyncio
    async def test_only_shift_triggers_drift(self) -> None:
        """Distribution shift alone (z > threshold) must set drift_detected."""
        ai  = _ai_model()
        ml  = _ml_meta(
            training_metrics={"accuracy": 0.60},
            training_prediction_stats={"mean": 0.50, "std": 0.10},
        )
        # High z-score predictions; no outcomes (realised signals skip)
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus"):
            report = await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        assert report.drift_detected is True
        assert report.distribution_metrics["distribution_shift"]["triggered"] is True

    @pytest.mark.asyncio
    async def test_all_clear_no_drift(self) -> None:
        """All signals below threshold → drift_detected must be False."""
        ai  = _ai_model()
        ml  = _ml_meta(
            training_metrics={"accuracy": 0.60},
            training_prediction_stats={"mean": 0.50, "std": 0.10},
        )
        preds = [_ml_prediction(0.51)] * 50  # z ≈ 0.1
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus"):
            report = await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        assert report.drift_detected is False
        assert report.action_taken is None

    @pytest.mark.asyncio
    async def test_distribution_metrics_has_all_three_signal_keys(self) -> None:
        """Every drift report must carry all three signal sections."""
        ai  = _ai_model()
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.51)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus"):
            report = await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        dm = report.distribution_metrics
        assert "distribution_shift" in dm
        assert "realised_accuracy"  in dm
        assert "realised_net_ir"    in dm

    @pytest.mark.asyncio
    async def test_triggered_signals_recorded_in_governance_metadata(self) -> None:
        """When drift fires, governance_metadata.drift_recommendation.triggered_signals lists causes."""
        ai  = _ai_model()
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50  # high z-score → shift triggers
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus"):
            await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        signals = ai.governance_metadata["drift_recommendation"]["triggered_signals"]
        assert "distribution_shift" in signals


# ══════════════════════════════════════════════════════════════════════════════
# 6 — Prometheus metrics emission
# ══════════════════════════════════════════════════════════════════════════════

class TestPrometheusMetricsEmission:

    @pytest.mark.asyncio
    async def test_prometheus_called_on_every_check(self) -> None:
        """_emit_prometheus is called exactly once per check regardless of drift result."""
        ai  = _ai_model()
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.51)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus") as mock_emit:
            await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        mock_emit.assert_called_once()

    @pytest.mark.asyncio
    async def test_prometheus_receives_correct_model_name(self) -> None:
        ai  = _ai_model(model_name="cortex_gru_1d")
        # model_name must be a substring of ai.model_name for the resolver to match
        ml  = _ml_meta(
            model_name="gru",
            training_prediction_stats={"mean": 0.50, "std": 0.10},
        )
        preds = [_ml_prediction(0.51)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus") as mock_emit:
            await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        assert mock_emit.called, "_emit_prometheus must be called on every check"
        _, kwargs = mock_emit.call_args
        assert kwargs["model_name"] == "cortex_gru_1d"

    @pytest.mark.asyncio
    async def test_prometheus_drift_flagged_true_on_drift(self) -> None:
        ai  = _ai_model()
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.80)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus") as mock_emit:
            await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        _, kwargs = mock_emit.call_args
        assert kwargs["drift_flagged"] is True

    @pytest.mark.asyncio
    async def test_prometheus_drift_flagged_false_on_clear(self) -> None:
        ai  = _ai_model()
        ml  = _ml_meta(training_prediction_stats={"mean": 0.50, "std": 0.10})
        preds = [_ml_prediction(0.51)] * 50
        db    = _make_db(ai_model=ai, production_ml_records=[ml], ml_predictions=preds)
        ps    = _make_pubsub()
        det   = DriftDetector()
        with patch.object(det, "_emit_prometheus") as mock_emit:
            await det.check_drift(db=db, pubsub=ps, model_id=ai.id)
        _, kwargs = mock_emit.call_args
        assert kwargs["drift_flagged"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 7 — Drift advisory in C2 Promotion Report
# ══════════════════════════════════════════════════════════════════════════════

def _report_model(
    *,
    name: str = "xgboost",
    version: str = "1.1.0_xgboost",
    onnx: str | None = None,
    features: list | None = None,
    metrics: dict | None = None,
) -> MagicMock:
    m = MagicMock()
    m.model_name        = name
    m.model_version     = version
    m.status            = "development"
    m.is_active         = False
    # A6-passing metrics so quality gate does not block the report (AWAITING_HUMAN_SIGNOFF).
    ap_floor = {"xgboost": 0.60, "gru": 0.55}.get(name, 0.60)
    m.training_metrics  = metrics or {
        "accuracy":           0.65,
        "f1_score":           0.62,
        "auc_pr":             ap_floor + 0.05,
        "deflated_sharpe":    1.35,
        "ece_after":          0.03,
        "symbol_coverage":    0.92,
        "pbo":                0.12,
        "path_sharpes":       [0.8, 1.1, 0.9, 1.3, 1.0, 0.7, 1.2],
        "ensemble_net_dsr":   1.20,
        "ensemble_accretive": True,
    }
    m.training_features = features or []
    m.onnx_path         = onnx
    m.training_samples  = 3_000_000
    m.lineage           = {}
    m.trained_at        = datetime(2026, 5, 24, tzinfo=_UTC)
    m.deployed_at       = None
    return m


class TestDriftAdvisoryInPromotionReport:

    def test_drift_advisory_present_in_report_body(self, tmp_path: Path) -> None:
        """drift_advisory key must appear in the written report JSON."""
        chal  = {"xgboost": _report_model()}
        champ = {"xgboost": None}
        adv   = {"xgboost": {"challenger_recommended": False, "last_check": None}}
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=adv,
        )
        assert "drift_advisory" in report
        assert report["drift_advisory"] == adv

    def test_none_drift_advisory_serialises_as_empty_dict(self, tmp_path: Path) -> None:
        """None drift_advisory must be stored as {} (never absent from report body)."""
        chal  = {"xgboost": _report_model()}
        champ = {"xgboost": None}
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=None,
        )
        assert report["drift_advisory"] == {}

    def test_drift_advisory_covered_by_body_hash(self, tmp_path: Path) -> None:
        """Changing drift_advisory must produce a different body SHA-256."""
        chal  = {"xgboost": _report_model()}
        champ = {"xgboost": None}

        r1 = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path / "r1", drift_advisory=None,
        )
        r2 = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path / "r2",
            drift_advisory={"xgboost": {"challenger_recommended": True}},
        )

        sha1 = r1["bundle_manifest"]["report_body"]["sha256"]
        sha2 = r2["bundle_manifest"]["report_body"]["sha256"]
        assert sha1 != sha2, (
            "drift_advisory must be covered by the report body hash — "
            "tampered drift advisory data must produce a different digest"
        )

    def test_flagged_drift_advisory_appears_in_operator_guidance(self, tmp_path: Path) -> None:
        """operator_guidance must call out champion models with challenger_recommended=True."""
        chal  = {"xgboost": _report_model()}
        champ = {"xgboost": None}
        adv   = {
            "xgboost": {
                "challenger_recommended": True,
                "drift_recommendation": {
                    "recommended_state": "paper",
                    "flagged_at": "2026-05-23T13:00:00+00:00",
                    "triggered_signals": ["distribution_shift"],
                },
            }
        }
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=adv,
        )
        guidance = report["operator_guidance"]
        assert "DRIFT ADVISORY" in guidance
        assert "distribution_shift" in guidance

    def test_no_drift_no_advisory_section_in_guidance(self, tmp_path: Path) -> None:
        """When no champion has challenger_recommended, no drift section in guidance."""
        chal  = {"xgboost": _report_model()}
        champ = {"xgboost": None}
        adv   = {"xgboost": {"challenger_recommended": False}}
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=adv,
        )
        assert "DRIFT ADVISORY" not in report["operator_guidance"]

    def test_report_roundtrip_with_drift_advisory(self, tmp_path: Path) -> None:
        """load_and_verify_report must pass on a report that contains drift_advisory."""
        chal  = {"xgboost": _report_model(version="2.0.0_xgboost")}
        champ = {"xgboost": None}
        adv   = {"xgboost": {"challenger_recommended": False, "last_check": None}}
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=adv,
        )
        assert report["status"] == PromotionStatus.AWAITING_HUMAN_SIGNOFF.value, (
            "Report must be AWAITING_HUMAN_SIGNOFF for a clean roundtrip verify — "
            "check that _report_model() supplies A6-passing metrics"
        )
        path = Path(report["report_path"])
        verified = load_and_verify_report(path, challenger_version="2.0.0_xgboost")
        assert verified["drift_advisory"] == adv

    def test_tampered_drift_advisory_detected(self, tmp_path: Path) -> None:
        """Modifying drift_advisory on disk after generation must fail bundle check."""
        from app.ml.evaluation.promotion_report import BundleChecksumError
        chal  = {"xgboost": _report_model(version="3.0.0_xgboost")}
        champ = {"xgboost": None}
        adv   = {"xgboost": {"challenger_recommended": False}}
        report = build_promotion_report(
            challengers=chal, champions=champ,
            report_dir=tmp_path, drift_advisory=adv,
        )
        path = Path(report["report_path"])
        data = json.loads(path.read_text())
        # Tamper
        data["drift_advisory"]["xgboost"]["challenger_recommended"] = True
        path.write_text(json.dumps(data))

        with pytest.raises(BundleChecksumError):
            load_and_verify_report(path, challenger_version="3.0.0_xgboost")


# ══════════════════════════════════════════════════════════════════════════════
# 8 — Baseline fallback (no production ML metadata)
# ══════════════════════════════════════════════════════════════════════════════

class TestBaselineFallback:

    @pytest.mark.asyncio
    async def test_baseline_drift_detected_is_false(self) -> None:
        """_baseline_drift_check must always return drift_detected=False."""
        ai = _ai_model()
        db = AsyncMock()
        db.add    = MagicMock()
        db.commit  = AsyncMock()
        db.refresh = AsyncMock()
        ps = _make_pubsub()

        det    = DriftDetector()
        report = await det._baseline_drift_check(db, ps, ai)
        assert report.drift_detected is False

    @pytest.mark.asyncio
    async def test_baseline_no_redis_alert(self) -> None:
        """Baseline check must never publish a Redis alert."""
        ai = _ai_model()
        db = AsyncMock()
        db.add    = MagicMock()
        db.commit  = AsyncMock()
        db.refresh = AsyncMock()
        ps = _make_pubsub()

        det = DriftDetector()
        await det._baseline_drift_check(db, ps, ai)
        ps.publish_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_baseline_action_taken_is_none(self) -> None:
        ai = _ai_model()
        db = AsyncMock()
        db.add    = MagicMock()
        db.commit  = AsyncMock()
        db.refresh = AsyncMock()
        ps = _make_pubsub()

        det    = DriftDetector()
        report = await det._baseline_drift_check(db, ps, ai)
        assert report.action_taken is None

    @pytest.mark.asyncio
    async def test_baseline_distribution_metrics_has_note(self) -> None:
        """Baseline report must document that only governance metadata is available."""
        ai = _ai_model()
        db = AsyncMock()
        db.add    = MagicMock()
        db.commit  = AsyncMock()
        db.refresh = AsyncMock()
        ps = _make_pubsub()

        det    = DriftDetector()
        report = await det._baseline_drift_check(db, ps, ai)
        assert report.distribution_metrics.get("baseline_only") is True
        assert "note" in report.distribution_metrics


# ══════════════════════════════════════════════════════════════════════════════
# Private helpers
# ══════════════════════════════════════════════════════════════════════════════

def _rows_result(rows: list) -> MagicMock:
    """Build a mock SQLAlchemy execute() result that returns *rows* from .all()."""
    result = MagicMock()
    result.all.return_value = rows
    return result
