#!/usr/bin/env python3
"""
Cutover: Redis Queues → Kafka (Redpanda)
=========================================
One-time drain of the legacy Redis job queues into their Kafka topics,
run once at deploy time with the api and worker processes STOPPED:

  cortex:stream:explanation:jobs    → cortex.explanation.jobs
  cortex:stream:context:jobs        → cortex.context.jobs
  cortex:stream:explanation:dlq     → cortex.explanation.dlq   (re-shaped)
  cortex:forecast:batch:queue       → cortex.forecast.batch    (+ fresh enqueued_at)
  cortex:event:classifier:pending   → cortex.classifier.pending

What moves
----------
Streams: PEL entries across ALL consumers (in-flight at shutdown; migrated
with header attempts = times_delivered − 1) plus never-delivered entries
(after the group's last-delivered-id).  ACKed history is deliberately
skipped — it was already processed.  The DLQ moves in full (XRANGE - +),
re-shaped to the new JSON entry format.  Lists move in full, oldest first;
forecast items gain a fresh ``enqueued_at`` (they predate the field, and
stamping drain time prevents the consumer's staleness gate from discarding
them on first dispatch).

Safety:
  - Idempotent: the marker key cortex:migration:kafka_cutover:done aborts a
    re-run (exit 2).  Delete the marker to force a re-run.
  - Dry-run: --dry-run prints per-queue counts without publishing, renaming,
    or setting the marker.  (Kafka is still initialized — topic creation is
    idempotent and doubles as a broker connectivity check.)
  - Nothing is deleted: source streams are RENAMEd to
    cortex:migrated:backup:<name> after publishing; lists are RENAMEd first
    and published from the backup, so the backup always holds the originals.
    Delete cortex:migrated:backup:* manually after 48h of clean operation.
  - Redis dedup keys (cortex:gemini:dlq:requeued:*, forecast dedup) are left
    untouched — they remain correct across the cutover.

Usage:
  cd backend/
  source .venv/bin/activate
  python -m scripts.migrate_redis_queues_to_kafka --dry-run
  python -m scripts.migrate_redis_queues_to_kafka

Exit codes: 0 = success · 1 = failure · 2 = already migrated (marker present)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure backend/ is on sys.path when run from the scripts/ directory.
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

from app.core.kafka import (
    KafkaTopics,
    close_kafka,
    init_kafka,
    publish,
    retry_headers,
)
from app.core.redis import close_redis, get_redis, init_redis

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kafka_cutover")

_MARKER_KEY = "cortex:migration:kafka_cutover:done"
_BACKUP_PREFIX = "cortex:migrated:backup:"

# Legacy Redis keys (constants were removed from app.core.redis with the
# migration, so the script carries them verbatim).
_EXPLANATION_STREAM = "cortex:stream:explanation:jobs"
_CONTEXT_STREAM = "cortex:stream:context:jobs"
_DLQ_STREAM = "cortex:stream:explanation:dlq"
_LEGACY_GROUP = "cortex-explanation-workers"
_FORECAST_LIST = "cortex:forecast:batch:queue"
_CLASSIFIER_LIST = "cortex:event:classifier:pending"

_PAGE = 100


def _backup_key(key: str) -> str:
    return _BACKUP_PREFIX + key


async def _stream_pending_entries(redis, stream: str) -> list[tuple[str, int]]:
    """All PEL (message_id, times_delivered) pairs across ALL consumers."""
    entries: list[tuple[str, int]] = []
    last = "-"
    while True:
        try:
            page = await redis.xpending_range(
                name=stream, groupname=_LEGACY_GROUP, min=last, max="+", count=_PAGE
            )
        except Exception as exc:
            if "NOGROUP" in str(exc) or "no such key" in str(exc).lower():
                return []
            raise
        if not page:
            return entries
        for e in page:
            entries.append((e["message_id"], int(e["times_delivered"])))
        if len(page) < _PAGE:
            return entries
        last = f"({page[-1]['message_id']}"


async def _stream_undelivered_entries(redis, stream: str) -> list[tuple[str, dict]]:
    """Entries published after the group's last-delivered-id (never delivered)."""
    try:
        groups = await redis.xinfo_groups(stream)
    except Exception as exc:
        if "no such key" in str(exc).lower() or "NOGROUP" in str(exc):
            return []
        raise
    last_delivered = next(
        (g["last-delivered-id"] for g in groups if g["name"] == _LEGACY_GROUP),
        None,
    )
    if last_delivered is None:
        # No consumer group — the whole stream is undelivered.
        last_delivered = "0-0"

    entries: list[tuple[str, dict]] = []
    cursor = f"({last_delivered}"
    while True:
        page = await redis.xrange(stream, min=cursor, max="+", count=_PAGE)
        if not page:
            return entries
        entries.extend(page)
        if len(page) < _PAGE:
            return entries
        cursor = f"({page[-1][0]}"


