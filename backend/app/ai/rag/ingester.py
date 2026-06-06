"""
RAG Ingester
============
Idempotent pipeline: ai_raw_events → embed → ai_document_embeddings.

Design decisions
----------------
No chunking:
    Source events are RSS news items with 12–1,124 character bodies (median 273).
    All are comfortably below the 512-token limit of nv-embedqa-e5-v5.  Each
    raw event is treated as a single chunk and produces exactly one embedding row.

Symbol assignment:
    Derived from ai_event_classifications.affected_symbols via a LEFT JOIN chain.
    - Exactly 1 affected symbol → symbol = that symbol.
    - 0 or 2+ affected symbols → symbol = NULL (general market event, included
      in every symbol-scoped retrieval pass).
    - All affected symbols are always stored in metadata['affected_symbols'].

Idempotency:
    The ai_document_embeddings table has a UNIQUE constraint on (source_table,
    source_id).  The ingester uses INSERT ... ON CONFLICT DO NOTHING, so running
    a backfill multiple times is safe with zero side effects.

NIM requirement:
    Embeddings must be 1024-dim to match the VECTOR(1024) column.  Only NIM's
    nv-embedqa-e5-v5 produces the correct dimension.  The ingester fails fast
    if NVIDIA_NIM_API_KEY is not configured.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, case, func, null, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

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
            AIDocumentEmbedding.id.is_(None),  # not yet embedded
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

    This function is idempotent: events already in the embeddings table are
    silently skipped via ON CONFLICT DO NOTHING.

    Args:
        db:         SQLAlchemy async session.
        events:     Rows from _fetch_unembedded_events().
        batch_size: Embedding API batch size (respects NIM's per-request limit).

    Returns:
        Number of rows inserted (conflicts excluded).
    """
    if not events:
        return 0

    texts = [row.raw_content for row in events]
    vectors = await embed_texts(texts, input_type="passage", batch_size=batch_size)

    rows_to_insert: list[dict] = []
    for row, vector in zip(events, vectors):
        affected_symbols = _flatten_affected_symbols(row.all_affected_symbols)
        symbol = _assign_symbol(affected_symbols)
        content_hash = hashlib.sha256(row.raw_content.encode("utf-8")).hexdigest()

        rows_to_insert.append(
            {
                "source_table":    _SOURCE_TABLE,
                "source_id":       row.id,
                "symbol":          symbol,
                "content_hash":    content_hash,
                "content_preview": row.raw_content[:200],
                "embedding":       vector,
                "as_of_timestamp": row.event_timestamp,
                # DB column is named "metadata"; ORM attr is "extra_data"
                # (reserved name conflict). The pg_insert() dict uses column names.
                "metadata": {
                    "source_name":      row.source_name,
                    "source_url":       row.source_url,
                    "affected_symbols": affected_symbols,
                },
            }
        )

    # Use __table__ (Core Table) not the ORM class — the DB column is named
    # "metadata" but the ORM attribute is "extra_data" to avoid the SQLAlchemy
    # reserved name conflict.  Core insert maps dict keys to column names
    # directly; the ORM layer would try to resolve "metadata" as an ORM
    # attribute name and fail with an AttributeError.
    stmt = (
        pg_insert(AIDocumentEmbedding.__table__)
        .values(rows_to_insert)
        .on_conflict_do_nothing(constraint="uq_ai_doc_embeddings_source")
    )
    result = await db.execute(stmt)
    await db.commit()

    inserted = result.rowcount if result.rowcount >= 0 else len(rows_to_insert)
    logger.info(
        "rag.ingest inserted=%d skipped=%d (already embedded)",
        inserted,
        len(rows_to_insert) - inserted,
    )
    return inserted


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
        embed_batch_size: Embedding API batch size per NIM request.
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
