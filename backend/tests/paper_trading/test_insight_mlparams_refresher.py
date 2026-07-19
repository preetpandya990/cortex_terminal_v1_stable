"""
Unit tests for the Portfolio-Insight ML-param refresher orchestration
(``app.services.paper_trading.insight_mlparams_refresher``).

Focus on the decision logic — not real Redis/DB/GPU:
  • _score_and_cache caches only available + derivable snapshots, with the
    correct prob_up / sigma, and never writes a degraded one.
  • _run_sweep scores only stale instruments (freshness filter), honours force,
    and respects the batch cap.
  • _handle_ondemand dedups against a still-fresh cache entry.
  • run() parks (no work) when the feature is disabled.

Marked ``unit``: mocked inference + in-memory fake Redis, no real I/O.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import get_settings
from app.services.paper_trading import insight_cache
from app.services.paper_trading import insight_mlparams_refresher as mod
from app.services.paper_trading.insight_mlparams_refresher import InsightMLParamsRefresher
from app.workers.supervisor import PauseToken, TriggerToken

pytestmark = pytest.mark.unit


# ── In-memory fake Redis (string + TTL surface) ─────────────────────────────────

class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key):
        return self.store.get(key)

    async def ttl(self, key):
        if key not in self.store:
            return -2
        return self.ttls.get(key, -1)


def _available(buy: float, hold: float, sell: float, vol: float = 0.25) -> dict:
    return {
        "available": True,
        "probabilities": {"buy": buy, "hold": hold, "sell": sell},
        "volatility": vol,
    }


def _unavailable(reason: str) -> dict:
    return {"available": False, "unavailable_reason": reason}


def _make_refresher(redis) -> InsightMLParamsRefresher:
    return InsightMLParamsRefresher(
        session_factory=object(),         # unused: DB access is patched
        redis=redis,
        predictor=object(),               # unused: batch inference is patched
        shutdown=asyncio.Event(),
        pause=PauseToken(),
        trigger=TriggerToken(),
        on_cycle=None,
    )


# ──────────────────────────────────────────────────────────────────────────────
# _score_and_cache
# ──────────────────────────────────────────────────────────────────────────────

class TestScoreAndCache:
    @pytest.mark.asyncio
    async def test_caches_only_available_and_derivable(self, monkeypatch):
        redis = _FakeRedis()
        snaps = {
            "A": _available(0.8, 0.1, 0.1, vol=0.3),   # ok → 0.85
            "B": _unavailable("insufficient_data"),      # skip
            "C": _available(0.0, 0.0, 0.0),              # derive_prob_up → None → skip
        }

        async def fake_batch(**kwargs):
            assert set(kwargs["instrument_keys"]) == {"A", "B", "C"}
            return snaps

        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", fake_batch)
        r = _make_refresher(redis)
        scored = await r._score_and_cache(["A", "B", "C"], trigger="sweep")

        assert scored == 1
        a = await insight_cache.read_mlparams(redis, "A")
        assert a["prob_up"] == pytest.approx(0.85)
        assert a["sigma"] == pytest.approx(0.3)
        assert await insight_cache.read_mlparams(redis, "B") is None
        assert await insight_cache.read_mlparams(redis, "C") is None

    @pytest.mark.asyncio
    async def test_empty_input_no_call(self, monkeypatch):
        called = False

        async def fake_batch(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", fake_batch)
        r = _make_refresher(_FakeRedis())
        assert await r._score_and_cache([], trigger="sweep") == 0
        assert called is False


# ──────────────────────────────────────────────────────────────────────────────
# _run_sweep — freshness filter, force, cap
# ──────────────────────────────────────────────────────────────────────────────

class TestRunSweep:
    @pytest.mark.asyncio
    async def test_scores_only_stale(self, monkeypatch):
        redis = _FakeRedis()
        # B is already fresh → must be skipped; A and C are absent → scored.
        await insight_cache.write_mlparams(redis, "B", prob_up=0.4, sigma=0.2, ttl_seconds=1800)

        monkeypatch.setattr(
            InsightMLParamsRefresher, "_open_position_instrument_keys",
            _fake_keys(["A", "B", "C"]),
        )
        scored_keys: list[str] = []
        monkeypatch.setattr(
            mod, "get_prediction_snapshots_batch", _capture_batch(scored_keys),
        )
        r = _make_refresher(redis)
        await r._run_sweep(force=False)

        assert set(scored_keys) == {"A", "C"}
        # B's original value untouched.
        assert (await insight_cache.read_mlparams(redis, "B"))["prob_up"] == pytest.approx(0.4)

    @pytest.mark.asyncio
    async def test_force_scores_all(self, monkeypatch):
        redis = _FakeRedis()
        await insight_cache.write_mlparams(redis, "B", prob_up=0.4, sigma=0.2, ttl_seconds=1800)
        monkeypatch.setattr(
            InsightMLParamsRefresher, "_open_position_instrument_keys",
            _fake_keys(["A", "B"]),
        )
        scored_keys: list[str] = []
        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", _capture_batch(scored_keys))
        r = _make_refresher(redis)
        await r._run_sweep(force=True)
        assert set(scored_keys) == {"A", "B"}

    @pytest.mark.asyncio
    async def test_empty_open_set_no_scoring(self, monkeypatch):
        monkeypatch.setattr(
            InsightMLParamsRefresher, "_open_position_instrument_keys", _fake_keys([]),
        )
        called = False

        async def fake_batch(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", fake_batch)
        await _make_refresher(_FakeRedis())._run_sweep(force=False)
        assert called is False

    @pytest.mark.asyncio
    async def test_respects_batch_cap(self, monkeypatch):
        redis = _FakeRedis()
        keys = [f"K{i}" for i in range(10)]
        monkeypatch.setattr(
            InsightMLParamsRefresher, "_open_position_instrument_keys", _fake_keys(keys),
        )
        monkeypatch.setattr(get_settings(), "INSIGHT_MLPARAMS_BATCH_CAP", 4)
        monkeypatch.setattr(get_settings(), "INSIGHT_MLPARAMS_CHUNK_SIZE", 2)
        scored_keys: list[str] = []
        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", _capture_batch(scored_keys))
        await _make_refresher(redis)._run_sweep(force=True)
        assert len(scored_keys) == 4          # capped; surplus deferred


# ──────────────────────────────────────────────────────────────────────────────
# _handle_ondemand — dedup
# ──────────────────────────────────────────────────────────────────────────────

class TestHandleOnDemand:
    @pytest.mark.asyncio
    async def test_skips_when_fresh(self, monkeypatch):
        redis = _FakeRedis()
        await insight_cache.write_mlparams(redis, "A", prob_up=0.4, sigma=0.2, ttl_seconds=1800)
        called = False

        async def fake_batch(**kwargs):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", fake_batch)
        await _make_refresher(redis)._handle_ondemand("A")
        assert called is False              # deduped against fresh cache

    @pytest.mark.asyncio
    async def test_scores_when_stale(self, monkeypatch):
        redis = _FakeRedis()
        scored_keys: list[str] = []
        monkeypatch.setattr(mod, "get_prediction_snapshots_batch", _capture_batch(scored_keys))
        await _make_refresher(redis)._handle_ondemand("A")
        assert scored_keys == ["A"]
        assert (await insight_cache.read_mlparams(redis, "A"))["prob_up"] == pytest.approx(0.85)


# ──────────────────────────────────────────────────────────────────────────────
# run() — disabled parks
# ──────────────────────────────────────────────────────────────────────────────

class TestRunDisabled:
    @pytest.mark.asyncio
    async def test_parks_when_disabled(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", False)
        r = _make_refresher(_FakeRedis())
        r._shutdown.set()                   # pre-set so the park loop exits at once
        await asyncio.wait_for(r.run(), timeout=1.0)   # returns without doing work


# ── helpers ─────────────────────────────────────────────────────────────────────

def _fake_keys(keys: list[str]):
    async def _impl(self):
        return list(keys)
    return _impl


def _capture_batch(sink: list[str]):
    async def _impl(**kwargs):
        keys = kwargs["instrument_keys"]
        sink.extend(keys)
        return {k: _available(0.8, 0.1, 0.1) for k in keys}
    return _impl
