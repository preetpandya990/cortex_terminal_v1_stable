"""
RAG Pipeline
============
Public entry point for the Cortex RAG system.

This module is the single import surface that Week 3+ code (explanation_worker,
nlp_engine) should use.  It wraps the retriever and exposes two functions:

  retrieve()         — fetch top-k relevant chunks for a symbol + query.
  format_context()   — render chunks into an LLM-ready context block with
                       inline source citations for hallucination prevention.

The citation format ("According to [Source, Date]...") is injected here so that
the LLM prompt engineering layer never has to think about it.
"""
from __future__ import annotations

import logging
from datetime import timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.rag.retriever import RetrievedChunk, retrieve as _retrieve
from app.core.config import get_settings

logger = logging.getLogger(__name__)


async def retrieve(
    db: AsyncSession,
    query: str,
    symbol: str,
    window_hours: int | None = None,
    top_k: int | None = None,
    sector_peers: frozenset[str] = frozenset(),
) -> list[RetrievedChunk]:
    """
    Retrieve the most relevant news chunks for a trading signal query.

    Args:
        db:           SQLAlchemy async session.
        query:        Retrieval query string (e.g. "RELIANCE BUY signal earnings").
        symbol:       NSE trading symbol (e.g. "RELIANCE").
        window_hours: Override RAG_WINDOW_HOURS from settings.
        top_k:        Override RAG_TOP_K from settings.
        sector_peers: Other trading symbols sharing the queried instrument's
                      resolved sector (see app.ai.rag.sector_resolver) — feeds
                      the retriever's "sector" candidate tier. Empty by
                      default when the caller has no sector to resolve.

    Returns:
        Ranked list of RetrievedChunk (best first).
    """
    settings = get_settings()
    return await _retrieve(
        db=db,
        query=query,
        symbol=symbol,
        window_hours=window_hours if window_hours is not None else settings.RAG_WINDOW_HOURS,
        top_k=top_k if top_k is not None else settings.RAG_TOP_K,
        sector_peers=sector_peers,
    )


def format_context(chunks: list[RetrievedChunk]) -> str:
    """
    Render retrieved chunks into a structured LLM context block.

    Format:
        [Source: Economic Times Markets | 2026-06-03 14:30 UTC]
        RELIANCE hits 52-week high as quarterly earnings beat estimates by 12%...

        [Source: Business Standard | 2026-06-02 09:15 UTC]
        Reliance Industries Q4 results: Net profit rises 15% YoY...

    The [Source: ...] header enables the LLM to produce inline citations
    (e.g. "According to [Economic Times Markets, 2026-06-03]...") that are
    verifiable and auditable against ai_llm_audit_log.retrieved_source_ids.

    Returns an empty string if chunks is empty.
    """
    if not chunks:
        return ""

    parts: list[str] = []
    for chunk in chunks:
        ts = chunk.as_of_timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        formatted_ts = ts.strftime("%Y-%m-%d %H:%M UTC")
        header = f"[Source: {chunk.source_name} | {formatted_ts}]"
        parts.append(f"{header}\n{chunk.content.strip()}")

    return "\n\n".join(parts)


def build_retrieval_source_refs(chunks: list[RetrievedChunk]) -> list[dict]:
    """
    Build the list of source references for ai_llm_audit_log.retrieved_source_ids.

    Each element matches the audit log schema: {table, id, as_of}.
    """
    return [
        {
            "table":  "ai_raw_events",
            "id":     chunk.source_id,
            "as_of":  chunk.as_of_timestamp.isoformat(),
        }
        for chunk in chunks
    ]