async def _fetch_fields(redis, stream: str, msg_id: str) -> dict | None:
    rows = await redis.xrange(stream, min=msg_id, max=msg_id, count=1)
    return rows[0][1] if rows else None


async def _migrate_job_stream(
    redis, stream: str, topic: str, key_field: str, *, dry_run: bool
) -> int:
    """Republish a job stream's PEL + undelivered entries to its Kafka topic."""
    moved = 0

    for msg_id, times_delivered in await _stream_pending_entries(redis, stream):
        fields = await _fetch_fields(redis, stream, msg_id)
        if not fields:
            logger.warning("%s: PEL entry %s already trimmed — skipping", stream, msg_id)
            continue
        key = str(fields.get(key_field, "") or "") or None
        if not dry_run:
            await publish(
                topic, fields, key=key,
                headers=retry_headers(max(0, times_delivered - 1)),
            )
        moved += 1

    for _msg_id, fields in await _stream_undelivered_entries(redis, stream):
        if not fields:
            continue
        key = str(fields.get(key_field, "") or "") or None
        if not dry_run:
            await publish(topic, fields, key=key, headers=retry_headers(0))
        moved += 1

    logger.info("%s → %s: %d entr%s %s", stream, topic, moved,
                "y" if moved == 1 else "ies", "would move" if dry_run else "moved")
    return moved


async def _migrate_dlq(redis, *, dry_run: bool) -> int:
    """Move the full legacy DLQ, re-shaped to the new Kafka entry format."""
    moved = 0
    cursor = "-"
    while True:
        page = await redis.xrange(_DLQ_STREAM, min=cursor, max="+", count=_PAGE)
        if not page:
            break
        for msg_id, fields in page:
            try:
                original_fields = json.loads(fields.get("fields", "{}"))
            except (json.JSONDecodeError, TypeError):
                original_fields = {}
            entry = {
                "original_topic": KafkaTopics.EXPLANATION_JOBS,
                "reason":         fields.get("reason", "unknown"),
                "fields":         original_fields,
                "attempts":       0,
                "moved_at":       fields.get("moved_at", ""),
            }
            key = str(original_fields.get("suggestion_id", "") or "") or None
            if not dry_run:
                await publish(KafkaTopics.EXPLANATION_DLQ, entry, key=key)
            moved += 1
        if len(page) < _PAGE:
            break
        cursor = f"({page[-1][0]}"

    logger.info("%s → %s: %d entr%s %s", _DLQ_STREAM, KafkaTopics.EXPLANATION_DLQ,
                moved, "y" if moved == 1 else "ies",
                "would move" if dry_run else "moved")
    return moved


