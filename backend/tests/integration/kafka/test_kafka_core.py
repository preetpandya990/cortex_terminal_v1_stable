"""
Core transport tests against the real Redpanda broker: topic-init
idempotence, JSON/header round-trips, producer idempotence configuration,
and pending_count (consumer-lag) arithmetic including the never-committed
group case.
"""
from __future__ import annotations

import time
import uuid

import pytest
from aiokafka.structs import TopicPartition

from app.core.kafka import (
    _TOPIC_SPECS,
    KafkaTopics,
    get_attempts,
    get_kafka_producer,
    get_not_before,
    init_kafka,
    new_consumer,
    pending_count,
    publish,
    retry_headers,
)
from tests.integration.kafka.conftest import read_one, tail_probe

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_init_kafka_idempotent(kafka):
    """A second init on an initialized process is a no-op; topics all exist."""
    await init_kafka()  # second call — must not raise
    producer = get_kafka_producer()
    assert producer is not None

    probe = await tail_probe(KafkaTopics.CONTEXT_JOBS)
    try:
        topics = await probe.topics()
        for topic in _TOPIC_SPECS:
            assert topic in topics
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_producer_idempotence_enabled(kafka):
    """enable_idempotence=True (which forces acks=all internally) creates a
    transaction manager — the delivery guarantee the commit-after-success
    design leans on."""
    producer = get_kafka_producer()
    assert producer._txn_manager is not None


@pytest.mark.asyncio
async def test_json_and_header_roundtrip(kafka):
    payload = {
        "suggestion_id": uuid.uuid4().hex,
        "nested": {"a": 1, "b": [1, 2, 3]},
        "unicode": "टाटा मोटर्स ₹",
    }
    probe = await tail_probe(KafkaTopics.CLASSIFIER_PENDING)
    try:
        before = time.time()
        await publish(
            KafkaTopics.CLASSIFIER_PENDING,
            payload,
            key=payload["suggestion_id"],
            headers=retry_headers(2, 60),
        )
        msg = await read_one(probe)
        assert msg is not None
        assert msg.value == payload
        assert msg.key == payload["suggestion_id"].encode()
        assert get_attempts(msg) == 2
        assert before + 55 <= get_not_before(msg) <= time.time() + 65
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_malformed_payload_deserializes_to_none(kafka):
    """A non-JSON record must decode to None (poison-skip contract), not kill
    the fetcher."""
    producer = get_kafka_producer()
    probe = await tail_probe(KafkaTopics.CLASSIFIER_PENDING)
    try:
        await producer.send_and_wait(
            KafkaTopics.CLASSIFIER_PENDING, value=b"\x00not-json{{{"
        )
        msg = await read_one(probe)
        assert msg is not None
        assert msg.value is None
    finally:
        await probe.stop()


@pytest.mark.asyncio
async def test_pending_count_arithmetic(kafka):
    """Never-committed group counts the full backlog; committing to end
    zeroes it; publishing N more raises it by exactly N."""
    group = f"lag-test-{uuid.uuid4().hex}"
    topic = KafkaTopics.CONTEXT_JOBS
    tp = TopicPartition(topic, 0)

    consumer = new_consumer(group_id=group)
    consumer.assign([tp])
    await consumer.start()
    try:
        end = (await consumer.end_offsets([tp]))[tp]
        beginning = (await consumer.beginning_offsets([tp]))[tp]

        # Never-committed group → full retained backlog.
        assert await pending_count(topic, group) == end - beginning

        # Commit to end → zero pending.
        await consumer.commit({tp: end})
        assert await pending_count(topic, group) == 0

        # Publish 3 → pending == 3.
        for i in range(3):
            await publish(topic, {"instrument_key": f"LAG|{i}"}, key=f"LAG|{i}")
        assert await pending_count(topic, group) == 3

        # Committing past them zeroes it again (cleanup for later tests).
        new_end = (await consumer.end_offsets([tp]))[tp]
        await consumer.commit({tp: new_end})
        assert await pending_count(topic, group) == 0
    finally:
        await consumer.stop()
