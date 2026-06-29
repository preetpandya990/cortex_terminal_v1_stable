"""
RAG Embedder
============
Batched, rate-aware text embedding via CortexIntelligenceClient.

All embedding calls flow through this module so that batching strategy and
rate-limit behaviour are configured in one place.  Callers pass plain strings;
this module handles chunking into API-sized batches and reassembling the result.

Embedding model:  gemini-embedding-001 (GEMINI_EMBED_DIM dims, L2-normalized).
                  The model and dimension are owned by CortexIntelligenceClient;
                  if GEMINI_API_KEY is unset, embed_texts() raises
                  LLMFallbackExhausted with a clear message.

Query embedding cache:
                  embed_query() results are cached in Redis for
                  _QUERY_EMBED_CACHE_TTL_SECS (15 min).  Callers in
                  explanation_worker.py construct queries deterministically
                  (symbol + direction, symbol + "market analysis news"), so
                  cache hit rates for active symbols approach 100%.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Literal

from app.ai.intelligence.llm_client import Priority, get_intelligence_client
from app.core.config import get_settings
from app.core.redis import get_cache_service

logger = logging.getLogger(__name__)

InputType = Literal["passage", "query"]

# Redis cache for query embeddings.  Query strings from explanation_worker.py
# are constructed deterministically, so the same query fires many times per day
# for any actively traded symbol.  15-min TTL balances freshness against quota.
_QUERY_EMBED_CACHE_KEY_PREFIX = "rag:qembed"
_QUERY_EMBED_CACHE_TTL_SECS = 900


def expected_dim() -> int:
    """The embedding dimension the ai_document_embeddings column is pinned to.

    Owned by GEMINI_EMBED_DIM so the pgvector column, the migration, and the
    runtime guard below can never drift apart.
    """
    return get_settings().GEMINI_EMBED_DIM


async def embed_texts(
    texts: list[str],
    input_type: InputType = "passage",
    batch_size: int | None = None,
) -> list[list[float]]:
    """
    Embed a list of text strings in batches and return one float vector per input.

    Args:
        texts:      Text strings to embed.  Empty strings are accepted and return
                    a zero-vector of length EXPECTED_DIM.
        input_type: "passage" for documents being indexed (default);
                    "query" for retrieval-time query strings.
        batch_size: Override the default RAG_EMBED_BATCH_SIZE from settings.

    Returns:
        List of float vectors in the same order as the input texts.

    Raises:
        LLMFallbackExhausted: If Gemini is not configured (GEMINI_API_KEY unset)
            or the embedding call fails.
        RuntimeError: If the provider returns vectors of the wrong dimension
            (signals a model or configuration mismatch vs. GEMINI_EMBED_DIM).
    """
    if not texts:
        return []

    client = get_intelligence_client()
    settings = get_settings()
    dim = settings.GEMINI_EMBED_DIM
    effective_batch = batch_size or settings.RAG_EMBED_BATCH_SIZE
    vectors: list[list[float]] = []

    for batch_start in range(0, len(texts), effective_batch):
        batch = texts[batch_start : batch_start + effective_batch]
        batch_vectors = await client.embed(batch, input_type=input_type, priority=Priority.BACKGROUND)

        if batch_vectors and len(batch_vectors[0]) != dim:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {dim}, "
                f"got {len(batch_vectors[0])}. "
                f"Verify GEMINI_EMBED_DIM matches the ai_document_embeddings column."
            )

        vectors.extend(batch_vectors)
        logger.debug(
            "rag.embed batch=%d/%d input_type=%s",
            batch_start // effective_batch + 1,
            -(-len(texts) // effective_batch),  # ceiling division
            input_type,
        )

    return vectors


async def embed_texts_batch(
    texts: list[str],
    input_type: InputType = "passage",
    *,
    poll_interval_secs: float | None = None,
    timeout_secs: float | None = None,
) -> list[list[float]]:
    """
    Batch-embed texts via the Gemini Batch API (paid tier only).

    Submits all texts as a single async Gemini batch job and polls for
    completion.  Returns one L2-normalized float vector per input in the same
    order as ``texts``.

    This is 50% cheaper than ``embed_texts()`` ($0.075 vs $0.15 per 1M tokens)
    but results are deferred by 30–90 minutes (up to 24 h per SLO).  Use only
    for bulk offline corpus ingestion — never for latency-sensitive paths.

    ``poll_interval_secs`` and ``timeout_secs`` default to
    ``RAG_BATCH_POLL_INTERVAL_SECS`` and ``RAG_BATCH_JOB_TIMEOUT_SECS``.

    Raises:
        LLMFallbackExhausted:  Gemini not configured or batch job failed.
        RuntimeError:          Response count mismatch or partial-text failures.
        asyncio.TimeoutError:  Job did not complete within ``timeout_secs``.
    """
    if not texts:
        return []

    client = get_intelligence_client()
    settings = get_settings()
    dim = settings.GEMINI_EMBED_DIM

    effective_poll = poll_interval_secs or float(settings.RAG_BATCH_POLL_INTERVAL_SECS)
    effective_timeout = timeout_secs or float(settings.RAG_BATCH_JOB_TIMEOUT_SECS)

    vectors = await client.embed_batch_job(
        texts,
        input_type=input_type,
        poll_interval_secs=effective_poll,
        timeout_secs=effective_timeout,
    )

    if vectors and len(vectors[0]) != dim:
        raise RuntimeError(
            f"Batch embedding dimension mismatch: expected {dim}, "
            f"got {len(vectors[0])}. Verify GEMINI_EMBED_DIM matches the "
            f"ai_document_embeddings column."
        )

    return vectors


async def embed_query(query: str) -> list[float]:
    """
    Embed a single query string for retrieval-time use.

    Results are cached in Redis for _QUERY_EMBED_CACHE_TTL_SECS seconds.
    Cache hits skip the Gemini API call entirely.  Redis errors fall through
    transparently to a live API call — cache is never on the critical path.

    Returns a single GEMINI_EMBED_DIM float vector.
    """
    query_hash = hashlib.sha256(query.encode()).hexdigest()
    cache_key = f"{_QUERY_EMBED_CACHE_KEY_PREFIX}:{query_hash}"
    dim = expected_dim()

    try:
        cached = await get_cache_service().get(cache_key)
        if cached is not None and isinstance(cached, list) and len(cached) == dim:
            logger.debug("rag: qembed cache hit hash=%.12s", query_hash)
            return cached
    except Exception as exc:
        logger.debug("rag: qembed cache read failed (non-fatal): %s", exc)

    vectors = await embed_texts([query], input_type="query")
    vector = vectors[0]

    try:
        await get_cache_service().set(cache_key, vector, ttl=_QUERY_EMBED_CACHE_TTL_SECS)
        logger.debug(
            "rag: qembed cached hash=%.12s ttl=%ds",
            query_hash,
            _QUERY_EMBED_CACHE_TTL_SECS,
        )
    except Exception as exc:
        logger.debug("rag: qembed cache write failed (non-fatal): %s", exc)

    return vector
