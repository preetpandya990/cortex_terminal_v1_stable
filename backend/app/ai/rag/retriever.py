"""
RAG Retriever
=============
Hybrid BM25 + cosine-similarity retrieval with Reciprocal Rank Fusion (RRF),
gated by a tiered relevance floor before ranking ever sees the candidates.

Architecture
------------
At Cortex's current corpus size (9k–10k events, 25–130 candidates per symbol+
time-window query), loading the full filtered candidate set from the DB and
performing ranking in Python is faster and simpler than two separate DB queries:

  1. One SQL query loads candidates for (symbol, time_window) in three tiers
     (see _load_candidates): "exact" (symbol = :s), "sector" (symbol in the
     instrument's resolved sector peers), "generic" (symbol IS NULL) — most
     recent first, capped at _MAX_CANDIDATES.

  2. Relevance gate (see retrieve()): "exact"-tier candidates are trusted
     outright (ingestion-time symbol tagging already validated them).
     "sector" and "generic" candidates must independently clear
     RAG_MIN_GENERIC_COSINE_SIMILARITY against the query embedding or they
     are dropped from the candidate pool entirely, before either ranker
     below ever sees them. This is what stops topically unrelated filler
     (e.g. generic high-buzz market wire content with no real connection to
     the instrument) from reaching the LLM prompt as if it were relevant —
     see NEWS_CONTEXT_RELEVANCE_GAP_FINDING.md for the incident this fixed.

  3. BM25 (rank_bm25.BM25Okapi) ranks the gated candidates by lexical match.
     Tokenization: lowercase whitespace split.  Appropriate for short financial
     texts where exact term matching (tickers, regulatory terms) is critical.

  4. Cosine similarity (numpy matmul) ranks the gated candidates by semantic
     proximity. Query embedding: gemini-embedding-001 (768-dim) — see
     rag/embedder.py and GEMINI_EMBED_MODEL / GEMINI_EMBED_DIM in
     core/config.py.

  5. Reciprocal Rank Fusion (k=60, Cormack et al. 2009) merges the two ranked
     lists into a single score without requiring score calibration.

RRF formula:  score(d) = Σ_r  1 / (60 + rank_r(d))
  Documents appearing in only one list still receive their RRF contribution.

Performance (at 130 candidates):
  - SQL query:     ~8ms
  - Relevance gate: <1ms (one extra cosine-similarity pass, reused from step 4)
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
from app.ai.intelligence.credibility_scorer import load_credibility_scores
from app.ai.rag.embedder import embed_query
from app.core.config import get_settings
from app.core.metrics import rag_relevance_gate_total

logger = logging.getLogger(__name__)

# RRF k constant — Cormack et al. 2009 empirical optimum.
_RRF_K = 60

# Maximum candidates loaded from DB per retrieval call.
# Safety cap: prevents pathological queries from pulling the entire corpus.
_MAX_CANDIDATES = 500

# Source-credibility RRF term: ~half the weight of the BM25/cosine relevance
# terms — authority breaks ties between comparably relevant docs but must
# never outrank relevance. Applied only to docs already relevance-ranked.
_CREDIBILITY_RRF_WEIGHT = 0.5

# Unknown sources rank as if they carried the neutral 0-100 seed score.
_CREDIBILITY_DEFAULT_SCORE = 50.0

# Final-score multiplier for docs tagged low_confidence_source at ingest
# (bodies under RAG_MIN_CONTENT_CHARS): they stay retrievable — a one-line
# exchange filing can still be the best answer — but rank behind substantive
# articles of comparable relevance.
_LOW_CONFIDENCE_DEMOTION = 0.5

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
    low_confidence: bool = False
    # "exact" (doc tagged with the queried symbol at ingestion — trusted,
    # exempt from the relevance floor), "sector" (doc tagged with a
    # different symbol that shares the instrument's resolved sector), or
    # "generic" (doc has no symbol tag at all). Sector and generic tiers
    # both must clear RAG_MIN_GENERIC_COSINE_SIMILARITY — see retrieve().
    tier: str = "generic"


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


def _cosine_similarities(
    candidates: list[_Candidate],
    query_vector: list[float],
) -> np.ndarray:
    """
    Cosine similarity of every candidate embedding against the query vector.

    Uses numpy matrix multiplication for efficient batch computation over the
    full candidate set. Normalisation is applied to handle non-unit vectors.

    Single source of truth: both ranking (_cosine_rank) and the relevance
    gate (retrieve()) compute similarity through this function, so the two
    can never drift apart.
    """
    embeddings = np.array([c.embedding for c in candidates], dtype=np.float32)
    query = np.array(query_vector, dtype=np.float32)

    # L2 normalise both sides for cosine similarity.
    emb_norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    emb_norms = np.where(emb_norms == 0, 1.0, emb_norms)  # avoid zero-division
    norm_embeddings = embeddings / emb_norms

    q_norm = np.linalg.norm(query)
    norm_query = query / q_norm if q_norm > 0 else query

    return norm_embeddings @ norm_query


def _cosine_rank(
    candidates: list[_Candidate],
    query_vector: list[float],
    top_n: int,
) -> list[int]:
    """
    Return indices into ``candidates`` sorted by cosine similarity (descending).
    """
    similarities = _cosine_similarities(candidates, query_vector)
    ranked = np.argsort(-similarities)
    return ranked[:top_n].tolist()


def _credibility_ranks(
    candidates: list[_Candidate],
    scores_by_source: dict[str, float],
) -> dict[int, int]:
    """
    Dense credibility rank (1 = most credible) per candidate index.

    All docs from the same source share the same rank — with a handful of
    feeds, an ordinal ranking would arbitrarily order same-source docs and
    inject noise into the RRF sum. Unknown sources rank at the neutral
    default score.
    """
    score_by_idx = {
        i: scores_by_source.get(c.source_name, _CREDIBILITY_DEFAULT_SCORE)
        for i, c in enumerate(candidates)
    }
    rank_of_score = {
        score: rank
        for rank, score in enumerate(sorted(set(score_by_idx.values()), reverse=True), start=1)
    }
    return {i: rank_of_score[s] for i, s in score_by_idx.items()}


def _rrf_merge(
    vector_ranks: list[int],
    bm25_ranks: list[int],
    candidates: list[_Candidate],
    top_k: int,
    credibility_ranks: dict[int, int] | None = None,
) -> list[RetrievedChunk]:
    """
    Reciprocal Rank Fusion of the ranked lists into a final result list.

    Documents that appear in only one relevance list still receive their RRF
    contribution (i.e. there is no penalty for single-list membership).

    The credibility term (weight _CREDIBILITY_RRF_WEIGHT) is added only to
    documents already ranked by BM25 or cosine — authority refines relevance
    ordering; it must never pull an irrelevant document into the results.
    Low-confidence-tagged docs have their final score demoted last, so the
    penalty applies uniformly across all three terms.
    """
    scores: dict[int, float] = {}

    for rank, idx in enumerate(vector_ranks, start=1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (_RRF_K + rank)

    for rank, idx in enumerate(bm25_ranks, start=1):
        scores[idx] = scores.get(idx, 0.0) + 1.0 / (_RRF_K + rank)

    if credibility_ranks:
        for idx in scores:
            rank = credibility_ranks.get(idx)
            if rank is not None:
                scores[idx] += _CREDIBILITY_RRF_WEIGHT / (_RRF_K + rank)

    for idx in scores:
        if candidates[idx].low_confidence:
            scores[idx] *= _LOW_CONFIDENCE_DEMOTION

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
    sector_peers: frozenset[str] = frozenset(),
    limit: int = _MAX_CANDIDATES,
) -> list[_Candidate]:
    """
    Load embedding records + raw content for a given symbol and time window.

    Strategy — three-tier prioritized inclusion:
      1. "exact" — docs tagged with the queried symbol (symbol = :symbol) in
         the window, most-recent first, capped at ``limit``. The cap keeps a
         heavily covered symbol during earnings/M&A news flow (or a 168h
         swing window) from loading thousands of rows into Python and
         blowing the ~10ms/130-candidate performance model — hitting it
         truncates the OLDEST docs in the window and logs a warning.
         Trusted at retrieval time (see retrieve()): ingestion-time symbol
         tagging (event_classifier.normalize_and_validate_symbols) already
         validated the doc-to-symbol relationship.
      2. "sector" — docs tagged with a *different* symbol that shares the
         queried instrument's resolved sector (``sector_peers``, resolved by
         app.ai.rag.sector_resolver). Fills the budget remaining after step
         1. This tier did not exist before this fix: genuinely relevant
         company-specific news about a sector peer (tagged with its own real
         symbol, never NULL) was previously invisible to retrieval entirely.
         Not trusted like "exact" — sector adjacency is a candidate-
         generation heuristic, not a relevance guarantee, so these still
         have to clear the relevance floor in retrieve().
      3. "generic" — docs with no symbol tag at all (symbol IS NULL), filling
         whatever budget remains. Must also clear the relevance floor.

    Without prioritized inclusion, the large general news pool (symbol IS
    NULL, currently ~3.9k docs) would displace all symbol-specific docs from
    the 500-candidate cap, causing the retriever to return unrelated market
    news for any symbol query. ``sector_peers`` defaults to an empty set, so
    callers that cannot resolve a sector get byte-identical exact→generic
    behavior to before this fix.
    """
    _COLUMNS = (
        AIDocumentEmbedding.source_id,
        AIDocumentEmbedding.symbol,
        AIDocumentEmbedding.embedding,
        AIDocumentEmbedding.as_of_timestamp,
        AIRawEvent.raw_content,
        AIRawEvent.source_name,
        AIRawEvent.source_url,
        AIRawEvent.extra_data,
    )

    def _to_candidates(rows: list, tier: str) -> list[_Candidate]:
        return [
            _Candidate(
                source_id=row.source_id,
                content=row.raw_content,
                source_name=row.source_name,
                source_url=row.source_url,
                as_of_timestamp=row.as_of_timestamp,
                embedding=row.embedding,
                symbol=row.symbol,
                low_confidence=bool(
                    (row.extra_data or {}).get("low_confidence_source")
                ),
                tier=tier,
            )
            for row in rows
        ]

    # Step 1: exact-symbol docs in the time window, recency-capped.
    exact_stmt = (
        select(*_COLUMNS)
        .join(AIRawEvent, AIRawEvent.id == AIDocumentEmbedding.source_id)
        .where(
            AIDocumentEmbedding.symbol == symbol,
            AIDocumentEmbedding.as_of_timestamp >= cutoff,
        )
        .order_by(AIDocumentEmbedding.as_of_timestamp.desc())
        .limit(limit)
    )
    exact_rows = (await db.execute(exact_stmt)).all()
    if len(exact_rows) >= limit:
        logger.warning(
            "rag.retrieve: exact-symbol candidates for %s hit the %d cap — "
            "oldest docs in the window were truncated",
            symbol,
            limit,
        )

    # Step 2: sector-peer docs to fill remaining budget.
    sector_limit = max(0, limit - len(exact_rows))
    sector_rows: list = []
    if sector_limit > 0 and sector_peers:
        sector_stmt = (
            select(*_COLUMNS)
            .join(AIRawEvent, AIRawEvent.id == AIDocumentEmbedding.source_id)
            .where(
                AIDocumentEmbedding.symbol.in_(sector_peers),
                AIDocumentEmbedding.as_of_timestamp >= cutoff,
            )
            .order_by(AIDocumentEmbedding.as_of_timestamp.desc())
            .limit(sector_limit)
        )
        sector_rows = (await db.execute(sector_stmt)).all()

    # Step 3: general market docs to fill whatever budget remains.
    general_limit = max(0, limit - len(exact_rows) - len(sector_rows))
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

    return (
        _to_candidates(exact_rows, "exact")
        + _to_candidates(sector_rows, "sector")
        + _to_candidates(general_rows, "generic")
    )


def _apply_relevance_gate(
    candidates: list[_Candidate],
    query_vector: list[float],
    min_similarity: float,
) -> tuple[list[_Candidate], dict[str, int], dict[str, int]]:
    """
    Split ``candidates`` into admitted vs. filtered by tier trust rules.

    "exact"-tier candidates are exempt (ingestion-time symbol tagging already
    validated relevance). "sector"/"generic" candidates must independently
    clear ``min_similarity`` against ``query_vector`` or they're dropped from
    the pool entirely — before either ranker ever sees them, so a
    filtered-out candidate cannot leak back in via a nonzero BM25 score alone.

    Returns (admitted_candidates, admitted_count_by_tier, filtered_count_by_tier).
    """
    trusted = [c for c in candidates if c.tier == "exact"]
    gated = [c for c in candidates if c.tier != "exact"]

    admitted: list[_Candidate] = []
    admitted_by_tier: dict[str, int] = {"exact": len(trusted)}
    filtered_by_tier: dict[str, int] = {}

    if gated:
        gated_sims = _cosine_similarities(gated, query_vector)
        for c, s in zip(gated, gated_sims):
            if s >= min_similarity:
                admitted.append(c)
                admitted_by_tier[c.tier] = admitted_by_tier.get(c.tier, 0) + 1
            else:
                filtered_by_tier[c.tier] = filtered_by_tier.get(c.tier, 0) + 1

    return trusted + admitted, admitted_by_tier, filtered_by_tier


async def retrieve(
    db: AsyncSession,
    query: str,
    symbol: str,
    window_hours: int = 24,
    top_k: int = 5,
    sector_peers: frozenset[str] = frozenset(),
) -> list[RetrievedChunk]:
    """
    Retrieve the top-k most relevant news chunks for a trading signal query.

    Combines BM25 lexical ranking (critical for exact ticker and regulatory term
    matching) with semantic cosine ranking (captures paraphrase and context) via
    Reciprocal Rank Fusion.

    A relevance gate runs on the loaded candidate pool before either ranker
    sees it (see the "exact"/"sector"/"generic" tiering in _load_candidates):
    only "exact"-tier candidates are trusted outright; "sector" and "generic"
    tier candidates must independently clear RAG_MIN_GENERIC_COSINE_SIMILARITY
    against the query embedding or they are dropped from the pool entirely —
    not merely down-ranked. This is what prevents topically unrelated filler
    (e.g. generic "AI wave" market wire content) from ever reaching the LLM
    prompt as if it were relevant instrument context.

    Args:
        db:           SQLAlchemy async session.
        query:        Natural-language retrieval query (e.g. "RELIANCE BUY signal
                      Q4 earnings momentum").
        symbol:       NSE trading symbol to scope retrieval (e.g. "RELIANCE").
        window_hours: Freshness window.  Default 24h.  Use up to 168h (7 days)
                      for swing signals with slower information decay.
        top_k:        Number of chunks to return.  Default 5 (RAG_TOP_K config).
        sector_peers: Other trading symbols sharing the queried instrument's
                      resolved sector (see app.ai.rag.sector_resolver). Empty
                      by default — callers that don't resolve a sector get
                      the exact→generic-only behavior that existed before
                      this parameter was introduced.

    Returns:
        List of RetrievedChunk ordered by descending RRF score.
        Empty list if no candidates exist, or none clear the relevance gate.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    # Load candidates and embed the query in parallel.
    import asyncio

    candidates_task = asyncio.create_task(
        _load_candidates(db, symbol, cutoff, sector_peers=sector_peers)
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

    # ── Relevance gate ────────────────────────────────────────────────────
    settings = get_settings()
    candidates, admitted_by_tier, filtered_by_tier = _apply_relevance_gate(
        candidates, query_vector, settings.RAG_MIN_GENERIC_COSINE_SIMILARITY
    )

    for tier_name in ("exact", "sector", "generic"):
        if admitted_by_tier.get(tier_name):
            rag_relevance_gate_total.labels(tier=tier_name, outcome="admitted").inc(
                admitted_by_tier[tier_name]
            )
        if filtered_by_tier.get(tier_name):
            rag_relevance_gate_total.labels(tier=tier_name, outcome="filtered").inc(
                filtered_by_tier[tier_name]
            )

    if not candidates:
        logger.info(
            "rag.retrieve: all candidates for symbol=%s filtered below "
            "RAG_MIN_GENERIC_COSINE_SIMILARITY (no exact-tier coverage) — "
            "returning no news context",
            symbol,
        )
        return []

    # Number of candidates to rank before RRF merge.
    # More candidates → better recall; fewer → lower latency.  3× top_k is the
    # standard trade-off for hybrid retrieval at this corpus scale.
    rank_n = min(len(candidates), max(top_k * 3, 20))

    bm25_ranks = _bm25_rank(candidates, query, top_n=rank_n)
    vector_ranks = _cosine_rank(candidates, query_vector, top_n=rank_n)

    # Third RRF term: source authority (live credibility scores, batch-loaded
    # through a 900s in-process cache). Never blocks retrieval — on lookup
    # failure ranking degrades to pure relevance.
    credibility_ranks: dict[int, int] | None = None
    try:
        source_scores = await load_credibility_scores(
            db, {c.source_name for c in candidates}
        )
        credibility_ranks = _credibility_ranks(candidates, source_scores)
    except Exception as exc:
        logger.warning("rag.retrieve: credibility lookup failed (non-fatal): %s", exc)

    chunks = _rrf_merge(
        vector_ranks, bm25_ranks, candidates, top_k=top_k,
        credibility_ranks=credibility_ranks,
    )

    logger.info(
        "rag.retrieve symbol=%s candidates=%d (exact=%d sector=%d generic=%d) "
        "gate_filtered=%d bm25_ranked=%d vector_ranked=%d returned=%d",
        symbol,
        len(candidates),
        admitted_by_tier.get("exact", 0),
        admitted_by_tier.get("sector", 0),
        admitted_by_tier.get("generic", 0),
        sum(filtered_by_tier.values()),
        len(bm25_ranks),
        len(vector_ranks),
        len(chunks),
    )
    return chunks
