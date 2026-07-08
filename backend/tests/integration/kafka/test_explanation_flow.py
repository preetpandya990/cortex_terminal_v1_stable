"""
End-to-end explanation-job flow against the real broker, real Redis, and a
REAL trade_suggestions row from the dev DB.  The Gemini boundary
(_generate_explanation) is faked per the owner directive so tests never
burn RPD quota.

Covers: success → commit; rate-limit → attempts-header increment; DLQ at
the attempts cap; quota → DLQ + failed-state SSE event; crash-before-commit
→ redelivery from the committed offset.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.intelligence.explanation_worker import (
    MAX_ATTEMPTS,
    _process_explanation_message,
)
from app.core.config import get_settings
from app.core.kafka import (
    KafkaGroups,
    KafkaTopics,
    get_attempts,
    new_consumer,
    pending_count,
    publish,
    retry_headers,
)
from app.core.redis import RedisStreams
from tests.integration.kafka.conftest import fast_forward, read_one, tail_probe

pytestmark = pytest.mark.integration

_GROUP = KafkaGroups.EXPLANATION_WORKERS
_TOPIC = KafkaTopics.EXPLANATION_JOBS


@pytest.fixture
async def real_suggestion():
    """A real trade_suggestions row from the dev DB (skips when absent)."""
    engine = create_async_engine(
        str(get_settings().DATABASE_URL), poolclass=NullPool, echo=False
    )
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT suggestion_id, id, instrument_key FROM trade_suggestions "
                "ORDER BY created_at DESC LIMIT 1"
            ))).first()
    finally:
        await engine.dispose()
    if row is None:
        pytest.skip("dev DB has no trade_suggestions rows")
    return {
        "suggestion_id": str(row.suggestion_id),
        "id": str(row.id),
        "instrument_key": row.instrument_key,
    }


async def _consume_one(consumer, timeout_ms: int = 15_000):
    msg = await read_one(consumer, timeout_ms=timeout_ms)
    assert msg is not None, "expected a message on the explanation topic"
    return msg


@pytest.fixture
async def worker_consumer(kafka):
    """A group consumer positioned past all prior history."""
    await fast_forward(_TOPIC, _GROUP)
    consumer = new_consumer(_TOPIC, group_id=_GROUP, max_poll_records=1)
    await consumer.start()
    yield consumer
    await consumer.stop()


@pytest.mark.asyncio
async def test_success_commits_offset(redis, worker_consumer, real_suggestion):
    await publish(_TOPIC, real_suggestion, key=real_suggestion["suggestion_id"])
    msg = await _consume_one(worker_consumer)

    with patch(
        "app.ai.intelligence.explanation_worker._generate_explanation",
        new=AsyncMock(return_value=None),
    ) as fake_gemini:
        await _process_explanation_message(redis, worker_consumer, msg, 0)

    fake_gemini.assert_awaited_once_with(
        real_suggestion["suggestion_id"], int(real_suggestion["id"]), trigger="auto"
    )
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_rate_limit_republishes_with_incremented_attempts(
    redis, worker_consumer, real_suggestion
):
    from app.ai.intelligence.llm_client import GeminiRateLimitError

    probe = await tail_probe(_TOPIC)
    try:
        await publish(_TOPIC, real_suggestion, key=real_suggestion["suggestion_id"])
        msg = await _consume_one(worker_consumer)
        assert get_attempts(msg) == 0

        with patch(
            "app.ai.intelligence.explanation_worker._generate_explanation",
            new=AsyncMock(side_effect=GeminiRateLimitError("429")),
        ):
            await _process_explanation_message(redis, worker_consumer, msg, 0)

        # Probe sees the original AND the retry republish — the retry is the
        # one with attempts=1 and a future not_before.
        first = await read_one(probe)
        retry = await read_one(probe)
        assert retry is not None
        assert retry.value == real_suggestion
        assert get_attempts(retry) == 1
    finally:
        await probe.stop()
    # The retry itself is still pending for the group (deliberately).
    assert await pending_count(_TOPIC, _GROUP) == 1


@pytest.mark.asyncio
async def test_max_attempts_goes_to_dlq(redis, worker_consumer, real_suggestion):
    dlq_probe = await tail_probe(KafkaTopics.EXPLANATION_DLQ)
    try:
        await publish(
            _TOPIC, real_suggestion,
            key=real_suggestion["suggestion_id"],
            headers=retry_headers(MAX_ATTEMPTS),  # attempts cap reached
        )
        msg = await _consume_one(worker_consumer)

        with patch(
            "app.ai.intelligence.explanation_worker._generate_explanation",
            new=AsyncMock(return_value=None),
        ) as fake_gemini:
            await _process_explanation_message(redis, worker_consumer, msg, 0)

        fake_gemini.assert_not_awaited()  # capped before any LLM work
        dlq_msg = await read_one(dlq_probe)
        assert dlq_msg is not None
        assert dlq_msg.value["reason"] == "max_attempts_exceeded"
        assert dlq_msg.value["fields"] == real_suggestion
        assert dlq_msg.value["attempts"] == MAX_ATTEMPTS
    finally:
        await dlq_probe.stop()
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_quota_exhausted_dlqs_and_publishes_failed_state(
    redis, worker_consumer, real_suggestion
):
    from app.ai.intelligence.llm_client import GeminiQuotaExhausted

    sse_key = RedisStreams.sse_explanation_key(real_suggestion["suggestion_id"])
    await redis.delete(sse_key)
    dlq_probe = await tail_probe(KafkaTopics.EXPLANATION_DLQ)
    try:
        await publish(_TOPIC, real_suggestion, key=real_suggestion["suggestion_id"])
        msg = await _consume_one(worker_consumer)

        with patch(
            "app.ai.intelligence.explanation_worker._generate_explanation",
            new=AsyncMock(side_effect=GeminiQuotaExhausted("RPD exhausted")),
        ):
            await _process_explanation_message(redis, worker_consumer, msg, 0)

        dlq_msg = await read_one(dlq_probe)
        assert dlq_msg is not None
        assert dlq_msg.value["reason"] == "gemini_quota_exhausted"

        # Failed-state SSE event written so the UI can stop the skeleton.
        entries = await redis.xrevrange(sse_key, max="+", min="-", count=1)
        assert entries, "expected a failed-state SSE event"
        payload = json.loads(entries[0][1]["data"])
        assert payload["failed"] is True
    finally:
        await dlq_probe.stop()
        await redis.delete(sse_key)
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_crash_before_commit_redelivers(kafka, redis, real_suggestion):
    """Kill the consumer between processing and commit → a new group member
    receives the same message again (the PEL-drain replacement)."""
    await fast_forward(_TOPIC, _GROUP)
    marker = uuid.uuid4().hex
    job = dict(real_suggestion, marker=marker)
    await publish(_TOPIC, job, key=real_suggestion["suggestion_id"])

    # First consumer reads the message but "crashes" (stops) without commit.
    first = new_consumer(_TOPIC, group_id=_GROUP, max_poll_records=1)
    await first.start()
    try:
        msg = await read_one(first, timeout_ms=15_000)
        assert msg is not None and msg.value["marker"] == marker
    finally:
        await first.stop()  # no commit issued

    # Restarted consumer must be redelivered the same message.
    second = new_consumer(_TOPIC, group_id=_GROUP, max_poll_records=1)
    await second.start()
    try:
        redelivered = await read_one(second, timeout_ms=15_000)
        assert redelivered is not None
        assert redelivered.value["marker"] == marker
        await second.commit()
    finally:
        await second.stop()
