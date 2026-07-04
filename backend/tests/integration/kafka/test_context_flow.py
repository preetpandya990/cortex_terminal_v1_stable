"""
Instrument-context job flow against the real broker and Redis: guard-key
skips (recent-success, cooldown), the attempts cap, and the not_before
hybrid rule (short deadline → sleep; long deadline → republish-to-tail).
The Gemini boundary (_generate_instrument_context) is faked.
"""
from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.intelligence.explanation_worker import (
    _MAX_CONTEXT_ATTEMPTS,
    _process_context_message,
)
from app.core.kafka import (
    KafkaGroups,
    KafkaTopics,
    get_attempts,
    get_not_before,
    new_consumer,
    pending_count,
    publish,
    retry_headers,
)
from tests.integration.kafka.conftest import fast_forward, read_one, tail_probe

pytestmark = pytest.mark.integration

_GROUP = KafkaGroups.CONTEXT_WORKERS
_TOPIC = KafkaTopics.CONTEXT_JOBS


def _job(instrument_key: str) -> dict:
    return {
        "instrument_key": instrument_key,
        "symbol": instrument_key.split("|")[-1],
        "prediction_data": "",
        "lock_key": "",
        "lock_token": "",
        "force": "1",
        "source": "watchlist_scheduler",
    }


@pytest.fixture
async def worker_consumer(kafka):
    await fast_forward(_TOPIC, _GROUP)
    consumer = new_consumer(_TOPIC, group_id=_GROUP, max_poll_records=1)
    await consumer.start()
    yield consumer
    await consumer.stop()


@pytest.fixture
def fake_generate():
    with patch(
        "app.ai.intelligence.explanation_worker._generate_instrument_context",
        new=AsyncMock(return_value=None),
    ) as mock:
        yield mock


async def _consume_one(consumer, timeout_ms: int = 15_000):
    msg = await read_one(consumer, timeout_ms=timeout_ms)
    assert msg is not None, "expected a message on the context topic"
    return msg


@pytest.mark.asyncio
async def test_success_commits_and_sets_recent_key(redis, worker_consumer, fake_generate):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    recent_key = f"cortex:context:recent:{instrument}"
    await redis.delete(recent_key)

    await publish(_TOPIC, _job(instrument), key=instrument)
    msg = await _consume_one(worker_consumer)
    await _process_context_message(redis, worker_consumer, msg)

    fake_generate.assert_awaited_once()
    assert await redis.exists(recent_key)
    assert await pending_count(_TOPIC, _GROUP) == 0
    await redis.delete(recent_key)


@pytest.mark.asyncio
async def test_recent_key_skips_duplicate(redis, worker_consumer, fake_generate):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    recent_key = f"cortex:context:recent:{instrument}"
    await redis.set(recent_key, "1", ex=60)

    await publish(_TOPIC, _job(instrument), key=instrument)
    msg = await _consume_one(worker_consumer)
    await _process_context_message(redis, worker_consumer, msg)

    fake_generate.assert_not_awaited()
    assert await pending_count(_TOPIC, _GROUP) == 0
    await redis.delete(recent_key)


@pytest.mark.asyncio
async def test_cooldown_republishes_with_matching_deadline(
    redis, worker_consumer, fake_generate
):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    cooldown_key = f"cortex:context:cooldown:{instrument}"
    await redis.set(cooldown_key, "1", ex=120)

    probe = await tail_probe(_TOPIC)
    try:
        await publish(_TOPIC, _job(instrument), key=instrument)
        msg = await _consume_one(worker_consumer)
        await _process_context_message(redis, worker_consumer, msg)

        fake_generate.assert_not_awaited()
        original = await read_one(probe)
        republished = await read_one(probe)
        assert republished is not None
        # SAME attempts (cooldown is not a failed attempt), deadline ≈ cooldown TTL.
        assert get_attempts(republished) == 0
        assert time.time() + 60 < get_not_before(republished) <= time.time() + 125
    finally:
        await probe.stop()
        await redis.delete(cooldown_key)


@pytest.mark.asyncio
async def test_abandon_at_attempts_cap(redis, worker_consumer, fake_generate):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    await publish(
        _TOPIC, _job(instrument), key=instrument,
        headers=retry_headers(_MAX_CONTEXT_ATTEMPTS),
    )
    msg = await _consume_one(worker_consumer)
    await _process_context_message(redis, worker_consumer, msg)

    fake_generate.assert_not_awaited()
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_not_before_short_deadline_sleeps_then_processes(
    redis, worker_consumer, fake_generate
):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    await publish(
        _TOPIC, _job(instrument), key=instrument,
        headers=retry_headers(1, 1.5),  # 1.5s away — inside the sleep cap
    )
    msg = await _consume_one(worker_consumer)
    t0 = time.monotonic()
    await _process_context_message(redis, worker_consumer, msg)
    elapsed = time.monotonic() - t0

    fake_generate.assert_awaited_once()
    assert elapsed >= 1.0  # actually slept off the deadline
    assert await pending_count(_TOPIC, _GROUP) == 0
    await redis.delete(f"cortex:context:recent:{instrument}")


@pytest.mark.asyncio
async def test_not_before_long_deadline_republishes_unchanged(
    redis, worker_consumer, fake_generate
):
    instrument = f"NSE_EQ|TEST{uuid.uuid4().hex[:8]}"
    probe = await tail_probe(_TOPIC)
    try:
        await publish(
            _TOPIC, _job(instrument), key=instrument,
            headers=retry_headers(2, 120),  # beyond the 60s sleep cap
        )
        msg = await _consume_one(worker_consumer)
        deadline = get_not_before(msg)
        await _process_context_message(redis, worker_consumer, msg)

        fake_generate.assert_not_awaited()
        original = await read_one(probe)
        republished = await read_one(probe)
        assert republished is not None
        # Republished UNCHANGED: same attempts, same deadline.
        assert get_attempts(republished) == 2
        assert abs(get_not_before(republished) - deadline) < 0.001
    finally:
        await probe.stop()
