"""
Unit tests for the hit-probability kernel  P(hit TP before SL).

Covers the full contract of ``app.services.paper_trading.hit_probability``:

  • boundary values (price at/through a barrier → 1.0 / 0.0)
  • zero-edge limit equals the pure log-distance ratio
  • LONG/SHORT reflection symmetry
  • monotonicity: P rises as price → TP (LONG) and as the bullish edge grows
  • missing-barrier abstention (→ None), and neutral degrade on missing prob_up
  • structural / domain guards (invalid barriers, non-positive & non-finite price)
  • programmer-contract failures (bad edge_lambda, bad side → ValueError)
  • output is always a valid probability in [0, 1]

Hypothesis property tests assert the invariants across the whole input space, not
just hand-picked points.

Marked ``unit``: pure, no I/O.
"""
from __future__ import annotations

import math

import pytest
from hypothesis import given, settings, strategies as st

from app.schemas.paper_trading import PositionSide
from app.services.paper_trading.hit_probability import hit_tp_before_sl

pytestmark = pytest.mark.unit

LAMBDA = 3.0  # matches the default Settings.INSIGHT_EDGE_LAMBDA

# Finite, sensibly-bounded price/probability strategies for property tests.
_prices = st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)
_probs = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_lambdas = st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False)
_prop_settings = settings(max_examples=300, deadline=None)


# ──────────────────────────────────────────────────────────────────────────────
# Boundary values — price already at/through a barrier
# ──────────────────────────────────────────────────────────────────────────────

class TestBoundaryValues:
    def test_long_price_at_or_above_tp_is_certain(self):
        assert hit_tp_before_sl(110.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) == 1.0
        assert hit_tp_before_sl(120.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) == 1.0

    def test_long_price_at_or_below_sl_is_impossible(self):
        assert hit_tp_before_sl(95.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) == 0.0
        assert hit_tp_before_sl(90.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) == 0.0

    def test_short_price_at_or_below_tp_is_certain(self):
        # SHORT: TP below, SL above. Price at/under TP ⇒ target hit.
        assert hit_tp_before_sl(90.0, 90.0, 105.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA) == 1.0
        assert hit_tp_before_sl(85.0, 90.0, 105.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA) == 1.0

    def test_short_price_at_or_above_sl_is_impossible(self):
        assert hit_tp_before_sl(105.0, 90.0, 105.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA) == 0.0
        assert hit_tp_before_sl(110.0, 90.0, 105.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA) == 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Zero-edge limit == pure log-distance ratio
# ──────────────────────────────────────────────────────────────────────────────

class TestZeroEdgeDistanceRatio:
    @staticmethod
    def _distance_ratio_long(s: float, tp: float, sl: float) -> float:
        a = math.log(sl / s)
        b = math.log(tp / s)
        return -a / (b - a)

    def test_neutral_prob_equals_distance_ratio(self):
        p = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA)
        assert p == pytest.approx(self._distance_ratio_long(100.0, 110.0, 95.0), abs=1e-12)

    def test_lambda_zero_disables_ml_tilt(self):
        # With λ=0 the bullish prob_up must not move the estimate at all.
        p = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.9, edge_lambda=0.0)
        assert p == pytest.approx(self._distance_ratio_long(100.0, 110.0, 95.0), abs=1e-12)

    def test_none_prob_up_degrades_to_neutral(self):
        # Missing prob_up ⇒ ρ=0, i.e. identical to prob_up=0.5, NOT abstention.
        p_none = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, None, edge_lambda=LAMBDA)
        p_half = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA)
        assert p_none is not None
        assert p_none == pytest.approx(p_half, abs=1e-12)

    def test_symmetric_barriers_give_half_at_zero_edge(self):
        # tp·sl = S² ⇒ b = −a ⇒ distance ratio = 0.5.
        s, tp = 100.0, 110.0
        sl = s * s / tp  # 90.909...
        p = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, 0.5, edge_lambda=LAMBDA)
        assert p == pytest.approx(0.5, abs=1e-12)


# ──────────────────────────────────────────────────────────────────────────────
# ML edge direction & known-value regression
# ──────────────────────────────────────────────────────────────────────────────

