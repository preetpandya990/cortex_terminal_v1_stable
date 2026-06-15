"""Re-dimension RAG embeddings for Gemini (gemini-embedding-001).

Revision ID: 0046
Revises: 0045
Create Date: 2026-06-10

Context
-------
The RAG vector store (``ai_document_embeddings``) was built for NVIDIA NIM's
``nvidia/nv-embedqa-e5-v5`` at 1024 dimensions.  The AI layer has migrated to
Google Gemini, so embeddings now come from ``gemini-embedding-001`` at
``GEMINI_EMBED_DIM`` dimensions (default 1536).

Why a full reset
----------------
Vectors from a different embedding model are NOT comparable to the existing ones
— a query embedded by Gemini cannot be cosine-matched against passages embedded
by NIM.  Re-embedding the entire corpus is mandatory, so this migration TRUNCATEs
the table; the operator must re-populate it afterwards via the RAG backfill
(``app/ai/rag/ingester.run_backfill`` — see scripts/reembed_rag_corpus.py).

Dimension choice
----------------
GEMINI_EMBED_DIM defaults to 1536.  pgvector's HNSW index supports at most 2000
dimensions, so 3072 (Gemini's native size) cannot be indexed — 1536 keeps full
ANN indexing while halving storage vs. 3072 at negligible quality cost.

Steps (upgrade)
---------------
1. Drop the HNSW index (its dimension is fixed at creation).
2. TRUNCATE ai_document_embeddings (incompatible vectors).
3. ALTER the embedding column to vector(GEMINI_EMBED_DIM).
4. Recreate the HNSW index (cosine, m=16, ef_construction=64).
"""
from __future__ import annotations

from typing import Union

from alembic import op

from app.core.config import get_settings

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_HNSW_MAX_DIM = 2000  # pgvector HNSW hard limit
_PREV_DIM = 1024      # nvidia/nv-embedqa-e5-v5


def _new_dim() -> int:
    dim = get_settings().GEMINI_EMBED_DIM
    if dim > _HNSW_MAX_DIM:
        raise RuntimeError(
            f"GEMINI_EMBED_DIM={dim} exceeds the pgvector HNSW limit of "
            f"{_HNSW_MAX_DIM}. Choose 768, 1536, or another value ≤ {_HNSW_MAX_DIM}."
        )
    return dim


def _redimension(dim: int) -> None:
    """Drop HNSW → truncate → alter dimension → recreate HNSW."""
    op.execute("DROP INDEX IF EXISTS idx_ai_doc_embeddings_hnsw")
    # Empty the table — vectors from the old model are not reusable.
    op.execute("TRUNCATE TABLE ai_document_embeddings")
    op.execute(
        f"ALTER TABLE ai_document_embeddings "
        f"ALTER COLUMN embedding TYPE vector({dim})"
    )
    op.execute(
        """
        CREATE INDEX idx_ai_doc_embeddings_hnsw
        ON ai_document_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def upgrade() -> None:
    _redimension(_new_dim())


def downgrade() -> None:
    # Reverts to the NIM dimension; the corpus must be re-embedded with NIM.
    _redimension(_PREV_DIM)
