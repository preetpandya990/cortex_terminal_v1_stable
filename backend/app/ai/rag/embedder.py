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
"""
from __future__ import annotations

import logging
from typing import Literal

from app.ai.intelligence.llm_client import Priority, get_intelligence_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

InputType = Literal["passage", "query"]


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


async def embed_query(query: str) -> list[float]:
    """
    Embed a single query string for retrieval-time use.

    Convenience wrapper around embed_texts() with input_type="query".
    Returns a single GEMINI_EMBED_DIM float vector.
    """
    vectors = await embed_texts([query], input_type="query")
    return vectors[0]
