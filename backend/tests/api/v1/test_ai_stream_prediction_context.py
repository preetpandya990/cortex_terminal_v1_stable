from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import ai_stream

pytestmark = pytest.mark.unit


class _EmptyResult:
    def scalar_one_or_none(self):
        return None


@pytest.mark.asyncio
async def test_fetch_explanation_enqueues_with_live_prediction_snapshot(monkeypatch):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_EmptyResult(), _EmptyResult()])
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)

    snapshot = {"available": True, "direction": "BUY", "confidence": 0.91}
    get_prediction_snapshot = AsyncMock(return_value=snapshot)
    kafka_publish = AsyncMock()

    monkeypatch.setattr(ai_stream, "get_prediction_snapshot", get_prediction_snapshot)
    monkeypatch.setattr(ai_stream, "kafka_publish", kafka_publish)

    payload = await ai_stream._fetch_explanation_for_instrument(
        db=db,
        instrument_key="NSE_EQ|INE002A01018",
        symbol="RELIANCE",
        redis=redis,
        predictor=object(),
        prediction_snapshot=None,
    )

    assert payload["available"] is False
    get_prediction_snapshot.assert_awaited_once()
    kafka_publish.assert_awaited_once()
    published = kafka_publish.await_args.args[1]
    assert json.loads(published["prediction_data"]) == snapshot


@pytest.mark.asyncio
async def test_fetch_explanation_reuses_existing_prediction_snapshot(monkeypatch):
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[_EmptyResult(), _EmptyResult()])
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=True)

    get_prediction_snapshot = AsyncMock()
    kafka_publish = AsyncMock()
    existing_snapshot = {"available": True, "direction": "SELL", "confidence": 0.77}

    monkeypatch.setattr(ai_stream, "get_prediction_snapshot", get_prediction_snapshot)
    monkeypatch.setattr(ai_stream, "kafka_publish", kafka_publish)

    await ai_stream._fetch_explanation_for_instrument(
        db=db,
        instrument_key="NSE_EQ|INE002A01018",
        symbol="RELIANCE",
        redis=redis,
        predictor=object(),
        prediction_snapshot=existing_snapshot,
    )

    get_prediction_snapshot.assert_not_awaited()
    published = kafka_publish.await_args.args[1]
    assert json.loads(published["prediction_data"]) == existing_snapshot
