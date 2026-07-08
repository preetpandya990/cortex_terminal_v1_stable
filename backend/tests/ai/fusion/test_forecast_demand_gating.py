"""
Forecast demand-gating tests (WS6)
==================================
Behind FORECAST_DEMAND_GATING, gather_news_forecast enqueues background
forecasts only for in-demand symbols; gated symbols get a fallback shape the
consensus renormalizes away (F.A). Cache hits are always served regardless.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.fusion.signal_assembler import SignalAssembler

pytestmark = pytest.mark.unit


def _event_signals() -> dict:
    return {
        "score": 40.0,
        "confidence": 0.6,
        "event_count": 1,
        "events": [{"id": 1, "type": "earnings"}],
        "available": True,
    }


def _assembler() -> SignalAssembler:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)          # forecast cache miss
    redis.set = AsyncMock(return_value=True)          # dedup key acquired
    assembler = SignalAssembler(redis=redis)
    return assembler


def _settings(gating: bool) -> MagicMock:
    settings = MagicMock()
    settings.FORECAST_DEMAND_GATING = gating
    settings.FORECAST_ENQUEUE_DEDUP_TTL_SECS = 3600
    return settings


class TestDemandGating:
    @pytest.mark.asyncio
    async def test_gated_symbol_skips_enqueue_and_returns_fallback(self):
        assembler = _assembler()
        with (
            patch("app.ai.fusion.signal_assembler.get_settings", return_value=_settings(True)),
            patch("app.ai.fusion.signal_assembler.is_in_demand", new=AsyncMock(return_value=False)),
            patch("app.ai.fusion.signal_assembler.kafka_publish", new=AsyncMock()) as publish,
        ):
            result = await assembler.gather_news_forecast(
                AsyncMock(), "UNWATCHED", event_signals=_event_signals(),
            )

        publish.assert_not_awaited()
        assert result["forecast_source"] == "fallback"
        assert result["fallback_reason"] == "demand_gated"
        assert "direction" not in result  # F.A: treated as unavailable by consensus

    @pytest.mark.asyncio
    async def test_in_demand_symbol_enqueues_normally(self):
        assembler = _assembler()
        with (
            patch("app.ai.fusion.signal_assembler.get_settings", return_value=_settings(True)),
            patch("app.ai.fusion.signal_assembler.is_in_demand", new=AsyncMock(return_value=True)),
            patch("app.ai.fusion.signal_assembler.kafka_publish", new=AsyncMock()) as publish,
        ):
            result = await assembler.gather_news_forecast(
                AsyncMock(), "RELIANCE", event_signals=_event_signals(),
            )

        publish.assert_awaited_once()
        assert result["fallback_reason"] == "batch_pending"

    @pytest.mark.asyncio
    async def test_flag_off_never_checks_demand(self):
        assembler = _assembler()
        with (
            patch("app.ai.fusion.signal_assembler.get_settings", return_value=_settings(False)),
            patch(
                "app.ai.fusion.signal_assembler.is_in_demand",
                new=AsyncMock(return_value=False),
            ) as demand_check,
            patch("app.ai.fusion.signal_assembler.kafka_publish", new=AsyncMock()) as publish,
        ):
            result = await assembler.gather_news_forecast(
                AsyncMock(), "ANY", event_signals=_event_signals(),
            )

        demand_check.assert_not_awaited()  # flag off → zero behavior change
        publish.assert_awaited_once()
        assert result["fallback_reason"] == "batch_pending"

    @pytest.mark.asyncio
    async def test_cache_hit_served_even_when_gated(self):
        """A cached forecast costs nothing — gating must not hide it."""
        import json

        assembler = _assembler()
        cached = {"score": 60.0, "direction": "BUY", "forecast_source": "gemini_batch"}
        assembler._ml_cache.get = AsyncMock(return_value=json.dumps(cached))

        with (
            patch("app.ai.fusion.signal_assembler.get_settings", return_value=_settings(True)),
            patch(
                "app.ai.fusion.signal_assembler.is_in_demand",
                new=AsyncMock(return_value=False),
            ) as demand_check,
        ):
            result = await assembler.gather_news_forecast(
                AsyncMock(), "UNWATCHED", event_signals=_event_signals(),
            )

        assert result == cached
        demand_check.assert_not_awaited()  # gate sits strictly at the enqueue point

    @pytest.mark.asyncio
    async def test_dedup_key_uses_configured_ttl(self):
        assembler = _assembler()
        with (
            patch("app.ai.fusion.signal_assembler.get_settings", return_value=_settings(False)),
            patch("app.ai.fusion.signal_assembler.kafka_publish", new=AsyncMock()),
        ):
            await assembler.gather_news_forecast(
                AsyncMock(), "TCS", event_signals=_event_signals(),
            )

        set_kwargs = assembler._ml_cache.set.call_args.kwargs
        assert set_kwargs["ex"] == 3600
        assert set_kwargs["nx"] is True
