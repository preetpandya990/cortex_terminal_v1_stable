"""
Unit tests for SignalAssembler
"""
import math

import pytest

from app.ai.fusion.signal_assembler import SignalAssembler


class TestSignalAssembler:
    """Unit tests for SignalAssembler construction and core logic."""

    # ── Initialisation ─────────────────────────────────────────────────────────

    def test_init_weights_sum_to_one(self):
        assembler = SignalAssembler()
        total = assembler.event_weight + assembler.ml_weight + assembler.technical_weight
        assert abs(total - 1.0) < 1e-9

    def test_init_invalid_weights_raises_value_error(self):
        with pytest.raises(ValueError):
            SignalAssembler(event_weight=0.5, ml_weight=0.3, technical_weight=0.1)

    # ── Two-component decay ────────────────────────────────────────────────────

    def test_decay_at_zero_age_is_one(self):
        """At t=0 both components equal 1.0 → decay=1.0."""
        decay = SignalAssembler.calculate_event_decay(0.0, fast_hl=12.0, slow_hl=72.0)
        assert abs(decay - 1.0) < 1e-9

    def test_decay_decreases_with_age(self):
        """Older events must produce strictly smaller decay factors."""
        d1 = SignalAssembler.calculate_event_decay(12.0, fast_hl=12.0, slow_hl=72.0)
        d2 = SignalAssembler.calculate_event_decay(48.0, fast_hl=12.0, slow_hl=72.0)
        assert d1 > d2 > 0.0

    def test_decay_formula_matches_hawkes_kernel(self):
        """Verify 0.7·0.5^(t/fast) + 0.3·0.5^(t/slow) exactly."""
        fast_hl, slow_hl, t = 12.0, 72.0, 24.0
        expected = 0.7 * math.pow(0.5, t / fast_hl) + 0.3 * math.pow(0.5, t / slow_hl)
        assert abs(SignalAssembler.calculate_event_decay(t, fast_hl, slow_hl) - expected) < 1e-12

    def test_decay_negative_age_raises_value_error(self):
        with pytest.raises(ValueError, match="negative"):
            SignalAssembler.calculate_event_decay(-1.0, fast_hl=12.0, slow_hl=72.0)

    def test_decay_zero_half_life_raises_value_error(self):
        with pytest.raises(ValueError):
            SignalAssembler.calculate_event_decay(1.0, fast_hl=0.0, slow_hl=72.0)

    # ── Signal fusion ──────────────────────────────────────────────────────────

    def test_fuse_signals_buy(self):
        """Score above +50 → BUY; weighted average is correct."""
        a = SignalAssembler(event_weight=0.4, ml_weight=0.4, technical_weight=0.2)
        e = {"score": 60.0, "confidence": 0.8, "events": []}
        m = {"score": 70.0, "confidence": 0.9}
        t = {"score": 50.0, "confidence": 0.7}
        result = a.fuse_signals(e, m, t)
        assert abs(result["fused_score"] - 62.0) < 1e-6   # 0.4*60 + 0.4*70 + 0.2*50
        assert result["action"] == "BUY"

    def test_fuse_signals_sell(self):
        a = SignalAssembler()
        e = {"score": -60.0, "confidence": 0.8, "events": []}
        m = {"score": -70.0, "confidence": 0.9}
        t = {"score": -50.0, "confidence": 0.7}
        assert a.fuse_signals(e, m, t)["action"] == "SELL"

    def test_fuse_signals_hold(self):
        a = SignalAssembler()
        e = {"score": 10.0, "confidence": 0.5, "events": []}
        m = {"score":  0.0, "confidence": 0.5}
        t = {"score":  5.0, "confidence": 0.5}
        assert a.fuse_signals(e, m, t)["action"] == "HOLD"

    def test_fuse_signals_exact_positive_boundary_is_buy(self):
        """fused_score == +50 exactly → BUY (inclusive threshold)."""
        a = SignalAssembler()
        e = {"score": 50.0, "confidence": 0.5, "events": []}
        m = {"score": 50.0, "confidence": 0.5}
        t = {"score": 50.0, "confidence": 0.5}
        result = a.fuse_signals(e, m, t)
        assert abs(result["fused_score"] - 50.0) < 1e-9
        assert result["action"] == "BUY"

    def test_fuse_signals_exact_negative_boundary_is_sell(self):
        """fused_score == -50 exactly → SELL (inclusive threshold)."""
        a = SignalAssembler()
        e = {"score": -50.0, "confidence": 0.5, "events": []}
        m = {"score": -50.0, "confidence": 0.5}
        t = {"score": -50.0, "confidence": 0.5}
        result = a.fuse_signals(e, m, t)
        assert abs(result["fused_score"] + 50.0) < 1e-9
        assert result["action"] == "SELL"

    def test_fuse_signals_just_inside_boundary_is_hold(self):
        """Scores strictly inside (-50, +50) remain HOLD."""
        a = SignalAssembler()
        for score, expected in ((49.99, "HOLD"), (-49.99, "HOLD")):
            e = {"score": score, "confidence": 0.5, "events": []}
            m = {"score": score, "confidence": 0.5}
            t = {"score": score, "confidence": 0.5}
            assert a.fuse_signals(e, m, t)["action"] == expected
