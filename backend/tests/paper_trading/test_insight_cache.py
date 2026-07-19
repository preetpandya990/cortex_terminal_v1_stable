"""
Unit tests for the Portfolio-Insight ML-param cache contract
(``app.services.paper_trading.insight_cache``).

Covers:
  • derive_prob_up — the ordinal-expected-value 3-class → scalar collapse,
    including HOLD-neutrality, robustness to an undecided model, 2-class
    fallback, clamping, and the invalid-input → None contract.
  • write/read round-trip and corrupt-payload tolerance.
  • needs_refresh freshness predicate against Redis TTL sentinels.
  • request_refresh feature-gating and never-propagate guarantee.

Marked ``unit``: pure logic + an in-memory fake Redis, no real I/O.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.services.paper_trading import insight_cache

pytestmark = pytest.mark.unit


# ── In-memory fake Redis (string ops + TTL + pub/sub publish) ───────────────────

class _FakeRedis:
    """Minimal async Redis stand-in for the string/TTL/publish surface used here."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []
        self.fail_publish = False

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def get(self, key: str):
        return self.store.get(key)

    async def ttl(self, key: str) -> int:
        if key not in self.store:
            return -2                      # missing
        return self.ttls.get(key, -1)      # -1 = no expiry set

    async def publish(self, channel: str, payload: str) -> int:
        if self.fail_publish:
            raise RuntimeError("boom")
        self.published.append((channel, payload))
        return 1


# ──────────────────────────────────────────────────────────────────────────────
# derive_prob_up
# ──────────────────────────────────────────────────────────────────────────────

class TestDeriveProbUp:
    def test_strong_up_and_down(self):
        assert insight_cache.derive_prob_up({"buy": 0.8, "hold": 0.1, "sell": 0.1}) == pytest.approx(0.85)
        assert insight_cache.derive_prob_up({"buy": 0.1, "hold": 0.1, "sell": 0.8}) == pytest.approx(0.15)

    def test_undecided_hold_is_neutral(self):
        assert insight_cache.derive_prob_up({"buy": 0.05, "hold": 0.90, "sell": 0.05}) == pytest.approx(0.5)

    def test_neutral_whenever_buy_equals_sell(self):
        # Ordinal expected value is exactly 0.5 when buy==sell, for ANY hold mass.
        for hold in (0.0, 0.2, 0.6, 0.98):
            buy = sell = (1.0 - hold) / 2.0
            assert insight_cache.derive_prob_up({"buy": buy, "hold": hold, "sell": sell}) == pytest.approx(0.5)

    def test_mixed_case(self):
        assert insight_cache.derive_prob_up({"buy": 0.4, "hold": 0.4, "sell": 0.2}) == pytest.approx(0.6)

    def test_two_class_no_hold_reduces_to_p_up(self):
        # Binary model: no hold key ⇒ prob_up == P(buy)/(P(buy)+P(sell)).
        assert insight_cache.derive_prob_up({"buy": 0.7, "sell": 0.3}) == pytest.approx(0.7)

    def test_unnormalised_input_is_normalised(self):
        # Calibrated classes need not sum to 1; the ratio still lands in [0,1].
        p = insight_cache.derive_prob_up({"buy": 0.6, "hold": 0.2, "sell": 0.6})
        assert p == pytest.approx((0.6 + 0.1) / 1.4)
        assert 0.0 <= p <= 1.0

    def test_negative_dust_is_clamped(self):
        p = insight_cache.derive_prob_up({"buy": 0.7, "hold": -0.001, "sell": 0.3})
        assert 0.0 <= p <= 1.0

    @pytest.mark.parametrize("probs", [
        None, {}, {"buy": 0.5}, {"sell": 0.5}, {"buy": None, "sell": 0.5},
        {"buy": "x", "sell": 0.5}, {"buy": float("nan"), "sell": 0.5},
        {"buy": 0.0, "hold": 0.0, "sell": 0.0},
    ])
    def test_invalid_inputs_return_none(self, probs):
        assert insight_cache.derive_prob_up(probs) is None

    def test_output_always_probability(self):
        assert 0.0 <= insight_cache.derive_prob_up({"buy": 1.0, "hold": 0.0, "sell": 0.0}) <= 1.0
        assert 0.0 <= insight_cache.derive_prob_up({"buy": 0.0, "hold": 0.0, "sell": 1.0}) <= 1.0


