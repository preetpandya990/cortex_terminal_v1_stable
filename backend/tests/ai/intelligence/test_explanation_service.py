"""
Explanation service tests (WS7)
===============================
The demand-side state machine: ready/generating/weak_signal/failed
classification, first-viewer-publishes semantics, lock contention, and the
fail-open/fail-terminal policies.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from app.ai.intelligence.explanation_service import (
    DEMAND_INFLIGHT_KEY,
    ensure_explanation,
    publish_explanation_job,
)

pytestmark = pytest.mark.unit


def _suggestion(
    *,
    llm_summary: str | None = None,
    consensus_score: str = "82.0",
) -> MagicMock:
    s = MagicMock()
    s.suggestion_id = uuid4()
    s.id = 42
    s.instrument_key = "NSE_EQ|INE002A01018"
    s.symbol = "RELIANCE"
    s.llm_summary = llm_summary
    s.consensus_score = Decimal(consensus_score)
    return s


def _settings(on_demand: bool, threshold: float = 75.0) -> MagicMock:
    settings = MagicMock()
    settings.EXPLANATION_ON_DEMAND = on_demand
    settings.EXPLANATION_CONSENSUS_THRESHOLD = threshold
    return settings


def _patch(on_demand: bool):
    return patch(
        "app.ai.intelligence.explanation_service.get_settings",
        return_value=_settings(on_demand),
    )


class TestReadyState:
    @pytest.mark.asyncio
    async def test_existing_explanation_is_ready_in_both_modes(self):
        for mode in (False, True):
            with _patch(mode):
                state = await ensure_explanation(AsyncMock(), _suggestion(llm_summary="done"))
            assert state["status"] == "ready"


class TestLegacyMode:
    @pytest.mark.asyncio
    async def test_weak_signal_below_threshold(self):
        with _patch(False):
            state = await ensure_explanation(
                AsyncMock(), _suggestion(consensus_score="60.0")
            )
        assert state["status"] == "weak_signal"

    @pytest.mark.asyncio
    async def test_strong_signal_is_generating_without_publish(self):
        redis = AsyncMock()
        with (
            _patch(False),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(),
            ) as publish,
        ):
            state = await ensure_explanation(redis, _suggestion(consensus_score="90.0"))
        assert state["status"] == "generating"
        publish.assert_not_awaited()  # legacy: the engine already published
        redis.set.assert_not_awaited()  # no demand lock in legacy mode


class TestOnDemandMode:
    @pytest.mark.asyncio
    async def test_first_viewer_publishes_demand_job(self):
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)  # lock acquired
        suggestion = _suggestion()
        with (
            _patch(True),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(),
            ) as publish,
        ):
            state = await ensure_explanation(redis, suggestion)

        assert state["status"] == "generating"
        publish.assert_awaited_once()
        topic, payload = publish.await_args.args[0], publish.await_args.args[1]
        assert payload["trigger"] == "demand"
        assert payload["suggestion_id"] == str(suggestion.suggestion_id)

    @pytest.mark.asyncio
    async def test_second_viewer_sees_generating_without_second_job(self):
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=None)  # lock already held
        with (
            _patch(True),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(),
            ) as publish,
        ):
            state = await ensure_explanation(redis, _suggestion())

        assert state["status"] == "generating"
        publish.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_weak_signals_also_generate_on_demand(self):
        """On-demand mode: demand IS the gate — the consensus threshold
        applies only to the legacy auto-publish path."""
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        with (
            _patch(True),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(),
            ) as publish,
        ):
            state = await ensure_explanation(redis, _suggestion(consensus_score="55.0"))

        assert state["status"] == "generating"
        publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publish_failure_frees_lock_and_reports_failed(self):
        redis = AsyncMock()
        redis.set = AsyncMock(return_value=True)
        suggestion = _suggestion()
        with (
            _patch(True),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(side_effect=RuntimeError("broker down")),
            ),
        ):
            state = await ensure_explanation(redis, suggestion)

        assert state["status"] == "failed"
        redis.delete.assert_awaited_once_with(
            DEMAND_INFLIGHT_KEY.format(suggestion_id=str(suggestion.suggestion_id))
        )

    @pytest.mark.asyncio
    async def test_redis_failure_fails_open_and_still_publishes(self):
        """Redis down must not strand the user on a skeleton — publish anyway
        (the worker's own in-flight key + DB idempotency absorb a duplicate)."""
        redis = AsyncMock()
        redis.set = AsyncMock(side_effect=ConnectionError("redis down"))
        with (
            _patch(True),
            patch(
                "app.ai.intelligence.explanation_service.kafka_publish",
                new=AsyncMock(),
            ) as publish,
        ):
            state = await ensure_explanation(redis, _suggestion())

        assert state["status"] == "generating"
        publish.assert_awaited_once()


class TestPublishExplanationJob:
    @pytest.mark.asyncio
    async def test_payload_shape_and_key(self):
        with patch(
            "app.ai.intelligence.explanation_service.kafka_publish",
            new=AsyncMock(),
        ) as publish:
            await publish_explanation_job("sid-1", 7, "NSE_EQ|X", trigger="bypass")

        _topic, payload = publish.await_args.args
        assert payload == {
            "suggestion_id": "sid-1",
            "id": "7",
            "instrument_key": "NSE_EQ|X",
            "trigger": "bypass",
        }
        assert publish.await_args.kwargs["key"] == "sid-1"
