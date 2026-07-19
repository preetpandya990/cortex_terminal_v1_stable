"""
Unit tests for the Portfolio AI Advice service (B5):
``app.services.paper_trading.portfolio_advice_service`` + the router feature gate.

Covers materiality hashing, prompt grounding, the reused guardrails, the cache
hit/miss paths, and the never-500 quota-degrade contract — all with the LLM
client, stats, and Redis mocked (no network/DB).

Marked ``unit``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.schemas.portfolio_insight import (
    CapitalAtRiskStat,
    CorrelationStat,
    HoldingWeight,
    PerPositionNote,
    PortfolioAdviceGeneration,
    PortfolioInsightStats,
    SectorConcentration,
    SectorWeight,
    SingleNameConcentration,
    StressScan,
    StressScenario,
)
from app.ai.intelligence.request_manager import GeminiQuotaExhausted as RMQuota
from app.services.paper_trading import portfolio_advice_service as svc

pytestmark = pytest.mark.unit


# ── Fixtures / builders ─────────────────────────────────────────────────────────

class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.store[key] = value


class _FakeClient:
    """Stub intelligence client: returns a canned generation or raises."""
    def __init__(self, generation=None, exc=None):
        self._gen = generation
        self._exc = exc
        self.calls = 0

    async def generate_structured_with_usage(self, prompt, model, **kwargs):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._gen, {"model_id": "gemini-test"}


def _stats(*, n=3, pid=None):
    return PortfolioInsightStats(
        portfolio_id=pid or uuid4(),
        portfolio_value=5000.0,
        open_position_count=n,
        capital_at_risk=CapitalAtRiskStat(
            capital_at_risk=90.0, capital_at_risk_pct=1.8,
            positions_with_stop=2, positions_without_stop=1,
        ),
        single_name=SingleNameConcentration(
            max_weight_pct=22.0, max_weight_symbol="RELIANCE", hhi=0.355,
            effective_positions=2.81,
            top_holdings=[
                HoldingWeight(symbol="RELIANCE", weight_pct=22.0),
                HoldingWeight(symbol="ONGC", weight_pct=21.0),
            ],
        ),
        sector=SectorConcentration(
            max_sector="Energy", max_sector_weight_pct=43.0,
            unclassified_weight_pct=11.6,
            breakdown=[SectorWeight(sector="Energy", weight_pct=43.0)],
        ),
        correlation=CorrelationStat(
            max_pair_correlation=0.82, max_pair=["RELIANCE", "ONGC"],
            avg_pairwise_correlation=0.44, covered_positions=2,
            excluded_positions=0, window_days=90,
        ),
        stress=StressScan(scenarios=[
            StressScenario(key="index_down", label="Broad market −5%", delta_pct=-1.57, detail="x"),
        ]),
        notes=["1 open position(s) have no stop-loss and are excluded from capital-at-risk."],
        computed_at=datetime.now(timezone.utc),
    )


def _generation():
    return PortfolioAdviceGeneration(
        assessment="Energy is 43.0% of the book and RELIANCE is 22.0% of portfolio.",
        key_risks=["Single-sector concentration in Energy at 43.0%"],
        considerations=["Consider trimming the RELIANCE/ONGC pair (correlation 0.82)"],
        per_position=[PerPositionNote(symbol="RELIANCE", note="Largest name at 22.0% of portfolio.")],
    )


def _patch_common(monkeypatch, stats, holdings, client):
    async def fake_stats(session, portfolio):
        return stats

    async def fake_holdings(session, pid):
        return holdings

    monkeypatch.setattr(svc, "compute_portfolio_insight_stats", fake_stats)
    monkeypatch.setattr(svc, "_open_holdings_signature", fake_holdings)
    monkeypatch.setattr(svc, "get_intelligence_client", lambda: client)


_PORTFOLIO = type("P", (), {"id": uuid4(), "current_cash": 2270.0})()
_HOLDINGS = [["NSE_EQ|A", 10, "LONG"], ["NSE_EQ|B", 5, "LONG"], ["NSE_EQ|C", 4, "SHORT"]]


# ── Materiality hash ────────────────────────────────────────────────────────────

class TestMaterialityHash:
    def test_stable_and_sensitive(self):
        s = _stats()
        h1 = svc._materiality_hash(_HOLDINGS, s)
        h2 = svc._materiality_hash(list(_HOLDINGS), s)
        assert h1 == h2                                   # deterministic

        changed = [["NSE_EQ|A", 20, "LONG"]]              # qty changed
        assert svc._materiality_hash(changed, s) != h1    # structural change

    def test_small_drift_within_bucket_is_stable(self):
        s1 = _stats()
        s2 = _stats()
        s2.capital_at_risk.capital_at_risk_pct = 1.9      # 1.8 → 1.9 rounds to same 2% bucket
        assert svc._materiality_hash(_HOLDINGS, s1) == svc._materiality_hash(_HOLDINGS, s2)

    def test_bucket_crossing_changes_hash(self):
        s1 = _stats()
        s2 = _stats()
        s2.single_name.max_weight_pct = 30.0              # 22 (→20 bucket) vs 30 (→30 bucket)
        assert svc._materiality_hash(_HOLDINGS, s1) != svc._materiality_hash(_HOLDINGS, s2)


# ── Prompt grounding ─────────────────────────────────────────────────────────────

class TestPrompt:
    def test_contains_key_figures(self):
        p = svc._build_prompt(_stats())
        for token in ["43.0%", "22.0%", "RELIANCE", "ONGC", "0.82", "Energy", "1.8%"]:
            assert token in p


# ── Guardrails ───────────────────────────────────────────────────────────────────

class TestGuardrails:
    def test_strips_guarantee_and_ungrounded(self):
        prompt = svc._build_prompt(_stats())          # contains 43.0%, 22.0%, ...
        gen = PortfolioAdviceGeneration(
            assessment="This portfolio will guarantee a 99% profit.",   # guarantee + ungrounded 99%
            key_risks=["Energy is 43.0% of the book."],                  # grounded → kept
            considerations=["This position is 88% likely to bounce."],   # ungrounded 88% → stripped
            per_position=[PerPositionNote(symbol="X", note="Guaranteed profit ahead.")],
        )
        clean = svc._apply_advice_guardrails(gen, prompt)
        assert "guarantee" not in clean.assessment.lower()
        assert clean.key_risks == ["Energy is 43.0% of the book."]
        assert clean.considerations == []                # ungrounded 88% sentence removed
        # per-position "guaranteed profit" note stripped to empty → dropped
        assert clean.per_position == []

    def test_empty_assessment_falls_back(self):
        prompt = "nothing"
        gen = PortfolioAdviceGeneration(
            assessment="Guaranteed 100% returns.", key_risks=[], considerations=[], per_position=[],
        )
        clean = svc._apply_advice_guardrails(gen, prompt)
        assert clean.assessment == svc._ASSESSMENT_FALLBACK


# ── Orchestrator ─────────────────────────────────────────────────────────────────

class TestGenerate:
    @pytest.mark.asyncio
    async def test_empty_portfolio_no_llm(self, monkeypatch):
        client = _FakeClient(generation=_generation())
        _patch_common(monkeypatch, _stats(n=0), [], client)
        advice = await svc.generate_portfolio_advice(object(), _PORTFOLIO, _FakeRedis())
        assert advice.stale is False
        assert "no open positions" in advice.assessment.lower()
        assert client.calls == 0
        assert advice.disclaimer

    @pytest.mark.asyncio
    async def test_cache_miss_generates_and_caches(self, monkeypatch):
        client = _FakeClient(generation=_generation())
        _patch_common(monkeypatch, _stats(), _HOLDINGS, client)
        redis = _FakeRedis()
        advice = await svc.generate_portfolio_advice(object(), _PORTFOLIO, redis)

        assert client.calls == 1
        assert advice.stale is False
        assert advice.model_id == "gemini-test"
        assert advice.disclaimer
        assert "RELIANCE" in advice.assessment
        # cached for next time
        assert redis.store, "advice should be cached"

    @pytest.mark.asyncio
    async def test_cache_hit_skips_llm(self, monkeypatch):
        stats = _stats()
        client = _FakeClient(generation=_generation())
        _patch_common(monkeypatch, stats, _HOLDINGS, client)
        redis = _FakeRedis()

        first = await svc.generate_portfolio_advice(object(), _PORTFOLIO, redis)
        assert client.calls == 1
        # second call, portfolio unchanged → served from cache, no new LLM call
        second = await svc.generate_portfolio_advice(object(), _PORTFOLIO, redis)
        assert client.calls == 1
        assert second.stale is False
        assert second.assessment == first.assessment

    @pytest.mark.asyncio
    async def test_quota_exhaustion_with_cache_serves_stale(self, monkeypatch):
        redis = _FakeRedis()
        # 1) seed the cache with a successful generation
        ok = _FakeClient(generation=_generation())
        _patch_common(monkeypatch, _stats(), _HOLDINGS, ok)
        await svc.generate_portfolio_advice(object(), _PORTFOLIO, redis)

        # 2) now the portfolio changes (force regen) AND the LLM is exhausted
        changed_stats = _stats()
        changed_stats.single_name.max_weight_pct = 40.0   # crosses bucket → materiality differs
        exhausted = _FakeClient(exc=RMQuota("quota"))
        _patch_common(monkeypatch, changed_stats, [["NSE_EQ|A", 99, "LONG"]], exhausted)
        advice = await svc.generate_portfolio_advice(object(), _PORTFOLIO, redis)

        assert exhausted.calls == 1
        assert advice.stale is True                       # served last-known cached advice
        assert advice.disclaimer

    @pytest.mark.asyncio
    async def test_quota_exhaustion_no_cache_graceful(self, monkeypatch):
        exhausted = _FakeClient(exc=RMQuota("quota"))
        _patch_common(monkeypatch, _stats(), _HOLDINGS, exhausted)
        advice = await svc.generate_portfolio_advice(object(), _PORTFOLIO, _FakeRedis())

        assert advice.stale is True
        assert "unavailable" in advice.assessment.lower()
        assert advice.key_risks == []
        assert advice.disclaimer                          # never blank, never raised

    @pytest.mark.asyncio
    async def test_unexpected_error_never_raises(self, monkeypatch):
        boom = _FakeClient(exc=RuntimeError("boom"))
        _patch_common(monkeypatch, _stats(), _HOLDINGS, boom)
        advice = await svc.generate_portfolio_advice(object(), _PORTFOLIO, _FakeRedis())
        assert advice.stale is True                       # degrades, does not propagate


# ── Feature gate ─────────────────────────────────────────────────────────────────

class TestFeatureGate:
    @pytest.mark.asyncio
    async def test_gate_blocks_when_disabled(self, monkeypatch):
        from fastapi import HTTPException
        from app.api.v1.portfolio_insight import _require_insight_enabled

        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", False)
        with pytest.raises(HTTPException) as ei:
            await _require_insight_enabled()
        assert ei.value.status_code == 404

    @pytest.mark.asyncio
    async def test_gate_allows_when_enabled(self, monkeypatch):
        from app.api.v1.portfolio_insight import _require_insight_enabled
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", True)
        await _require_insight_enabled()   # no raise
