"""
Forecast-batch queue flow against the real broker, using a REAL symbol from
the dev DB's instrument_master.  The Gemini boundary (_flush_batch) is faked.

Covers: commit-only-after-success (inject failure → items stay pending and
are redelivered on the next dispatch) and the staleness skip.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

import app.ai.fusion.forecast_batch_worker as fbw
from app.core.config import get_settings
from app.core.kafka import KafkaGroups, KafkaTopics, pending_count, publish
from tests.integration.kafka.conftest import fast_forward

pytestmark = pytest.mark.integration

_TOPIC = KafkaTopics.FORECAST_BATCH
_GROUP = KafkaGroups.FORECAST_BATCH


@pytest.fixture
async def real_symbol():
    engine = create_async_engine(
        str(get_settings().DATABASE_URL), poolclass=NullPool, echo=False
    )
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT trading_symbol FROM instrument_master "
                "WHERE is_active LIMIT 1"
            ))).first()
    finally:
        await engine.dispose()
    if row is None:
        pytest.skip("dev DB has no active instrument_master rows")
    return row.trading_symbol


def _payload(symbol: str, *, age_secs: int = 0) -> dict:
    enqueued = datetime.now(timezone.utc) - timedelta(seconds=age_secs)
    return {
        "symbol": symbol,
        "events": [],
        "indicators": {"rsi_14": 55.2},
        "regime": "neutral",
        "cache_key": f"cortex:news_forecast:{symbol}:{uuid.uuid4().hex[:12]}",
        "enqueued_at": enqueued.isoformat(),
    }


@pytest.fixture
async def fresh_queue_consumer(kafka):
    """Give the module a fresh ForecastQueueConsumer bound to THIS event
    loop, fast-forwarded past prior history; stop it after the test."""
    await fast_forward(_TOPIC, _GROUP)
    original = fbw._queue_consumer
    fbw._queue_consumer = fbw.ForecastQueueConsumer()
    yield fbw._queue_consumer
    await fbw._queue_consumer.stop()
    fbw._queue_consumer = original


@pytest.mark.asyncio
async def test_flush_commits_only_after_success(redis, fresh_queue_consumer, real_symbol):
    await publish(_TOPIC, _payload(real_symbol), key=real_symbol)
    assert await pending_count(_TOPIC, _GROUP) == 1

    with patch.object(
        fbw, "_flush_batch",
        new=AsyncMock(return_value={"outcome": "success", "valid_count": 1, "total": 1}),
    ) as fake_gemini:
        result = await fbw.flush_pending_forecasts(redis, MagicMock())

    assert result == {"dispatched": 1, "calls_made": 1}
    fake_gemini.assert_awaited_once()
    (batch, _redis_arg, _sf), _ = fake_gemini.await_args
    assert batch[0]["symbol"] == real_symbol
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_failed_flush_leaves_items_pending_for_redelivery(
    redis, fresh_queue_consumer, real_symbol
):
    await publish(_TOPIC, _payload(real_symbol), key=real_symbol)

    with patch.object(
        fbw, "_flush_batch",
        new=AsyncMock(
            return_value={"outcome": "budget_throttled", "valid_count": 0, "total": 1}
        ),
    ):
        with pytest.raises(RuntimeError, match="budget_throttled"):
            await fbw.flush_pending_forecasts(redis, MagicMock())

    # NOT committed — the item is still pending...
    assert await pending_count(_TOPIC, _GROUP) == 1

    # ...and the very same consumer redelivers it on the next dispatch.
    with patch.object(
        fbw, "_flush_batch",
        new=AsyncMock(return_value={"outcome": "success", "valid_count": 1, "total": 1}),
    ):
        result = await fbw.flush_pending_forecasts(redis, MagicMock())

    assert result["dispatched"] == 1
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_stale_payloads_are_skipped_and_committed(
    redis, fresh_queue_consumer, real_symbol
):
    """Payloads older than the 1h staleness gate are dropped without a
    Gemini call, and their offsets are committed past."""
    await publish(_TOPIC, _payload(real_symbol, age_secs=2 * 3600), key=real_symbol)

    with patch.object(fbw, "_flush_batch", new=AsyncMock()) as fake_gemini:
        result = await fbw.flush_pending_forecasts(redis, MagicMock())

    fake_gemini.assert_not_awaited()
    assert result == {"dispatched": 0, "calls_made": 0}
    assert await pending_count(_TOPIC, _GROUP) == 0