class TestEdgeDirection:
    def test_bullish_edge_raises_p_for_long(self):
        base = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA)
        bull = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.7, edge_lambda=LAMBDA)
        bear = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.3, edge_lambda=LAMBDA)
        assert bear < base < bull

    def test_bullish_edge_lowers_p_for_short(self):
        # For a SHORT, price going UP hurts, so a higher prob_up must lower P.
        base = hit_tp_before_sl(100.0, 90.0, 105.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA)
        bull = hit_tp_before_sl(100.0, 90.0, 105.0, PositionSide.SHORT, 0.7, edge_lambda=LAMBDA)
        bear = hit_tp_before_sl(100.0, 90.0, 105.0, PositionSide.SHORT, 0.3, edge_lambda=LAMBDA)
        assert bull < base < bear

    def test_known_value_regression(self):
        # Hand-computed: S=100, TP=110, SL=95, prob_up=0.7, λ=3 ⇒ ρ=1.2.
        # a=ln(0.95), b=ln(1.1); P = expm1(ρa)/expm1(ρ(a−b)) ≈ 0.370055.
        p = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.7, edge_lambda=LAMBDA)
        assert p == pytest.approx(0.370055, abs=1e-5)

    def test_zero_edge_known_value_regression(self):
        # S=100, TP=110, SL=95, neutral ⇒ distance ratio ≈ 0.349878.
        p = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA)
        assert p == pytest.approx(0.349878, abs=1e-5)


# ──────────────────────────────────────────────────────────────────────────────
# LONG / SHORT reflection symmetry
# ──────────────────────────────────────────────────────────────────────────────

class TestLongShortSymmetry:
    def test_short_equals_reflected_long(self):
        # SHORT(S, tp, sl, p) ≡ LONG(S, S²/tp, S²/sl, 1−p).
        s, tp, sl, p = 100.0, 90.0, 105.0, 0.3
        short = hit_tp_before_sl(s, tp, sl, PositionSide.SHORT, p, edge_lambda=LAMBDA)
        long_reflected = hit_tp_before_sl(
            s, s * s / tp, s * s / sl, PositionSide.LONG, 1.0 - p, edge_lambda=LAMBDA
        )
        assert short == pytest.approx(long_reflected, abs=1e-12)

    @given(s=_prices, spread=st.floats(min_value=0.01, max_value=0.4), p=_probs, lam=_lambdas)
    @_prop_settings
    def test_reflection_symmetry_property(self, s, spread, p, lam):
        tp = s * (1.0 - spread)          # SHORT target below
        sl = s * (1.0 + spread)          # SHORT stop above
        short = hit_tp_before_sl(s, tp, sl, PositionSide.SHORT, p, edge_lambda=lam)
        long_reflected = hit_tp_before_sl(
            s, s * s / tp, s * s / sl, PositionSide.LONG, 1.0 - p, edge_lambda=lam
        )
        assert short == pytest.approx(long_reflected, abs=1e-9)


# ──────────────────────────────────────────────────────────────────────────────
# Missing barriers — abstain to None
# ──────────────────────────────────────────────────────────────────────────────

class TestMissingBarriers:
    def test_missing_tp_returns_none(self):
        assert hit_tp_before_sl(100.0, None, 95.0, PositionSide.LONG, 0.6, edge_lambda=LAMBDA) is None

    def test_missing_sl_returns_none(self):
        assert hit_tp_before_sl(100.0, 110.0, None, PositionSide.LONG, 0.6, edge_lambda=LAMBDA) is None

    def test_missing_both_returns_none(self):
        assert hit_tp_before_sl(100.0, None, None, PositionSide.LONG, 0.6, edge_lambda=LAMBDA) is None


# ──────────────────────────────────────────────────────────────────────────────
# Domain & structural guards — abstain to None
# ──────────────────────────────────────────────────────────────────────────────

class TestDomainGuards:
    @pytest.mark.parametrize("price", [0.0, -1.0, math.inf, -math.inf, math.nan])
    def test_bad_current_price_returns_none(self, price):
        assert hit_tp_before_sl(price, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) is None

    @pytest.mark.parametrize("tp", [0.0, -5.0, math.inf, math.nan])
    def test_bad_tp_returns_none(self, tp):
        assert hit_tp_before_sl(100.0, tp, 95.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) is None

    @pytest.mark.parametrize("sl", [0.0, -5.0, math.inf, math.nan])
    def test_bad_sl_returns_none(self, sl):
        assert hit_tp_before_sl(100.0, 110.0, sl, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) is None

    def test_long_invalid_config_tp_not_above_sl(self):
        # LONG target must sit above the stop; tp ≤ sl is corrupt ⇒ abstain.
        assert hit_tp_before_sl(100.0, 95.0, 110.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) is None
        assert hit_tp_before_sl(100.0, 100.0, 100.0, PositionSide.LONG, 0.5, edge_lambda=LAMBDA) is None

    def test_short_invalid_config_tp_not_below_sl(self):
        assert hit_tp_before_sl(100.0, 105.0, 90.0, PositionSide.SHORT, 0.5, edge_lambda=LAMBDA) is None


