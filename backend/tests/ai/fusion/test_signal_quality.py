"""
Integration tests — Signal Quality Checklist
============================================
Covers all 12 items in SIGNAL_QUALITY_PLAN.md § Integration Test Checklist.

Items 1–5   Weight renormalization (dynamic source exclusion)
Item  6     Event aggregation 1/√N normalization
Item  7     Two-component Hawkes temporal decay
Items 8–9   Calibration ECE < 0.05 (XGBoost Beta cal + GRU temperature scaling)
Items 10–12 Conviction scale flows predictor → qty_suggester (graduated sizing)
"""
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

from app.ai.fusion.signal_assembler import SignalAssembler
from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.services.paper_trading.qty_suggester import suggest_quantity


# ── Shared helpers ─────────────────────────────────────────────────────────────

_BASE_WEIGHTS = dict(event_weight=0.35, ml_weight=0.40, technical_weight=0.25)


def _avail(score: float, confidence: float) -> dict:
    """Build an 'available' signal source dict."""
    return {"score": score, "confidence": confidence, "available": True, "events": []}


def _unavail() -> dict:
    """Build an 'unavailable' signal source dict (no predictor / insufficient data)."""
    return {"score": 0.0, "confidence": 0.0, "available": False, "events": []}


class _ConstantBackend:
    """Deterministic model backend — always returns the same probabilities."""

    def __init__(self, p_down: float, p_up: float) -> None:
        self._proba = np.array([p_down, p_up], dtype=np.float32)

    def predict(self, input_data: np.ndarray) -> np.ndarray:
        return np.tile(self._proba, (input_data.shape[0], 1))