# ──────────────────────────────────────────────────────────────────────────────
# write / read round-trip
# ──────────────────────────────────────────────────────────────────────────────

class TestReadWrite:
    @pytest.mark.asyncio
    async def test_round_trip(self):
        r = _FakeRedis()
        await insight_cache.write_mlparams(
            r, "NSE_EQ|X", prob_up=0.62, sigma=0.31, ttl_seconds=1800, ts=123.0
        )
        key = insight_cache.mlparams_key("NSE_EQ|X")
        assert r.ttls[key] == 1800
        got = await insight_cache.read_mlparams(r, "NSE_EQ|X")
        assert got == {"prob_up": 0.62, "sigma": 0.31, "ts": 123.0}

    @pytest.mark.asyncio
    async def test_missing_key_returns_none(self):
        assert await insight_cache.read_mlparams(_FakeRedis(), "absent") is None

    @pytest.mark.asyncio
    async def test_corrupt_payload_treated_as_miss(self):
        r = _FakeRedis()
        r.store[insight_cache.mlparams_key("k")] = "not json{"
        assert await insight_cache.read_mlparams(r, "k") is None

    @pytest.mark.asyncio
    async def test_payload_without_prob_up_is_miss(self):
        r = _FakeRedis()
        r.store[insight_cache.mlparams_key("k")] = json.dumps({"sigma": 0.2})
        assert await insight_cache.read_mlparams(r, "k") is None


# ──────────────────────────────────────────────────────────────────────────────
# needs_refresh
# ──────────────────────────────────────────────────────────────────────────────

class TestNeedsRefresh:
    @pytest.mark.asyncio
    async def test_missing_key_needs_refresh(self):
        assert await insight_cache.needs_refresh(_FakeRedis(), "absent", margin_seconds=600) is True

    @pytest.mark.asyncio
    async def test_key_without_expiry_needs_refresh(self):
        r = _FakeRedis()
        r.store[insight_cache.mlparams_key("k")] = "{}"  # ttl() → -1
        assert await insight_cache.needs_refresh(r, "k", margin_seconds=600) is True

    @pytest.mark.asyncio
    async def test_fresh_key_skipped(self):
        r = _FakeRedis()
        await insight_cache.write_mlparams(r, "k", prob_up=0.5, sigma=0.2, ttl_seconds=1800)
        assert await insight_cache.needs_refresh(r, "k", margin_seconds=600) is False

    @pytest.mark.asyncio
    async def test_expiring_soon_needs_refresh(self):
        r = _FakeRedis()
        await insight_cache.write_mlparams(r, "k", prob_up=0.5, sigma=0.2, ttl_seconds=300)
        assert await insight_cache.needs_refresh(r, "k", margin_seconds=600) is True


# ──────────────────────────────────────────────────────────────────────────────
# request_refresh — gating + never-propagate
# ──────────────────────────────────────────────────────────────────────────────

class TestRequestRefresh:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", False)
        r = _FakeRedis()
        await insight_cache.request_refresh(r, "NSE_EQ|X")
        assert r.published == []

    @pytest.mark.asyncio
    async def test_publishes_when_enabled(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", True)
        r = _FakeRedis()
        await insight_cache.request_refresh(r, "NSE_EQ|X")
        assert len(r.published) == 1
        channel, payload = r.published[0]
        assert channel == "cai:paper:insight:refresh_request"
        assert json.loads(payload) == {"instrument_key": "NSE_EQ|X"}

    @pytest.mark.asyncio
    async def test_empty_key_not_published(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", True)
        r = _FakeRedis()
        await insight_cache.request_refresh(r, "")
        assert r.published == []

    @pytest.mark.asyncio
    async def test_publish_failure_swallowed(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "INSIGHT_ENABLED", True)
        r = _FakeRedis()
        r.fail_publish = True
        # Must not raise — the order path must never break on a refresh publish.
        await insight_cache.request_refresh(r, "NSE_EQ|X")