# ──────────────────────────────────────────────────────────────────────────────
# Programmer-contract violations — fail fast
# ──────────────────────────────────────────────────────────────────────────────

class TestContractViolations:
    @pytest.mark.parametrize("lam", [-0.1, -1.0, math.inf, math.nan])
    def test_bad_edge_lambda_raises(self, lam):
        with pytest.raises(ValueError):
            hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.5, edge_lambda=lam)

    @pytest.mark.parametrize("side", ["BUY", "", "up", "NEUTRAL", "longshort"])
    def test_unknown_side_raises(self, side):
        with pytest.raises(ValueError):
            hit_tp_before_sl(100.0, 110.0, 95.0, side, 0.5, edge_lambda=LAMBDA)

    @pytest.mark.parametrize("side", ["long", "LONG", "  long  ", "Long"])
    def test_string_side_normalised_case_and_whitespace(self, side):
        # Robust to case and surrounding whitespace; all resolve to LONG.
        p_enum = hit_tp_before_sl(100.0, 110.0, 95.0, PositionSide.LONG, 0.6, edge_lambda=LAMBDA)
        p_str = hit_tp_before_sl(100.0, 110.0, 95.0, side, 0.6, edge_lambda=LAMBDA)
        assert p_str == pytest.approx(p_enum, abs=1e-12)


# ──────────────────────────────────────────────────────────────────────────────
# Property-based invariants
# ──────────────────────────────────────────────────────────────────────────────

class TestProperties:
    @given(s=_prices, tp=_prices, sl=_prices, p=_probs, lam=_lambdas,
           side=st.sampled_from([PositionSide.LONG, PositionSide.SHORT]))
    @_prop_settings
    def test_output_is_valid_probability_or_none(self, s, tp, sl, p, lam, side):
        result = hit_tp_before_sl(s, tp, sl, side, p, edge_lambda=lam)
        assert result is None or (0.0 <= result <= 1.0)

    @given(
        sl=st.floats(min_value=1.0, max_value=90.0),
        tp=st.floats(min_value=110.0, max_value=1000.0),
        p=_probs, lam=_lambdas,
        step=st.floats(min_value=0.5, max_value=5.0),
        base=st.floats(min_value=91.0, max_value=104.0),
    )
    @_prop_settings
    def test_monotonic_in_price_long(self, sl, tp, p, lam, step, base):
        # For a LONG, moving the price closer to TP must not decrease P.
        lo = hit_tp_before_sl(base, tp, sl, PositionSide.LONG, p, edge_lambda=lam)
        hi = hit_tp_before_sl(base + step, tp, sl, PositionSide.LONG, p, edge_lambda=lam)
        assert lo is not None and hi is not None
        assert hi >= lo - 1e-9

    @given(
        s=_prices, spread=st.floats(min_value=0.01, max_value=0.4),
        lam=st.floats(min_value=0.1, max_value=20.0),
        p_lo=st.floats(min_value=0.0, max_value=0.49),
        p_hi=st.floats(min_value=0.51, max_value=1.0),
    )
    @_prop_settings
    def test_monotonic_in_prob_up_long(self, s, spread, lam, p_lo, p_hi):
        # A stronger bullish edge must not decrease a LONG's P.
        tp = s * (1.0 + spread)
        sl = s * (1.0 - spread)
        lo = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, p_lo, edge_lambda=lam)
        hi = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, p_hi, edge_lambda=lam)
        assert lo is not None and hi is not None
        assert hi >= lo - 1e-9

    @given(
        s=_prices, spread=st.floats(min_value=0.01, max_value=0.4), p=_probs,
        rho_lam=st.floats(min_value=1e-7, max_value=1e-5),
    )
    @_prop_settings
    def test_continuity_at_zero_edge(self, s, spread, p, rho_lam):
        # As λ → 0 the drifted estimate must converge to the zero-edge value:
        # no discontinuity at the branch boundary.
        tp = s * (1.0 + spread)
        sl = s * (1.0 - spread)
        near_zero = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, p, edge_lambda=rho_lam)
        exact_zero = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, p, edge_lambda=0.0)
        assert near_zero == pytest.approx(exact_zero, abs=1e-4)

    @given(s=_prices, spread=st.floats(min_value=0.01, max_value=0.4), p=_probs, lam=_lambdas)
    @_prop_settings
    def test_no_overflow_for_extreme_lambda(self, s, spread, p, lam):
        # Guards the numerical-stability claim: finite probability, never inf/nan.
        tp = s * (1.0 + spread)
        sl = s * (1.0 - spread)
        result = hit_tp_before_sl(s, tp, sl, PositionSide.LONG, p, edge_lambda=lam)
        assert result is not None and math.isfinite(result)
