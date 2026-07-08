"""
RAG Ingester
============
Idempotent pipeline: ai_raw_events → embed → ai_document_embeddings.

Design decisions
----------------
No chunking:
    Source events are RSS news items with 12–1,124 character bodies (median 273).
    Each raw event is treated as a single chunk and produces exactly one embedding row.

Symbol assignment:
    Derived from ai_event_classifications.affected_symbols via a LEFT JOIN chain.
    - Exactly 1 affected symbol → symbol = that symbol.
    - 0 or 2+ affected symbols → symbol = NULL (general market event, included
      in every symbol-scoped retrieval pass).
    - All affected symbols are always stored in metadata['affected_symbols'].

Idempotency:
    Two unique constraints guard the embeddings table:
    - uq_ai_doc_embeddings_source  (source_table, source_id): prevents re-embedding
      the same source row.
    - uq_ai_doc_embeddings_content_hash  (content_hash): prevents duplicate embeddings
      for articles with identical body text published by different RSS feeds.

    The fetch query applies both checks as early filters so the Gemini embedding API
    is not called for content that is already indexed.  The INSERT falls back to
    ON CONFLICT DO NOTHING to handle any races atomically.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, null, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.ai.fusion.models import (
    AIDocumentEmbedding,
    AIEventClassification,
    AINLPResult,
    AIProcessedEvent,
    AIRawEvent,
)
from app.ai.rag.embedder import embed_texts

logger = logging.getLogger(__name__)

# Source table name used in ai_document_embeddings.source_table for raw events.
_SOURCE_TABLE = "ai_raw_events"

# Maximum events fetched per DB query to avoid loading unbounded result sets.
_FETCH_LIMIT = 500

# Alias used in the content_hash EXISTS subquery inside _fetch_unembedded_events.
# A separate alias is required to avoid SQLAlchemy conflating it with the outer
# LEFT JOIN on AIDocumentEmbedding (which is also in scope for that query).
_EmbedHash = aliased(AIDocumentEmbedding, name="ade_hash")


def _dedup_events(events: list[Any]) -> list[Any]:
    """
    Deduplicate a list of raw event rows by content hash.

    Returns events in original order, keeping the first occurrence of each
    unique body text.  Used as Layer 1 dedup before the embedding API call.
    """
    seen: dict[str, Any] = {}
    for row in events:
        h = hashlib.sha256(row.raw_content.encode("utf-8")).hexdigest()
        if h not in seen:
            seen[h] = row
    removed = len(events) - len(seen)
    if removed:
        logger.debug("rag.ingest batch_dedup removed=%d intra-batch duplicate(s)", removed)
    return list(seen.values())


def _build_embed_rows(
    deduped: list[Any],
    vectors: list[list[float]],
) -> list[dict]:
    """
    Build ai_document_embeddings insert dicts from (event, vector) pairs.

    Uses column names (not ORM attribute names) so the result is compatible
    with Core-level ``pg_insert(AIDocumentEmbedding.__table__)``.

    ``strict=True``: a length mismatch between events and vectors means the
    embedding provider dropped an item — silently zipping would write another
    event's embedding to every row after the gap, and the anti-join in
    ``_fetch_unembedded_events`` would never re-select those rows for repair.
    Raising instead leaves the batch unembedded, so the next cycle retries it.
    """
    rows: list[dict] = []
    for row, vector in zip(deduped, vectors, strict=True):
        affected_symbols = _flatten_affected_symbols(row.all_affected_symbols)
        symbol = _assign_symbol(affected_symbols)
        content_hash = hashlib.sha256(row.raw_content.encode("utf-8")).hexdigest()
        rows.append(
            {
                "source_table":    _SOURCE_TABLE,
                "source_id":       row.id,
                "symbol":          symbol,
                "content_hash":    content_hash,
                "content_preview": row.raw_content[:200],
                "embedding":       vector,
                "as_of_timestamp": row.event_timestamp,
                # DB column is "metadata"; ORM attribute is "extra_data" to avoid
                # the SQLAlchemy reserved-name conflict.  Core insert uses column names.
                "metadata": {
                    "source_name":      row.source_name,
                    "source_url":       row.source_url,
                    "affected_symbols": affected_symbols,
                },
            }
        )
    return rows


async def _insert_embed_rows(db: AsyncSession, rows: list[dict]) -> int:
    """
    Insert embedding rows, ignoring conflicts on either unique constraint.

    ON CONFLICT DO NOTHING (no constraint specified) suppresses violations of
    ANY unique constraint on the table — covers both uq_ai_doc_embeddings_source
    and uq_ai_doc_embeddings_content_hash atomically.

    Returns the number of rows actually inserted (conflicts excluded).
    """
    if not rows:
        return 0
    # Use __table__ (Core Table) not the ORM class — see _build_embed_rows docstring.
    stmt = (
        pg_insert(AIDocumentEmbedding.__table__)
        .values(rows)
        .on_conflict_do_nothing()
    )
    result = await db.execute(stmt)
    await db.commit()
    inserted = result.rowcount if result.rowcount >= 0 else len(rows)
    logger.info(
        "rag.ingest inserted=%d skipped=%d (already embedded or duplicate content)",
        inserted,
        len(rows) - inserted,
    )
    return inserted


def _assign_symbol(affected_symbols: list[str] | None) -> str | None:
    """
    Return the embedding-store symbol for an event.

    Single-symbol events get a direct symbol assignment for precise retrieval.
    Multi-symbol (sector/market) events get NULL so they are included in every
    symbol-scoped retrieval pass rather than being siloed to one instrument.
    """
    if not affected_symbols:
        return None
    return affected_symbols[0] if len(affected_symbols) == 1 else None


async def _fetch_unembedded_events(
    db: AsyncSession,
    cutoff: datetime,
    limit: int = _FETCH_LIMIT,
) -> list[Any]:
    """
    Return raw events that are within the time window and not yet in
    ai_document_embeddings.

    The LEFT JOIN + IS NULL pattern is the standard "anti-join" for finding
    rows absent from a related table — reads the UNIQUE index on
    (source_table, source_id) efficiently.
    """
    stmt = (
        select(
            AIRawEvent.id,
            AIRawEvent.raw_content,
            AIRawEvent.source_name,
            AIRawEvent.source_url,
            AIRawEvent.event_timestamp,
            # Aggregate affected_symbols from the classification chain.
            # One raw event → one processed event → one NLP result → one classification.
            # json_agg instead of array_agg: asyncpg cannot serialize
            # array_agg(array_column) — it crashes on both empty arrays AND
            # null arrays.  json_agg returns a Python list after deserialization
            # and handles both nulls and empty arrays transparently.
            # CASE guard skips empty '{}' arrays before aggregating.
            func.json_agg(
                case(
                    (func.cardinality(AIEventClassification.affected_symbols) > 0,
                     AIEventClassification.affected_symbols),
                    else_=null(),
                )
            ).label("all_affected_symbols"),
        )
        .select_from(AIRawEvent)
        # Anti-join: keep only events without an existing embedding row.
        .outerjoin(
            AIDocumentEmbedding,
            and_(
                AIDocumentEmbedding.source_table == _SOURCE_TABLE,
                AIDocumentEmbedding.source_id == AIRawEvent.id,
            ),
        )
        # Classification chain (all optional — events may not yet be classified).
        .outerjoin(AIProcessedEvent, AIProcessedEvent.raw_event_id == AIRawEvent.id)
        .outerjoin(AINLPResult, AINLPResult.processed_event_id == AIProcessedEvent.id)
        .outerjoin(AIEventClassification, AIEventClassification.nlp_result_id == AINLPResult.id)
        .where(
            AIDocumentEmbedding.id.is_(None),  # not yet embedded by (source_table, source_id)
            # Cross-source content dedup: skip events whose body text is already
            # indexed (possibly from a different RSS feed).  Uses the B-tree index
            # on content_hash (uq_ai_doc_embeddings_content_hash) — O(log n) per row.
            ~(
                select(_EmbedHash.id)
                .where(
                    _EmbedHash.content_hash == func.encode(
                        func.sha256(func.convert_to(AIRawEvent.raw_content, "UTF8")),
                        "hex",
                    )
                )
                .correlate(AIRawEvent)
                .exists()
            ),
            AIRawEvent.event_timestamp >= cutoff,
        )
        .group_by(
            AIRawEvent.id,
            AIRawEvent.raw_content,
            AIRawEvent.source_name,
            AIRawEvent.source_url,
            AIRawEvent.event_timestamp,
        )
        .order_by(AIRawEvent.event_timestamp.desc())
        .limit(limit)
    )

    result = await db.execute(stmt)
    return result.all()


def _flatten_affected_symbols(all_affected_symbols: list | None) -> list[str]:
    """
    Flatten the array_agg result from _fetch_unembedded_events.

    array_agg of ARRAY columns returns a list of arrays (or None values).
    We flatten and deduplicate, preserving order of first occurrence.
    """
    if not all_affected_symbols:
        return []
    seen: dict[str, None] = {}
    for group in all_affected_symbols:
        if not group:
            continue
        for sym in group:
            if sym and sym not in seen:
                seen[sym] = None
    return list(seen.keys())


async def ingest_batch(
    db: AsyncSession,
    events: list[Any],
    batch_size: int = 32,
) -> int:
    """
    Embed a list of raw event rows and upsert them into ai_document_embeddings.

    Idempotent: rows already present in the embeddings table are silently
    skipped.  Two dedup layers protect against duplicate API calls:
    1. Python-side: events with identical content_hash are deduplicated before
       calling the Gemini embedding API (via ``_dedup_events``).
    2. DB-side: INSERT ... ON CONFLICT DO NOTHING suppresses any race between
       concurrent callers (covers both unique constraints atomically).

    Args:
        db:         SQLAlchemy async session.
        events:     Rows from ``_fetch_unembedded_events()``.
        batch_size: Embedding API texts-per-request.

    Returns:
        Number of rows inserted (conflicts excluded).
    """
    if not events:
        return 0
    deduped = _dedup_events(events)
    texts = [row.raw_content for row in deduped]
    vectors = await embed_texts(texts, input_type="passage", batch_size=batch_size)
    rows = _build_embed_rows(deduped, vectors)
    return await _insert_embed_rows(db, rows)


async def run_backfill(
    db: AsyncSession,
    window_days: int = 30,
    embed_batch_size: int = 32,
    fetch_limit: int = _FETCH_LIMIT,
) -> int:
    """
    Backfill embeddings for all unembedded events within the time window.

    Runs multiple fetch→embed→insert cycles until no unembedded events remain
    in the window.  Safe to run multiple times.

    Args:
        db:              SQLAlchemy async session.
        window_days:     How many days back to backfill.  Default: 30.
        embed_batch_size: Embedding API texts-per-request (Gemini).
        fetch_limit:     Max events loaded per DB fetch cycle.

    Returns:
        Total number of new rows inserted across all cycles.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    total_inserted = 0
    cycle = 0

    logger.info(
        "rag.backfill starting: window_days=%d cutoff=%s",
        window_days,
        cutoff.isoformat(),
    )

    while True:
        cycle += 1
        events = await _fetch_unembedded_events(db, cutoff, limit=fetch_limit)

        if not events:
            logger.info(
                "rag.backfill complete: cycles=%d total_inserted=%d",
                cycle - 1,
                total_inserted,
            )
            break

        logger.info(
            "rag.backfill cycle=%d fetched=%d events to embed",
            cycle,
            len(events),
        )
        inserted = await ingest_batch(db, events, batch_size=embed_batch_size)
        total_inserted += inserted

    return total_inserted
