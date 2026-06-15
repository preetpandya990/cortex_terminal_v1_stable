"""
Pure-logic unit tests for the Gemini-backed CortexIntelligenceClient.

These cover deterministic, side-effect-free logic with literal inputs only —
error classification, vector normalization, and the model-id property.  Live
generation, structured output, and embeddings are verified against the real
Gemini API in the integration suite, not here.
"""
from __future__ import annotations

import math

from app.ai.intelligence import llm_client as L
from app.ai.intelligence.llm_client import CortexIntelligenceClient


def _exc(code):
    """A bare exception carrying an HTTP-style status code (not a service mock)."""
    e = Exception("boom")
    e.code = code
    return e


# ── Error classification ─────────────────────────────────────────────────────

def test_permanent_errors():
    for code in (400, 401, 403, 404):
        assert L._is_permanent(_exc(code)) is True
        assert L._is_transient(_exc(code)) is False


def test_transient_errors():
    for code in (429, 500, 502, 503, 504):
        assert L._is_transient(_exc(code)) is True
        assert L._is_permanent(_exc(code)) is False


# ── Vector normalization ─────────────────────────────────────────────────────

def test_l2_normalize_unit_norm():
    v = L._l2_normalize([3.0, 4.0])
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, rel_tol=1e-9)


def test_l2_normalize_zero_vector_is_noop():
    assert L._l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


# ── Thinking-config model routing ────────────────────────────────────────────

def test_thinking_param_routing_by_model_generation():
    # Gemini 3.x uses thinking_level; 2.x uses integer thinking_budget.
    assert L._model_uses_thinking_budget("gemini-2.5-flash") is True
    assert L._model_uses_thinking_budget("gemini-2.0-flash") is True
    assert L._model_uses_thinking_budget("gemini-3.5-flash") is False
    assert L._model_uses_thinking_budget("gemini-flash-latest") is False


# ── model_id property ────────────────────────────────────────────────────────

def test_model_id_property():
    inst = object.__new__(CortexIntelligenceClient)
    inst._model = "gemini-2.5-flash"
    assert inst.model_id == "gemini/gemini-2.5-flash"
