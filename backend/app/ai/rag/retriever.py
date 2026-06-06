"""
RAG Retriever
=============
Hybrid BM25 + cosine-similarity retrieval with Reciprocal Rank Fusion (RRF).

Architecture
------------
At Cortex's current corpus size (9k–10k events, 25–130 candidates per symbol+
time-window query), loading the full filtered candidate set from the DB and
performing ranking in Python is faster and simpler than two separate DB queries:

  1. One SQL query fetches all candidates for (symbol, time_window):
       SELECT embedding, raw_content, source_name, source_url, as_of_timestamp
       FROM ai_document_embeddings JOIN ai_raw_events
       WHERE (symbol = :s OR symbol IS NULL) AND as_of_timestamp >= :cutoff

  2. BM25 (rank_bm25.BM25Okapi) ranks candidates by lexical match.
     Tokenization: lowercase whitespace split.  Appropriate for short financial
     texts where exact term matching (tickers, regulatory terms) is critical.

  3. Cosine similarity (numpy matmul) ranks candidates by semantic proximity.
     Query embedding: nv-embedqa-e5-v5 in "query" mode (1024-dim).

  4. Reciprocal Rank Fusion (k=60, Cormack et al. 2009) merges the two ranked
     lists into a single score without requiring score calibration.

RRF formula:  score(d) = Σ_r  1 / (60 + rank_r(d))
  Documents appearing in only one list still receive their RRF contribution.

Performance (at 130 candidates):
  - SQL query:     ~8ms
  - BM25 ranking:  <1ms
  - Cosine (numpy): <1ms
  - RRF merge:      <1ms
  Total:           ~10ms — well within the 500ms p95 SLO.

Scale-out note:
  If the candidate set grows beyond 1,000 documents (e.g., window_hours=168 for
  a heavily traded symbol), switch the vector ranking step to DB-side pgvector
  ORDER BY cosine_distance() and retain BM25 over the smaller pgvector result
  set for the hybrid step.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from rank_bm25 import BM25Okapi
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDocumentEmbedding, AIRawEvent
from app.ai.rag.embedder import embed_query

logger = logging.getLogger(__name__)

# RRF k constant — Cormack et al. 2009 empirical optimum.
_RRF_K = 60

# Maximum candidates loaded from DB per retrieval call.
# Safety cap: prevents pathological queries from pulling the entire corpus.
_MAX_CANDIDATES = 500

# BM25 parameters tuned for short financial texts (avg 60–80 tokens).
# k1=1.5: moderate term-frequency saturation for short docs.
# b=0.75: standard length normalisation (short docs don't need aggressive normalisation).
_BM25_K1 = 1.5
_BM25_B = 0.75


@dataclass(frozen=True)
class RetrievedChunk:
    """
    A single document retrieved by the hybrid RAG pipeline.

    Attributes:
        content:          Full text of the source event.
        source_name:      Publication name (e.g. "Economic Times Markets").
        as_of_timestamp:  Event timestamp — the freshness anchor for citations.
        source_url:       Original article URL.
        rrf_score:        Reciprocal Rank Fusion score (higher = more relevant).
        symbol:           Trading symbol if the event is instrument-specific.
        source_id:        Primary key in ai_raw_events (for audit traceability).
    """

    content: str
    source_name: str
    as_of_timestamp: datetime
    source_url: str
    rrf_score: float
    symbol: str | None = None
    source_id: int = 0


@dataclass
class _Candidate:
    """Internal working representation during ranking."""

    source_id: int
    content: str
    source_name: str
    source_url: str
    as_of_timestamp: datetime
    embedding: list[float]
    symbol: str | None


def _tokenize(text: str) -> list[str]:
    """
    Minimal tokenizer for BM25 on financial text.

    Lowercasing is critical: "RELIANCE" and "reliance" must share the same IDF.
    Punctuation is retained (e.g. "50.5" is kept intact; splitting "Q4" on "4"
    would lose the financial abbreviation meaning).
    """
    return text.lower().split()


def _bm25_rank(
    candidates: list[_Candidate],
    query: str,
    top_n: int,
) -> list[int]:
    """
    Return indices into ``candidates`` sorted by BM25 score (descending).

    Returns at most top_n indices.  Candidates with score 0 are excluded so
    that semantically irrelevant documents don't pollute the RRF merge.
    """
    corpus = [_tokenize(c.content) for c in candidates]
    bm25 = BM25Okapi(corpus, k1=_BM25_K1, b=_BM25_B)
    scores: np.ndarray = bm25.get_scores(_tokenize(query))

    # Exclude zero-score documents — they contribute no BM25 signal.
    non_zero = np.where(scores > 0)[0]
    if len(non_zero) == 0:
        return []

    ranked = non_zero[np.argsort(-scores[non_zero])]
    return ranked[:top_n].tolist()


def _cosine_rank(
    candidates: list[_Candidate],
    query_vector: list[float],
    top_n: int,
) -> list[int]:
    """
    Return indices into ``candidates`` sorted by cosine similarity (descending).

    Uses numpy matrix multiplication for efficient batch computation over the
    full candidate set.  Normalisation is applied to handle non-unit vectors.
    """
    embeddings = np.array([c.embedding for c in candidates], dtype=np.float32)
    query = np.array(query_vector, dtype=np.float32)

    # L2 normalise both sides for cosine similarity.
    emb_norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    emb_norms = np.where(emb_norms == 0, 1.0, emb_norms)  # avoid zero-division
    norm_embeddings = embeddings / emb_norms

    q_norm = np.linalg.norm(query)
    norm_query = query / q_norm if q_norm > 0 else query

    similarities: np.ndarray = norm_embeddings @ norm_query
    ranked = np.argsort(-similarities)
    return ranked[:top_n].tolist()


def _rrf_merge(
    vector_ranks: list[int],
    bm25_ranks: list[int],
    candidates: list[_Candidate],
    top_k: int,
) -> list[RetrievedChunk]:
    """
    Reciprocal Rank Fusion of two ranked index lists into a final result list.

    Documents that appear in only one ranked list still receive their RRF
    contribution (i.e. there is no penalty for single-list membership).
    """
    scores: dict[int, float] = {}

    for rank, idx in enumerate(vector_ranks, start=1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (_RRF_K + rank)

    for rank, idx in enumerate(bm25_ranks, start=1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (_RRF_K + rank)

    top_indices = sorted(scores, key=lambda i: -scores[i])[:top_k]

    return [
        RetrievedChunk(
            content=candidates[i].content,
            source_name=candidates[i].source_name,
            as_of_timestamp=candidates[i].as_of_timestamp,
            source_url=candidates[i].source_url,
            rrf_score=round(scores[i], 6),
            symbol=candidates[i].symbol,
            source_id=candidates[i].source_id,
        )
        for i in top_indices
    ]


async def _load_candidates(
    db: AsyncSession,
    symbol: str,
    cutoff: datetime,
    limit: int = _MAX_CANDIDATES,
) -> list[_Candidate]:
    """
    Load embedding records + raw content for a given symbol and time window.

    Strategy — guaranteed symbol inclusion:
      1. Fetch ALL instrument-specific docs (symbol = :symbol) in the window.
         These are always included regardless of count to prevent crowd-out.
      2. Fill remaining budget (limit - symbol_count) with general market docs
         (symbol IS NULL), most-recent first.

    Without this two-step strategy a large general news pool (symbol IS NULL,
    currently ~3.9k docs) would displace all symbol-specific docs from the 500-
    candidate cap, causing the retriever to return unrelated market news for any
    symbol query.
    """
    _COLUMNS = (
        AIDocumentEmbedding.source_id,
        AIDocumentEmbedding.symbol,
        AIDocumentEmbedding.embedding,
        AIDocumentEmbedding.as_of_timestamp,
        AIRawEvent.raw_content,
        AIRawEvent.source_name,
        AIRawEvent.source_url,
    )

    # Step 1: all symbol-specific docs in the time window.
    symbol_stmt = (
        select(*_COLUMNS)
        .join(AIRawEvent, AIRawEvent.id == AIDocumentEmbedding.source_id)
        .where(
            AIDocumentEmbedding.symbol == symbol,
            AIDocumentEmbedding.as_of_timestamp >= cutoff,
        )
        .order_by(AIDocumentEmbedding.as_of_timestamp.desc())
    )
    symbol_rows = (await db.execute(symbol_stmt)).all()

    # Step 2: general market docs to fill remaining budget.
    general_limit = max(0, limit - len(symbol_rows))
    general_rows: list = []
    if general_limit > 0:
        general_stmt = (
            select(*_COLUMNS)
            .join(AIRawEvent, AIRawEvent.id == AIDocumentEmbedding.source_id)
            .where(
                AIDocumentEmbedding.symbol.is_(None),
                AIDocumentEmbedding.as_of_timestamp >= cutoff,
            )
            .order_by(AIDocumentEmbedding.as_of_timestamp.desc())
            .limit(general_limit)
        )
        general_rows = (await db.execute(general_stmt)).all()

    rows = symbol_rows + general_rows

    return [
        _Candidate(
            source_id=row.source_id,
            content=row.raw_content,
            source_name=row.source_name,
            source_url=row.source_url,
            as_of_timestamp=row.as_of_timestamp,
            embedding=row.embedding if isinstance(row.embedding, list) else list(row.embedding),
            symbol=row.symbol,
        )
        for row in rows
    ]


async def retrieve(
    db: AsyncSession,
    query: str,
    symbol: str,
    window_hours: int = 24,
    top_k: int = 5,
) -> list[RetrievedChunk]:
    """
    Retrieve the top-k most relevant news chunks for a trading signal query.

    Combines BM25 lexical ranking (critical for exact ticker and regulatory term
    matching) with semantic cosine ranking (captures paraphrase and context) via
    Reciprocal Rank Fusion.

    Args:
        db:           SQLAlchemy async session.
        query:        Natural-language retrieval query (e.g. "RELIANCE BUY signal
                      Q4 earnings momentum").
        symbol:       NSE trading symbol to scope retrieval (e.g. "RELIANCE").
        window_hours: Freshness window.  Default 24h.  Use up to 168h (7 days)
                      for swing signals with slower information decay.
        top_k:        Number of chunks to return.  Default 5 (RAG_TOP_K config).

    Returns:
        List of RetrievedChunk ordered by descending RRF score.
        Empty list if no embeddings exist for the given symbol/window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # Load candidates and embed the query in parallel.
    import asyncio

    candidates_task = asyncio.create_task(
        _load_candidates(db, symbol, cutoff)
    )
    query_vector_task = asyncio.create_task(embed_query(query))

    candidates, query_vector = await asyncio.gather(candidates_task, query_vector_task)

    if not candidates:
        logger.debug(
            "rag.retrieve: no candidates for symbol=%s window_hours=%d",
            symbol,
            window_hours,
        )
        return []

    # Number of candidates to rank before RRF merge.
    # More candidates → better recall; fewer → lower latency.  3× top_k is the
    # standard trade-off for hybrid retrieval at this corpus scale.
    rank_n = min(len(candidates), max(top_k * 3, 20))

    bm25_ranks = _bm25_rank(candidates, query, top_n=rank_n)
    vector_ranks = _cosine_rank(candidates, query_vector, top_n=rank_n)
    chunks = _rrf_merge(vector_ranks, bm25_ranks, candidates, top_k=top_k)

    logger.info(
        "rag.retrieve symbol=%s candidates=%d bm25_ranked=%d vector_ranked=%d returned=%d",
        symbol,
        len(candidates),
        len(bm25_ranks),
        len(vector_ranks),
        len(chunks),
    )
    return chunks
