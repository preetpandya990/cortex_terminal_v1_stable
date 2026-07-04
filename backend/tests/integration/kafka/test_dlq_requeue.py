"""
DLQ quota-recovery drain against the real broker and Redis: eligibility by
moved_at (previous quota day only), Redis SET NX dedup skip, non-quota
entries committed past, not-yet-eligible entries recycled to the DLQ tail,
and publish-failure rollback (dedup key deleted, offset NOT committed).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.ai.intelligence.explanation_worker import _requeue_quota_dlq_entries
from app.core.kafka import (
    KafkaGroups,
    KafkaTopics,
    get_attempts,
    pending_count,
    publish,
)
from tests.integration.kafka.conftest import fast_forward, read_one, tail_probe

pytestmark = pytest.mark.integration

_DLQ = KafkaTopics.EXPLANATION_DLQ
_GROUP = KafkaGroups.DLQ_REQUEUE


def _dlq_entry(suggestion_id: str, reason: str, moved_at: datetime) -> dict:
    return {
        "original_topic": KafkaTopics.EXPLANATION_JOBS,
        "reason": reason,
        "fields": {
            "suggestion_id": suggestion_id,
            "id": "1",
            "instrument_key": "NSE_EQ|TEST",
        },
        "attempts": 3,
        "moved_at": moved_at.isoformat(),
    }


def _yesterday() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=2)


@pytest.fixture
async def clean_group(kafka):
    await fast_forward(_DLQ, _GROUP)
    yield


async def _cleanup_dedup(redis, *suggestion_ids: str) -> None:
    for sid in suggestion_ids:
        await redis.delete(f"cortex:gemini:dlq:requeued:{sid}")


@pytest.mark.asyncio
async def test_eligibility_and_recycling(redis, clean_group):
    """Eligible quota entry requeues with attempts=0; today's entry recycles
    to the DLQ tail; non-quota entry is committed past."""
    eligible_sid = uuid.uuid4().hex
    fresh_sid = uuid.uuid4().hex
    terminal_sid = uuid.uuid4().hex
    await _cleanup_dedup(redis, eligible_sid, fresh_sid, terminal_sid)

    await publish(_DLQ, _dlq_entry(eligible_sid, "gemini_quota_exhausted", _yesterday()),
                  key=eligible_sid)
    await publish(_DLQ, _dlq_entry(fresh_sid, "gemini_quota_exhausted",
                                   datetime.now(timezone.utc)), key=fresh_sid)
    await publish(_DLQ, _dlq_entry(terminal_sid, "max_attempts_exceeded", _yesterday()),
                  key=terminal_sid)

    jobs_probe = await tail_probe(KafkaTopics.EXPLANATION_JOBS)
    dlq_probe = await tail_probe(_DLQ)
    try:
        requeued = await _requeue_quota_dlq_entries(redis, trigger="boot")
        assert requeued == 1

        # The eligible entry landed on the jobs topic with a fresh budget.
        job = await read_one(jobs_probe)
        assert job is not None
        assert job.value["suggestion_id"] == eligible_sid
        assert get_attempts(job) == 0
        assert await read_one(jobs_probe, timeout_ms=1_500) is None  # only one

        # The not-yet-eligible entry was recycled to the DLQ tail.
        recycled = await read_one(dlq_probe)
        assert recycled is not None
        assert recycled.value["fields"]["suggestion_id"] == fresh_sid

        # Everything up to the snapshot was committed (the recycled copy is
        # beyond the snapshot and stays pending for the next trigger).
        assert await pending_count(_DLQ, _GROUP) == 1
    finally:
        await jobs_probe.stop()
        await dlq_probe.stop()
        await _cleanup_dedup(redis, eligible_sid, fresh_sid, terminal_sid)
        await fast_forward(_DLQ, _GROUP)


@pytest.mark.asyncio
async def test_dedup_key_skips_already_requeued(redis, clean_group):
    sid = uuid.uuid4().hex
    dedup_key = f"cortex:gemini:dlq:requeued:{sid}"
    await redis.set(dedup_key, "1", ex=600)  # already requeued this cycle

    await publish(_DLQ, _dlq_entry(sid, "gemini_quota_exhausted", _yesterday()), key=sid)

    jobs_probe = await tail_probe(KafkaTopics.EXPLANATION_JOBS)
    try:
        requeued = await _requeue_quota_dlq_entries(redis, trigger="quota_reset")
        assert requeued == 0
        assert await read_one(jobs_probe, timeout_ms=1_500) is None
        assert await pending_count(_DLQ, _GROUP) == 0  # committed past it
    finally:
        await jobs_probe.stop()
        await redis.delete(dedup_key)


@pytest.mark.asyncio
async def test_publish_failure_rolls_back_dedup_and_offset(redis, clean_group):
    """A jobs-topic publish failure must delete the dedup key, NOT commit the
    offset, and abort the drain — the entry is retried on the next trigger."""
    sid = uuid.uuid4().hex
    dedup_key = f"cortex:gemini:dlq:requeued:{sid}"
    await redis.delete(dedup_key)

    await publish(_DLQ, _dlq_entry(sid, "gemini_quota_exhausted", _yesterday()), key=sid)

    real_publish = publish

    async def _failing_publish(topic, value, *, key=None, headers=None):
        if topic == KafkaTopics.EXPLANATION_JOBS:
            raise ConnectionError("broker gone")
        await real_publish(topic, value, key=key, headers=headers)

    with patch(
        "app.ai.intelligence.explanation_worker.publish",
        new=AsyncMock(side_effect=_failing_publish),
    ):
        requeued = await _requeue_quota_dlq_entries(redis, trigger="boot")

    assert requeued == 0
    assert not await redis.exists(dedup_key)          # rolled back
    assert await pending_count(_DLQ, _GROUP) == 1     # offset NOT committed

    # Next (unpatched) trigger succeeds and drains it.
    requeued = await _requeue_quota_dlq_entries(redis, trigger="boot")
    assert requeued == 1
    assert await pending_count(_DLQ, _GROUP) == 0
    await redis.delete(dedup_key)
