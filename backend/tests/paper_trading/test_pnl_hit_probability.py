"""
Unit tests for the P&L-frame live-edge enrichment (B3):
``app.services.paper_trading.pnl_worker._hit_probability_fields``.

Verifies the B2 → B3 contract end-to-end through the real cache serialization:
  • a cached prob_up is used (stale=False) and the result matches a direct
    hit_tp_before_sl call with that prob_up;
  • a cache miss degrades to the neutral distance-ratio estimate (stale=True);
  • TP1 is the operative take-profit barrier;
  • missing TP1/SL yields None; LONG and SHORT are handled;
  • Decimal position prices are accepted.

Marked ``unit``: pure calc + in-memory fake Redis, no real I/O.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.paper_trading import insight_cache
from app.services.paper_trading.hit_probability import hit_tp_before_sl
from app.services.paper_trading.pnl_worker import _hit_probability_fields

pytestmark = pytest.mark.unit

LAMBDA = 3.0


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


def _pos(instrument_key="NSE_EQ|X", *, tp1, sl, side="LONG"):
    return SimpleNamespace(
        instrument_key=instrument_key,
        target_price_1=tp1,
        stop_loss=sl,
        side=side,
    )


class TestHitProbabilityFields:
    @pytest.mark.asyncio
    async def test_uses_cached_prob_up(self):
        redis = _FakeRedis()
        await insight_cache.write_mlparams(
            redis, "NSE_EQ|X", prob_up=0.7, sigma=0.2, ttl_seconds=1800
        )
        pos = _pos(tp1=Decimal("110"), sl=Decimal("95"))

        hit, stale = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)

        assert stale is False
        expected = hit_tp_before_sl(100.0, 110.0, 95.0, "LONG", 0.7, edge_lambda=LAMBDA)
        assert hit == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_cache_miss_degrades_to_neutral_and_flags_stale(self):
        redis = _FakeRedis()  # nothing cached
        pos = _pos(tp1=Decimal("110"), sl=Decimal("95"))

        hit, stale = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)

        assert stale is True
        # Neutral estimate == prob_up None (pure distance ratio).
        expected = hit_tp_before_sl(100.0, 110.0, 95.0, "LONG", None, edge_lambda=LAMBDA)
        assert hit == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_tp1_is_the_operative_barrier(self):
        # A different TP1 must change the result (proves target_price_1 is used).
        redis = _FakeRedis()
        near = _pos(tp1=Decimal("102"), sl=Decimal("95"))
        far = _pos(tp1=Decimal("130"), sl=Decimal("95"))
        hit_near, _ = await _hit_probability_fields(redis, near, Decimal("100"), LAMBDA)
        hit_far, _ = await _hit_probability_fields(redis, far, Decimal("100"), LAMBDA)
        # A nearer take-profit is more likely to be hit first.
        assert hit_near > hit_far

    @pytest.mark.asyncio
    async def test_missing_tp1_returns_none(self):
        redis = _FakeRedis()
        pos = _pos(tp1=None, sl=Decimal("95"))
        hit, stale = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)
        assert hit is None
        assert stale is True

    @pytest.mark.asyncio
    async def test_missing_sl_returns_none(self):
        redis = _FakeRedis()
        pos = _pos(tp1=Decimal("110"), sl=None)
        hit, _ = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)
        assert hit is None

    @pytest.mark.asyncio
    async def test_short_position(self):
        redis = _FakeRedis()
        await insight_cache.write_mlparams(
            redis, "NSE_EQ|X", prob_up=0.3, sigma=0.2, ttl_seconds=1800
        )
        pos = _pos(tp1=Decimal("90"), sl=Decimal("105"), side="SHORT")
        hit, stale = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)
        assert stale is False
        expected = hit_tp_before_sl(100.0, 90.0, 105.0, "SHORT", 0.3, edge_lambda=LAMBDA)
        assert hit == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_result_is_probability_or_none(self):
        redis = _FakeRedis()
        pos = _pos(tp1=Decimal("110"), sl=Decimal("95"))
        hit, _ = await _hit_probability_fields(redis, pos, Decimal("100"), LAMBDA)
        assert hit is not None and 0.0 <= hit <= 1.0
