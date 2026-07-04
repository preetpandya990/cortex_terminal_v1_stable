"""
Cortex AI — Kafka (Redpanda) Transport
=======================================
Durable job queueing on Redpanda via aiokafka.  This is the single shared
transport module for every producer and consumer in the system: topic/group
topology, serialization, retry headers, and consumer-lag arithmetic all live
here so the semantics cannot drift between call sites.

Design contract (see REDPANDA_MIGRATION_MASTER_PLAN.md):

- **At-least-once** delivery: consumers use manual commit
  (``enable_auto_commit=False``) and commit only after a message reaches a
  terminal outcome (success, DLQ, abandon, or republished retry).
- **Retries** are republish-to-tail with ``attempts``/``not_before`` headers —
  Kafka has no delivery counter, so the producer of the retry carries it.
- **Crash safety** falls out of committed offsets: crash-before-commit means
  redelivery from the last committed offset on restart (the PEL-drain
  replacement, for free).
- Topics use plain ``delete`` retention, never compaction — retries reuse
  keys and compaction could drop the original record.

Redis keeps everything it is genuinely good at (cache, pub/sub, locks,
dedup/inflight keys, SSE event stores, budget counters); only durable job
queues live here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError
from aiokafka.structs import TopicPartition

from app.core.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# ── Topology ───────────────────────────────────────────────────────────────────
class KafkaTopics:
    """Kafka topic constants — one topic per durable job queue."""

    EXPLANATION_JOBS = "cortex.explanation.jobs"
    """Durable explanation job queue (2 partitions, keyed by suggestion_id)."""

    CONTEXT_JOBS = "cortex.context.jobs"
    """Durable instrument-context job queue."""

    EXPLANATION_DLQ = "cortex.explanation.dlq"
    """Dead-letter queue for explanation jobs (quota exhaustion / max attempts)."""

    FORECAST_BATCH = "cortex.forecast.batch"
    """News-forecast batch accumulator (keyed by symbol)."""

    CLASSIFIER_PENDING = "cortex.classifier.pending"
    """Pending event-classification results awaiting manual dispatch."""


class KafkaGroups:
    """Consumer group constants — one group per consuming component."""

    EXPLANATION_WORKERS = "cortex-explanation-workers"
    CONTEXT_WORKERS = "cortex-context-workers"
    DLQ_REQUEUE = "cortex-dlq-requeue"
    FORECAST_BATCH = "cortex-forecast-batch"
    CLASSIFIER_FLUSH = "cortex-classifier-flush"


_DAY_MS = 24 * 60 * 60 * 1000

# (partitions, retention_ms) per topic.  cleanup.policy is always "delete":
# retries republish under the same key, so compaction could drop the original
# record before the retry is consumed.
_TOPIC_SPECS: dict[str, tuple[int, int]] = {
    KafkaTopics.EXPLANATION_JOBS: (2, 7 * _DAY_MS),
    KafkaTopics.CONTEXT_JOBS: (1, 7 * _DAY_MS),
    KafkaTopics.EXPLANATION_DLQ: (1, 14 * _DAY_MS),
    KafkaTopics.FORECAST_BATCH: (1, 1 * _DAY_MS),
    KafkaTopics.CLASSIFIER_PENDING: (1, 7 * _DAY_MS),
}

# ── Retry headers ──────────────────────────────────────────────────────────────
# Kafka has no PEL delivery counter; retries carry their own state in two
# headers (kept deliberately minimal — per-attempt bloat multiplies record size):
#   attempts    int   — how many processing attempts have already failed
#   not_before  float — epoch seconds before which the message must not run
_HEADER_ATTEMPTS = "attempts"
_HEADER_NOT_BEFORE = "not_before"


def retry_headers(
    attempts: int,
    delay_secs: float = 0.0,
    *,
    not_before: float | None = None,
) -> list[tuple[str, bytes]]:
    """
    Build the retry header set for a republished message.

    ``not_before`` (explicit epoch seconds) wins over ``delay_secs`` — used
    when republishing a not-yet-eligible message with its original deadline.
    """
    deadline = not_before if not_before is not None else time.time() + delay_secs
    return [
        (_HEADER_ATTEMPTS, str(attempts).encode()),
        (_HEADER_NOT_BEFORE, repr(deadline).encode()),
    ]


def get_attempts(msg: Any) -> int:
    """Read the ``attempts`` header from a ConsumerRecord (0 when absent)."""
    return int(_header_value(msg, _HEADER_ATTEMPTS, b"0"))


def get_not_before(msg: Any) -> float:
    """Read the ``not_before`` header from a ConsumerRecord (0.0 when absent)."""
    return float(_header_value(msg, _HEADER_NOT_BEFORE, b"0"))


def _header_value(msg: Any, name: str, default: bytes) -> bytes:
    for key, value in msg.headers or ():
        if key == name and value is not None:
            return value
    return default


# ── Serialization ──────────────────────────────────────────────────────────────
def _json_deserializer(raw: bytes | None) -> Any:
    """
    Tolerant JSON value deserializer for consumers.

    Malformed payloads decode to ``None`` instead of raising — a raising
    deserializer would kill the fetch task, not the message.  Callers treat
    ``None`` as poison: log, commit, skip.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        logger.warning("Dropping malformed Kafka payload (%d bytes)", len(raw))
        return None