async def _migrate_list(
    redis, list_key: str, topic: str, key_field: str,
    *, stamp_enqueued_at: bool, dry_run: bool,
) -> int:
    """
    RENAME the list to its backup key, then publish its items oldest-first
    (producers LPUSH, so the oldest item is at the tail).  The backup keeps
    the originals; nothing is popped.
    """
    if not await redis.exists(list_key):
        logger.info("%s: empty/absent — nothing to move", list_key)
        return 0

    source = list_key
    if not dry_run:
        source = _backup_key(list_key)
        await redis.rename(list_key, source)

    raw_items = await redis.lrange(source, 0, -1)
    moved = 0
    for raw in reversed(raw_items):  # tail = oldest → publish in arrival order
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning("%s: skipping malformed item", list_key)
            continue
        if stamp_enqueued_at:
            # Legacy items predate the field; stamping drain time keeps them
            # inside the consumer's 1h staleness gate for the first dispatch.
            payload["enqueued_at"] = datetime.now(timezone.utc).isoformat()
        key = str(payload.get(key_field, "") or "") or None
        if not dry_run:
            await publish(topic, payload, key=key)
        moved += 1

    logger.info("%s → %s: %d item%s %s", list_key, topic, moved,
                "" if moved == 1 else "s", "would move" if dry_run else "moved")
    return moved


async def _rename_stream_to_backup(redis, stream: str, *, dry_run: bool) -> None:
    if dry_run or not await redis.exists(stream):
        return
    await redis.rename(stream, _backup_key(stream))
    logger.info("%s renamed to %s", stream, _backup_key(stream))


async def run(dry_run: bool) -> int:
    await init_redis()
    redis = get_redis()

    try:
        if await redis.exists(_MARKER_KEY):
            done_at = await redis.get(_MARKER_KEY)
            logger.warning(
                "Cutover already completed at %s (marker %s present) — aborting. "
                "Delete the marker to force a re-run.", done_at, _MARKER_KEY,
            )
            return 2

        await init_kafka()

        counts = {
            "explanation": await _migrate_job_stream(
                redis, _EXPLANATION_STREAM, KafkaTopics.EXPLANATION_JOBS,
                "suggestion_id", dry_run=dry_run,
            ),
            "context": await _migrate_job_stream(
                redis, _CONTEXT_STREAM, KafkaTopics.CONTEXT_JOBS,
                "instrument_key", dry_run=dry_run,
            ),
            "dlq": await _migrate_dlq(redis, dry_run=dry_run),
            "forecast": await _migrate_list(
                redis, _FORECAST_LIST, KafkaTopics.FORECAST_BATCH,
                "symbol", stamp_enqueued_at=True, dry_run=dry_run,
            ),
            "classifier": await _migrate_list(
                redis, _CLASSIFIER_LIST, KafkaTopics.CLASSIFIER_PENDING,
                "nlp_result_id", stamp_enqueued_at=False, dry_run=dry_run,
            ),
        }

        for stream in (_EXPLANATION_STREAM, _CONTEXT_STREAM, _DLQ_STREAM):
            await _rename_stream_to_backup(redis, stream, dry_run=dry_run)

        if not dry_run:
            await redis.set(_MARKER_KEY, datetime.now(timezone.utc).isoformat())

        mode = "DRY RUN — nothing was published or renamed" if dry_run else "CUTOVER COMPLETE"
        logger.info("%s | per-queue counts: %s | total=%d",
                    mode, counts, sum(counts.values()))
        if not dry_run:
            logger.info(
                "Backups live under %s* — delete manually after 48h of clean "
                "operation.", _BACKUP_PREFIX,
            )
        return 0

    finally:
        try:
            await close_kafka()
        except Exception:
            pass
        await close_redis()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Drain legacy Redis job queues into their Kafka (Redpanda) topics.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Count and report what would move without publishing or renaming.",
    )
    args = parser.parse_args()

    try:
        code = asyncio.run(run(dry_run=args.dry_run))
    except Exception as exc:
        logger.error("Cutover failed: %s", exc, exc_info=True)
        code = 1
    sys.exit(code)


if __name__ == "__main__":
    main()
