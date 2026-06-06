"""
RAG Embedder
============
Batched, rate-aware text embedding via CortexIntelligenceClient.

All embedding calls flow through this module so that batching strategy and
rate-limit behaviour are configured in one place.  Callers pass plain strings;
this module handles chunking into API-sized batches and reassembling the result.

Embedding model:  nvidia/nv-embedqa-e5-v5 (1024-dim, finance-adapted, NIM)
Fallback:         Requires NIM — Ollama's nomic-embed-text is 768-dim and
                  incompatible with the VECTOR(1024) column.  If NIM is not
                  configured, embed_texts() raises LLMFallbackExhausted with
                  a clear message so the operator knows to add NVIDIA_NIM_API_KEY.
"""
from __future__ import annotations

import logging
from typing import Literal

from app.ai.intelligence.llm_client import LLMFallbackExhausted, get_intelligence_client
from app.core.config import get_settings

logger = logging.getLogger(__name__)

InputType = Literal["passage", "query"]

# Expected output dimension from nvidia/nv-embedqa-e5-v5.
# The database column is pinned to this value; mismatches are caught early.
EXPECTED_DIM = 1024


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
        LLMFallbackExhausted: If no embedding provider is available or NIM key
            is missing (NIM is required for 1024-dim vectors).
        RuntimeError: If the embedding provider returns vectors of the wrong
            dimension (signals a model or configuration mismatch).
    """
    if not texts:
        return []

    client = get_intelligence_client()

    if not client._nim_api_key:
        raise LLMFallbackExhausted(
            "Embeddings require NVIDIA NIM (1024-dim vectors). "
            "Add NVIDIA_NIM_API_KEY to backend/.env and restart. "
            "Ollama fallback is intentionally disabled for embeddings because "
            "nomic-embed-text produces 768-dim vectors incompatible with the "
            "VECTOR(1024) database column."
        )

    settings = get_settings()
    effective_batch = batch_size or settings.RAG_EMBED_BATCH_SIZE
    vectors: list[list[float]] = []

    for batch_start in range(0, len(texts), effective_batch):
        batch = texts[batch_start : batch_start + effective_batch]
        batch_vectors = await client.embed(batch, input_type=input_type)

        if batch_vectors and len(batch_vectors[0]) != EXPECTED_DIM:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {EXPECTED_DIM}, "
                f"got {len(batch_vectors[0])}. "
                f"Verify NVIDIA_NIM_EMBED_MODEL is set to nvidia/nv-embedqa-e5-v5."
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
    Returns a single 1024-dim float vector.
    """
    vectors = await embed_texts([query], input_type="query")
    return vectors[0]
