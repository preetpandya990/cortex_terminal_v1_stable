"""
AI stream delivery-guard tests (WS8)
====================================
The shared anti-downgrade guard (now applied on the poll path AND both push
branches) and the backend-computed direction-mismatch flag.
"""
from __future__ import annotations

import pytest

from app.api.v1.ai_stream import _direction_mismatch, _should_apply_explanation

pytestmark = pytest.mark.unit


def _ready(
    suggestion_id: str = "A",
    signal_at: str = "2026-07-06T10:00:00+00:00",
    **extra,
) -> dict:
    return {
        "available": True,
        "failed": False,
        "suggestion_id": suggestion_id,
        "signal_generated_at": signal_at,
        "context_type": "suggestion_explanation",
        **extra,
    }


def _skeleton(**extra) -> dict:
    return {"available": False, "failed": False, **extra}


class TestShouldApplyExplanation:
    def test_identical_payload_is_noop(self):
        payload = _ready()
        assert _should_apply_explanation(payload, dict(payload)) is False

    def test_first_payload_always_applies(self):
        assert _should_apply_explanation(None, _skeleton()) is True

    def test_none_incoming_never_applies(self):
        assert _should_apply_explanation(_ready(), None) is False

    def test_skeleton_never_downgrades_delivered(self):
        assert _should_apply_explanation(_ready(), _skeleton()) is False

    def test_skeleton_never_hides_confirmed_failure(self):
        failed = {"available": False, "failed": True}
        assert _should_apply_explanation(failed, _skeleton()) is False

    def test_weak_signal_never_downgrades_delivered(self):
        weak = _skeleton(weak_signal=True)
        assert _should_apply_explanation(_ready(), weak) is False

    def test_delivered_replaces_skeleton(self):
        assert _should_apply_explanation(_skeleton(), _ready()) is True

    def test_older_suggestion_never_overwrites_newer(self):
        """THE delayed-retry clobber (WS8): suggestion A's late completion
        must not replace superseding suggestion B's delivered explanation —
        even though A finished generating LAST."""
        current_b = _ready("B", signal_at="2026-07-06T11:00:00+00:00")
        late_a = _ready("A", signal_at="2026-07-06T10:00:00+00:00")
        assert _should_apply_explanation(current_b, late_a) is False

    def test_newer_suggestion_replaces_older(self):
        current_a = _ready("A", signal_at="2026-07-06T10:00:00+00:00")
        newer_b = _ready("B", signal_at="2026-07-06T11:00:00+00:00")
        assert _should_apply_explanation(current_a, newer_b) is True

    def test_failed_push_for_other_suggestion_never_clobbers_delivered(self):
        current_b = _ready("B")
        failed_a = {
            "available": False, "failed": True, "suggestion_id": "A",
            "signal_generated_at": None,
        }
        assert _should_apply_explanation(current_b, failed_a) is False

    def test_failed_push_for_same_suggestion_applies_over_skeleton(self):
        failed = {"available": False, "failed": True, "suggestion_id": "A"}
        assert _should_apply_explanation(_skeleton(suggestion_id="A"), failed) is True

    def test_context_never_replaces_delivered_suggestion_explanation(self):
        """Mirrors the Stage-1 lookup precedence (suggestion > context)."""
        context = {
            "available": True, "failed": False,
            "context_type": "instrument_context", "signal_generated_at": None,
        }
        assert _should_apply_explanation(_ready(), context) is False

    def test_suggestion_explanation_replaces_delivered_context(self):
        context = {
            "available": True, "failed": False,
            "context_type": "instrument_context", "signal_generated_at": None,
        }
        assert _should_apply_explanation(context, _ready()) is True


class TestDirectionMismatch:
    def _prediction(self, direction: str) -> dict:
        return {"available": True, "direction": direction}

    def test_contradiction_flags(self):
        assert _direction_mismatch(
            self._prediction("SELL"), _ready(signal_direction="BUY")
        ) is True

    def test_agreement_does_not_flag(self):
        assert _direction_mismatch(
            self._prediction("BUY"), _ready(signal_direction="BUY")
        ) is False

    def test_hold_prediction_is_not_a_contradiction(self):
        assert _direction_mismatch(
            self._prediction("HOLD"), _ready(signal_direction="BUY")
        ) is False

    def test_undelivered_explanation_never_flags(self):
        assert _direction_mismatch(
            self._prediction("SELL"),
            _skeleton(signal_direction="BUY", context_type="suggestion_explanation"),
        ) is False

    def test_instrument_context_never_flags(self):
        context = {
            "available": True, "context_type": "instrument_context",
            "signal_direction": None,
        }
        assert _direction_mismatch(self._prediction("SELL"), context) is False

    def test_missing_inputs_never_flag(self):
        assert _direction_mismatch(None, _ready(signal_direction="BUY")) is False
        assert _direction_mismatch(self._prediction("SELL"), None) is False