def _make_predictor(p_down: float = 0.2, p_up: float = 0.8) -> EnsemblePredictor:
    return EnsemblePredictor(
        xgboost_backend=_ConstantBackend(p_down, p_up),
        gru_backend=_ConstantBackend(p_down, p_up),
        xgboost_weight=0.75,
        gru_weight=0.25,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Items 1–5 — Weight Renormalization
# ══════════════════════════════════════════════════════════════════════════════

class TestWeightRenormalization:
    """Dynamic weight renormalization across all source-availability combinations."""

    def _assembler(self) -> SignalAssembler:
        return SignalAssembler(**_BASE_WEIGHTS)

    def test_all_sources_available_exact_weighted_sum(self):
        """Item 1 — all 3 available: fused_score == 0.35×E + 0.40×M + 0.25×T."""
        a = self._assembler()
        result = a.fuse_signals(
            _avail(score=50.0, confidence=0.7),
            _avail(score=80.0, confidence=0.9),
            _avail(score=40.0, confidence=0.6),
        )
        # 0.35×50 + 0.40×80 + 0.25×40 = 17.5 + 32.0 + 10.0 = 59.5
        assert abs(result["fused_score"] - 59.5) < 1e-9
        assert result["sources_available"] == 3

    def test_ml_unavailable_renormalizes_to_event_and_technical(self):
        """Item 2 — ML unavail: remaining weights renormalize to event=7/12, tech=5/12."""
        a = self._assembler()
        result = a.fuse_signals(
            _avail(score=60.0, confidence=0.8),
            _unavail(),
            _avail(score=40.0, confidence=0.6),
        )
        # Denominator = 0.35 + 0.25 = 0.60
        expected_score = (0.35 / 0.60) * 60.0 + (0.25 / 0.60) * 40.0
        assert abs(result["fused_score"] - expected_score) < 1e-9
        assert result["sources_available"] == 2

    def test_technical_unavailable_renormalizes_to_event_and_ml(self):
        """Item 3 — technical unavail (<52 candles): event+ML renormalize to 0.75 denom."""
        a = self._assembler()
        result = a.fuse_signals(
            _avail(score=60.0, confidence=0.8),
            _avail(score=80.0, confidence=0.9),
            _unavail(),
        )
        # Denominator = 0.35 + 0.40 = 0.75
        expected_score = (0.35 / 0.75) * 60.0 + (0.40 / 0.75) * 80.0
        assert abs(result["fused_score"] - expected_score) < 1e-9
        assert result["sources_available"] == 2

    def test_all_sources_unavailable_returns_neutral_hold(self):
        """Item 4 — all unavailable: fused_score=0.0, confidence=0.0, action=HOLD."""
        a = self._assembler()
        result = a.fuse_signals(_unavail(), _unavail(), _unavail())
        assert result["action"] == "HOLD"
        assert result["fused_score"] == 0.0
        assert result["confidence_score"] == 0.0
        assert result["sources_available"] == 0

    def test_single_source_confidence_capped_at_0_65(self):
        """Item 5 — one source with confidence=0.92; fused confidence capped to ≤ 0.65."""
        a = self._assembler()
        result = a.fuse_signals(
            {"score": 80.0, "confidence": 0.92, "available": True, "events": []},
            _unavail(),
            _unavail(),
        )
        assert result["confidence_score"] <= 0.65
        assert result["sources_available"] == 1

    def test_renormalized_weights_always_sum_to_one(self):
        """Renormalized weights must sum to 1.0 regardless of which sources are missing."""
        a = self._assembler()
        for event_avail, ml_avail, tech_avail in [
            (True, True, False),
            (True, False, True),
            (False, True, True),
            (True, False, False),
            (False, True, False),
            (False, False, True),
        ]:
            sources = {
                "event":     (0.35, event_avail),
                "ml":        (0.40, ml_avail),
                "technical": (0.25, tech_avail),
            }
            weights = SignalAssembler._renormalize_weights(sources)
            total = sum(weights.values())
            assert abs(total - 1.0) < 1e-9, (
                f"Weights summed to {total} for availability={event_avail, ml_avail, tech_avail}"
            )


# ── Helpers for item 6 — DB mock for gather_event_signals ────────────────────

class _MockEventRow:
    """
    Minimal mock of an AIEventClassification + joined columns row.

    age_hours defaults to 0.001 so decay ≈ 1.0, meaning
    decayed_score ≈ impact_score — the aggregation math is easy to verify.
    """
    _counter: int = 0

    def __init__(
        self,
        impact_score: float,
        fast_hl: int = 12,
        slow_hl: int = 72,
        confidence: float = 0.8,
        age_hours: float = 0.001,
    ) -> None:
        _MockEventRow._counter += 1
        self.id = _MockEventRow._counter
        self.impact_score = impact_score
        self.decay_half_life_hours = fast_hl
        self.decay_slow_half_life_hours = slow_hl
        self.classification_confidence = confidence
        self.created_at = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        self.event_type = "market_data"
        self.source_url = None
        self.source_name = None
        self.article_title = None


def _mock_db(rows: list) -> AsyncMock:
    """Return an AsyncSession mock whose execute().all() yields `rows`."""
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result
    return mock_db


# ══════════════════════════════════════════════════════════════════════════════
# Item 6 — Event Aggregation 1/√N Normalization
# ══════════════════════════════════════════════════════════════════════════════

class TestEventAggregationNormalization:
    """
    Item 6: 1/√N dampening in the production gather_event_signals() code path.

    All tests call SignalAssembler.gather_event_signals() with a mocked AsyncSession
    so that the real aggregation logic in signal_assembler.py is exercised directly.
    """

    async def test_one_high_impact_outweighs_ten_low_impact(self):
        """Item 6 — production path: 10×10-impact score < 1×100-impact score."""
        assembler = SignalAssembler()

        ten_rows = [_MockEventRow(impact_score=10.0) for _ in range(10)]
        one_row  = [_MockEventRow(impact_score=100.0)]

        ten_result = await assembler.gather_event_signals(_mock_db(ten_rows), "TEST")
        one_result = await assembler.gather_event_signals(_mock_db(one_row),  "TEST")

        # Both raw sums clip to ~100; after √N normalization:
        #   10 events: ~100 / √10 ≈ 31.6    1 event: ~100 / √1 ≈ 100.0
        assert one_result["score"] > ten_result["score"]
        assert ten_result["available"] is True
        assert one_result["available"] is True

    async def test_clip_prevents_unbounded_growth(self):
        """5×50-impact events: raw_sum=250 clipped to 100 before √5 division."""
        assembler = SignalAssembler()
        rows = [_MockEventRow(impact_score=50.0) for _ in range(5)]
        result = await assembler.gather_event_signals(_mock_db(rows), "TEST")
        # At age≈0 decay≈1.0, so score ≈ 100/√5 ≈ 44.72; allow small decay tolerance
        expected = 100.0 / math.sqrt(5)
        assert abs(result["score"] - expected) < 0.1

    async def test_normalization_monotone_with_per_event_impact(self):
        """Higher per-event impact → higher normalized score from gather_event_signals."""
        assembler = SignalAssembler()
        low_rows  = [_MockEventRow(impact_score=20.0) for _ in range(3)]
        high_rows = [_MockEventRow(impact_score=40.0) for _ in range(3)]
        low_result  = await assembler.gather_event_signals(_mock_db(low_rows),  "TEST")
        high_result = await assembler.gather_event_signals(_mock_db(high_rows), "TEST")
        assert high_result["score"] > low_result["score"]

    async def test_no_events_returns_unavailable_zero(self):
        """Empty DB result must return available=False and score=0.0 (no ZeroDivisionError)."""
        assembler = SignalAssembler()
        result = await assembler.gather_event_signals(_mock_db([]), "TEST")
        assert result["available"] is False
        assert result["score"] == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# Item 7 — Two-Component Hawkes Temporal Decay
# ══════════════════════════════════════════════════════════════════════════════

class TestTwoComponentDecay:
    """Item 7: two-component decay accuracy for earnings-type events."""

    # Earnings canonical half-lives from event_classifier._DECAY_HALF_LIVES
    _FAST_HL = 12.0
    _SLOW_HL = 72.0

    def test_earnings_decay_at_12h_vs_48h(self):
        """Item 7 — decay(12h) > decay(48h) for earnings event; both match formula exactly."""
        d12 = SignalAssembler.calculate_event_decay(12.0, self._FAST_HL, self._SLOW_HL)
        d48 = SignalAssembler.calculate_event_decay(48.0, self._FAST_HL, self._SLOW_HL)

        expected_12 = 0.7 * math.pow(0.5, 12.0 / 12.0) + 0.3 * math.pow(0.5, 12.0 / 72.0)
        expected_48 = 0.7 * math.pow(0.5, 48.0 / 12.0) + 0.3 * math.pow(0.5, 48.0 / 72.0)

        assert abs(d12 - expected_12) < 1e-12
        assert abs(d48 - expected_48) < 1e-12
        assert d12 > d48 > 0.0

    def test_decay_at_t0_is_one(self):
        """At t=0 both components equal 1.0 → total decay = 1.0."""
        assert abs(SignalAssembler.calculate_event_decay(0.0, 12.0, 72.0) - 1.0) < 1e-12

    def test_slow_component_preserves_signal_at_late_t(self):
        """Slow Hawkes component retains residual signal that pure fast decay extinguishes."""
        t = 120.0  # 5 days out
        two_comp = SignalAssembler.calculate_event_decay(t, self._FAST_HL, self._SLOW_HL)
        pure_fast = math.pow(0.5, t / self._FAST_HL)  # 0.5^10 ≈ 9.77e-4
        # Two-component must retain meaningfully more than pure fast decay
        assert two_comp > pure_fast * 5


# ══════════════════════════════════════════════════════════════════════════════
# Items 8–9 — Calibration ECE
# ══════════════════════════════════════════════════════════════════════════════

class TestCalibrationECE:
    """Items 8–9: post-hoc calibration achieves ECE < 0.05 on synthetic validation sets."""

    _N = 3_000
    _SEED = 42

    def _xgboost_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Bimodal overconfident scores: positives cluster near 0.9, negatives near 0.1."""
        rng = np.random.default_rng(self._SEED)
        n = self._N // 2
        raw_pos = rng.beta(a=18, b=2,  size=n)  # mean ≈ 0.90
        raw_neg = rng.beta(a=2,  b=18, size=n)  # mean ≈ 0.10
        y_true  = np.concatenate([np.ones(n), np.zeros(n)]).astype(int)
        y_proba = np.concatenate([raw_pos, raw_neg])
        return y_true, y_proba

    def _gru_data(self) -> tuple[np.ndarray, np.ndarray]:
        """Overconfident GRU softmax: raw proba from sigmoid(logit), true labels from sigmoid(logit/2)."""
        rng    = np.random.default_rng(self._SEED)
        logits = rng.normal(loc=0.0, scale=2.0, size=self._N)
        raw_proba  = 1.0 / (1.0 + np.exp(-logits))
        true_proba = 1.0 / (1.0 + np.exp(-logits / 2.0))  # temperature=2 ground truth
        y_true = (rng.uniform(size=self._N) < true_proba).astype(int)
        return y_true, raw_proba

    def test_xgboost_beta_calibration_ece_below_0_05(self):
        """Item 8 — Beta calibration reduces ECE to < 0.05 on bimodal XGBoost scores."""
        y_true, y_proba = self._xgboost_data()
        cal = ConfidenceCalibrator("xgboost")
        cal.fit(y_true, y_proba)
        assert cal.ece_after < 0.05, (
            f"Beta calibration ECE = {cal.ece_after:.4f} (target < 0.05)"
        )

    def test_gru_temperature_scaling_ece_below_0_05(self):
        """Item 9 — Temperature scaling reduces ECE to < 0.05 on overconfident GRU scores."""
        y_true, y_proba = self._gru_data()
        cal = ConfidenceCalibrator("gru")
        cal.fit(y_true, y_proba)
        assert cal.ece_after < 0.05, (
            f"Temperature scaling ECE = {cal.ece_after:.4f} (target < 0.05)"
        )

    def test_calibration_strictly_reduces_ece(self):
        """Both calibrators must improve ECE relative to uncalibrated baseline."""
        for model_type, data_fn in [
            ("xgboost", self._xgboost_data),
            ("gru",     self._gru_data),
        ]:
            y_true, y_proba = data_fn()
            cal = ConfidenceCalibrator(model_type)
            cal.fit(y_true, y_proba)
            assert cal.ece_after < cal.ece_before, (
                f"{model_type}: ECE did not improve ({cal.ece_before:.4f} → {cal.ece_after:.4f})"
            )


# ══════════════════════════════════════════════════════════════════════════════
# Items 10–12 — Conviction Scale → Position Sizing
# ══════════════════════════════════════════════════════════════════════════════

class TestConvictionScale:
    """Items 10–12: conviction_scale produced by _post_process flows through qty_suggester."""

    @staticmethod
    def _post_process(p_up: float, volatility: float) -> dict:
        predictor = _make_predictor(p_down=round(1.0 - p_up, 8), p_up=p_up)
        return predictor._post_process(
            probabilities=np.array([round(1.0 - p_up, 8), p_up], dtype=np.float32),
            current_price=1000.0,
            volatility=volatility,
            symbol="TEST_SYM",
            timeframe="1d",
        )

    # ── Threshold regime selection ─────────────────────────────────────────────

    def test_normal_vol_uses_threshold_0_60(self):
        result = self._post_process(p_up=0.80, volatility=0.25)
        assert result["threshold"] == 0.60

    def test_high_vol_regime_threshold_is_0_70(self):
        """Stressed regime (vol > 0.35): threshold raised to 0.70."""
        result = self._post_process(p_up=0.65, volatility=0.40)
        assert result["threshold"] == 0.70
        assert result["direction_label"] == "HOLD"

    def test_low_vol_regime_threshold_is_0_55(self):
        """Benign regime (vol < 0.20): threshold relaxed to 0.55."""
        result = self._post_process(p_up=0.60, volatility=0.15)
        assert result["threshold"] == 0.55
        assert result["direction_label"] == "BUY"

    # ── Conviction scale arithmetic ────────────────────────────────────────────

    def test_conviction_scale_formula_normal_regime(self):
        """Item 10 — p_up=0.80, vol=0.25 (normal, threshold=0.60) → scale=0.50."""
        result = self._post_process(p_up=0.80, volatility=0.25)
        # (0.80 - 0.60) / (1.0 - 0.60) = 0.20 / 0.40 = 0.50
        assert abs(result["conviction_scale"] - 0.50) < 1e-5

    def test_conviction_scale_zero_below_threshold(self):
        """Confidence below threshold (0.59 < 0.60) → HOLD override, scale=0.0."""
        result = self._post_process(p_up=0.59, volatility=0.25)
        assert result["conviction_scale"] == 0.0
        assert result["direction_label"] == "HOLD"

    def test_conviction_scale_one_at_full_confidence(self):
        """Item 12 (boundary) — p_up=1.0 → conviction_scale=1.0."""
        result = self._post_process(p_up=1.0, volatility=0.25)
        assert abs(result["conviction_scale"] - 1.0) < 1e-5

    def test_price_levels_preserved_on_hold_override(self):
        """HOLD override must not zero price levels — downstream risk display stays valid."""
        result = self._post_process(p_up=0.55, volatility=0.25)
        # p_up=0.55 < threshold=0.60 → overridden to HOLD
        assert result["direction_label"] == "HOLD"
        # Price levels must still reflect the underlying BUY direction
        assert result["stop_loss"] < result["entry_price"]
        assert result["tp1"] > result["entry_price"]

    # ── Position sizing integration ────────────────────────────────────────────

    def test_position_size_minimum_at_zero_conviction(self):
        """Item 11 — conviction_scale=0.0 → raw_qty×0=0 → clamped to 1 share."""
        result = suggest_quantity(
            current_cash=Decimal("100000"),
            risk_per_trade_pct=Decimal("2"),
            entry_price=Decimal("200"),
            stop_loss=Decimal("180"),       # stop_distance = 20
            conviction_scale=0.0,
        )
        # capital_at_risk = 100 000 × 0.02 = 2 000
        # raw_qty = 2 000 / 20 = 100 → × 0.0 → 0 → max(1, 0) = 1
        assert result.suggested_qty == 1

    def test_position_size_full_kelly_at_full_conviction(self):
        """Item 12 — conviction_scale=1.0 → full Kelly-sized quantity."""
        result = suggest_quantity(
            current_cash=Decimal("100000"),
            risk_per_trade_pct=Decimal("2"),
            entry_price=Decimal("200"),
            stop_loss=Decimal("180"),       # stop_distance = 20
            conviction_scale=1.0,
        )
        # capital_at_risk = 2 000; raw_qty = 100; max_affordable = 500
        # suggested = min(100, 500) = 100
        assert result.suggested_qty == 100

    def test_graduated_sizing_at_half_conviction(self):
        """Item 10 — 50% conviction → ~50% of full Kelly quantity."""
        full = suggest_quantity(
            current_cash=Decimal("100000"),
            risk_per_trade_pct=Decimal("2"),
            entry_price=Decimal("200"),
            stop_loss=Decimal("180"),
            conviction_scale=1.0,
        )
        half = suggest_quantity(
            current_cash=Decimal("100000"),
            risk_per_trade_pct=Decimal("2"),
            entry_price=Decimal("200"),
            stop_loss=Decimal("180"),
            conviction_scale=0.5,
        )
        assert half.suggested_qty < full.suggested_qty
        assert half.suggested_qty == 50   # floor(100 × 0.5) = 50