# ── Singleton state ────────────────────────────────────────────────────────────
_producer: AIOKafkaProducer | None = None
_admin: AIOKafkaAdminClient | None = None
_offset_client: AIOKafkaConsumer | None = None
_offset_lock = asyncio.Lock()


# ── Lifecycle ──────────────────────────────────────────────────────────────────
async def init_kafka() -> None:
    """
    Ensure topics exist and start the process-wide producer.

    Idempotent: safe to call from both the API lifespan and the worker
    lifespan; a second call on an initialized process is a no-op.
    Mirrors ``init_redis()`` — raises on an unreachable broker so startup
    fails loudly rather than degrading silently.
    """
    global _producer, _admin, _offset_client
    if _producer is not None:
        return

    admin = AIOKafkaAdminClient(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        client_id="cortex-admin",
    )
    # start() BEFORE create_topics — the admin client negotiates the broker
    # API version on start; calling create_topics first raises
    # IncompatibleBrokerVersion.
    await admin.start()
    new_topics = [
        NewTopic(
            name=topic,
            num_partitions=partitions,
            replication_factor=1,
            topic_configs={
                "retention.ms": str(retention_ms),
                "cleanup.policy": "delete",
            },
        )
        for topic, (partitions, retention_ms) in _TOPIC_SPECS.items()
    ]
    try:
        await admin.create_topics(new_topics)
        logger.info("Created %d Kafka topics", len(new_topics))
    except TopicAlreadyExistsError:
        # Restart path — topology already provisioned (mirrors the
        # BUSYGROUP pattern the Redis streams used).
        logger.debug("Kafka topics already exist — skipping creation")
    # Kept open for the lifetime of the process: pending_count() reads
    # committed group offsets through it on every 30s depth tick.
    _admin = admin

    # Single long-lived idempotent producer per process.  enable_idempotence
    # forces acks=all internally — do NOT pass acks (a conflicting value
    # raises ValueError).
    producer = AIOKafkaProducer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        client_id="cortex-producer",
        enable_idempotence=True,
        linger_ms=5,
    )
    await producer.start()
    _producer = producer

    # Group-less metadata consumer used exclusively for offset arithmetic in
    # pending_count() — long-lived so 30s depth tickers don't churn TCP
    # connections.  Serialized behind _offset_lock (one consumer, many tasks).
    offset_client = AIOKafkaConsumer(
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        client_id="cortex-offsets",
        group_id=None,
        enable_auto_commit=False,
    )
    await offset_client.start()
    _offset_client = offset_client

    logger.info(
        "Kafka initialized [bootstrap=%s topics=%d]",
        settings.KAFKA_BOOTSTRAP_SERVERS, len(_TOPIC_SPECS),
    )


