"""
Fixtures for the Kafka (Redpanda) integration suite.

These tests run against the REAL dev broker (localhost:19092 unless
KAFKA_BOOTSTRAP_SERVERS says otherwise), the real dev Redis, and real rows
from the dev DB.  Only the Gemini boundary is faked (owner directive — real
LLM calls would burn RPD quota).

Every module here is skipped automatically when the broker is unreachable,
so the suite is safe in environments without Redpanda.

Isolation strategy: tests share the production topic names, so each test
first FAST-FORWARDS its consumer group's committed offsets to the topic end
(so it only observes messages it publishes itself) and uses tail-positioned
probe consumers for asserting republishes.
"""
from __future__ import annotations

import socket
import uuid

import pytest
from aiokafka.structs import TopicPartition

from app.core.config import get_settings
from app.core.kafka import _TOPIC_SPECS, close_kafka, init_kafka, new_consumer
from app.core.redis import close_redis, get_redis, init_redis

settings = get_settings()


def _broker_reachable() -> bool:
    # Parse "host:port" (first entry if a list).
    first = settings.KAFKA_BOOTSTRAP_SERVERS.split(",")[0]
    host, _, port = first.rpartition(":")
    try:
        with socket.create_connection((host or "localhost", int(port)), timeout=2):
            return True
    except OSError:
        return False


_BROKER_UP = _broker_reachable()


def pytest_collection_modifyitems(config, items):
    if _BROKER_UP:
        return
    skip = pytest.mark.skip(
        reason=f"Redpanda broker unreachable at {settings.KAFKA_BOOTSTRAP_SERVERS}"
    )
    for item in items:
        item.add_marker(skip)
        item.add_marker(pytest.mark.integration)


@pytest.fixture
async def kafka():
    """Initialize the Kafka singletons on THIS test's event loop and tear
    them down after — clients cannot survive across pytest-asyncio's
    function-scoped loops."""
    await init_kafka()
    yield
    await close_kafka()


@pytest.fixture
async def redis(kafka):
    await init_redis()
    yield get_redis()
    await close_redis()


def _topic_partitions(topic: str) -> list[TopicPartition]:
    return [TopicPartition(topic, p) for p in range(_TOPIC_SPECS[topic][0])]


async def fast_forward(topic: str, group_id: str) -> None:
    """Commit the group's offsets to the topic end so the test only sees
    messages it publishes itself (prior test runs left history behind)."""
    consumer = new_consumer(group_id=group_id)
    tps = _topic_partitions(topic)
    consumer.assign(tps)
    await consumer.start()
    try:
        ends = await consumer.end_offsets(tps)
        await consumer.commit({tp: ends[tp] for tp in tps})
    finally:
        await consumer.stop()


async def tail_probe(topic: str):
    """A started consumer positioned at the topic end — reads only messages
    published after this call.  Caller owns stop()."""
    consumer = new_consumer(group_id=f"probe-{uuid.uuid4().hex}")
    tps = _topic_partitions(topic)
    consumer.assign(tps)
    await consumer.start()
    await consumer.seek_to_end(*tps)
    return consumer


async def read_one(consumer, timeout_ms: int = 10_000):
    """Next record from any assigned partition, or None on timeout."""
    batches = await consumer.getmany(timeout_ms=timeout_ms, max_records=1)
    for records in batches.values():
        if records:
            return records[0]
    return None