async def close_kafka() -> None:
    """Stop the producer, admin, and offset clients. Consumers must already be stopped."""
    global _producer, _admin, _offset_client
    if _offset_client is not None:
        await _offset_client.stop()
        _offset_client = None
    if _admin is not None:
        await _admin.close()
        _admin = None
    if _producer is not None:
        await _producer.stop()
        _producer = None
    logger.info("Kafka clients closed")


def get_kafka_producer() -> AIOKafkaProducer:
    """Return the process-wide producer singleton."""
    if _producer is None:
        raise RuntimeError("Kafka not initialized")
    return _producer


# ── Producing ──────────────────────────────────────────────────────────────────
async def publish(
    topic: str,
    value: dict[str, Any],
    *,
    key: str | None = None,
    headers: list[tuple[str, bytes]] | None = None,
) -> None:
    """
    Publish one JSON message and wait for broker acknowledgement.

    ``send_and_wait`` + idempotent producer means a normal return guarantees
    the record is durably appended — callers rely on this for their failure
    contracts (e.g. HTTP 500 on enqueue failure).
    """
    producer = get_kafka_producer()
    await producer.send_and_wait(
        topic,
        value=json.dumps(value, default=str).encode(),
        key=key.encode() if key is not None else None,
        headers=headers,
    )


# ── Consuming ──────────────────────────────────────────────────────────────────
def new_consumer(
    *topics: str,
    group_id: str,
    max_poll_records: int | None = None,
) -> AIOKafkaConsumer:
    """
    Build (but do not start) a manual-commit consumer.

    - ``enable_auto_commit=False``: offsets advance only on explicit commit
      after a terminal outcome — this IS the at-least-once guarantee.
    - ``auto_offset_reset="earliest"``: a brand-new group starts from the
      beginning of the topic; the default ``latest`` would silently skip any
      backlog present at first start.
    - Callers own start()/stop(); pass ``max_poll_records=1`` for
      one-message-at-a-time workers so long Gemini calls plus a bounded
      ``not_before`` sleep stay far below ``max_poll_interval_ms`` (300 s).
    """
    return AIOKafkaConsumer(
        *topics,
        bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
        group_id=group_id,
        client_id=f"cortex-{group_id}",
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=_json_deserializer,
        max_poll_records=max_poll_records,
    )


# ── Lag arithmetic ─────────────────────────────────────────────────────────────
async def pending_count(topic: str, group_id: str) -> int:
    """
    Number of messages the group has not yet committed on ``topic``.

    Per partition: ``end_offset − committed_offset``; a group that has never
    committed on a partition counts the full ``end − beginning`` backlog.
    Retried messages republished to the tail inflate this briefly during
    retry storms — arguably correct (retried = still pending); documented in
    the alert annotations.
    """
    if _offset_client is None or _admin is None:
        raise RuntimeError("Kafka not initialized")

    # Partition counts come from the canonical topology (we create every
    # topic in init_kafka).  Consumer metadata is NOT a reliable source here:
    # aiokafka's topics()/partitions_for_topic() only reflect topics the
    # client has registered interest in, so a group-less offset client can
    # report an empty partition set for a topic that exists.
    spec = _TOPIC_SPECS.get(topic)
    if spec is None:
        raise ValueError(f"Unknown topic {topic!r} — not in _TOPIC_SPECS")
    partitions = [TopicPartition(topic, p) for p in range(spec[0])]

    async with _offset_lock:
        end_offsets = await _offset_client.end_offsets(partitions)
        beginning_offsets = await _offset_client.beginning_offsets(partitions)
        committed = await _admin.list_consumer_group_offsets(
            group_id, partitions=partitions
        )

    total = 0
    for tp in partitions:
        end = end_offsets.get(tp, 0)
        meta = committed.get(tp)
        if meta is None or meta.offset < 0:
            total += end - beginning_offsets.get(tp, 0)
        else:
            total += end - meta.offset
    return max(0, total)
