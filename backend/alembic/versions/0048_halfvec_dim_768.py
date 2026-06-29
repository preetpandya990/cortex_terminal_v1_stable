"""Convert RAG embeddings to halfvec(768): 50% storage reduction + faster HNSW.

Revision ID: 0048
Revises: 0047
Create Date: 2026-06-20

Context
-------
Migration 0046 set up ``ai_document_embeddings`` with ``vector(1536)`` for
``gemini-embedding-001``.  This migration makes two combined changes:

1. **Dimension reduction 1536 → 768**
   ``gemini-embedding-001`` uses Matryoshka Representation Learning (MRL): the
   first N dimensions of its 3072-dim output are a valid, independently useful
   embedding.  Benchmarks (arXiv:2407.20243) show 768-dim retains ~97–99%
   retrieval quality on monolingual financial English corpora while halving
   the per-vector memory footprint vs. 1536-dim.  Gemini charges by input
   tokens, not output vector size, so there is no API cost change.

2. **FP32 → FP16 storage (halfvec)**
   pgvector 0.8+ supports the ``halfvec`` type, which stores each dimension as
   IEEE 754 FP16 rather than FP32.  For 768-dim:
     FP32 vector(768)   → 3,076 bytes/row
     FP16 halfvec(768)  →   1,538 bytes/row   (50% reduction)
   Benchmarks show < 0.02 NDCG@5 recall impact (Neon Blog).  Python-side cosine
   ranking in ``retriever.py`` is unaffected — pgvector returns ``list[float]``
   (FP16 values upcast to Python float) for both types.

Why combined
------------
Both changes require a TRUNCATE + full corpus re-embed.  Combining them into one
migration avoids two re-embed cycles (which would be expensive in Gemini quota
and operator time).  Post-migration, run the RAG backfill:

    cd backend
    .venv/bin/python -m scripts.reembed_rag_corpus

HNSW notes
----------
- halfvec HNSW max dimension: 4000 (vs. 2000 for ``vector``).  768 is well within
  both limits.
- HNSW operator class for halfvec cosine distance: ``halfvec_cosine_ops``
  (not ``vector_cosine_ops``).
- m=16, ef_construction=64 are the pgvector-recommended production defaults.

Steps (upgrade)
---------------
1. Drop the HNSW index (its operator class is dimension-and-type-fixed at creation).
2. TRUNCATE ai_document_embeddings (vector(1536) rows are incompatible with
   halfvec(768): both the element count and the binary encoding change).
3. ALTER COLUMN embedding TYPE halfvec(768).
4. Recreate the HNSW index with halfvec_cosine_ops.

Steps (downgrade)
-----------------
1. Drop the halfvec HNSW index.
2. TRUNCATE (halfvec(768) rows are incompatible with vector(1536)).
3. ALTER COLUMN embedding TYPE vector(1536).
4. Recreate the vector HNSW index.
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0048"
down_revision: Union[str, None] = "0047"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

# Pinned dimensions — do not read from settings so that the migration produces
# the same result regardless of what GEMINI_EMBED_DIM is set to at run time.
_NEW_DIM  = 768    # halfvec(768) — target
_PREV_DIM = 1536   # vector(1536) — previous (migration 0046)

_HNSW_INDEX   = "idx_ai_doc_embeddings_hnsw"
_TABLE        = "ai_document_embeddings"


def _upgrade_to_halfvec(dim: int) -> None:
    op.execute(f"DROP INDEX IF EXISTS {_HNSW_INDEX}")
    op.execute(f"TRUNCATE TABLE {_TABLE}")
    op.execute(
        f"ALTER TABLE {_TABLE} "
        f"ALTER COLUMN embedding TYPE halfvec({dim})"
    )
    op.execute(
        f"""
        CREATE INDEX {_HNSW_INDEX}
        ON {_TABLE}
        USING hnsw (embedding halfvec_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def _downgrade_to_vector(dim: int) -> None:
    op.execute(f"DROP INDEX IF EXISTS {_HNSW_INDEX}")
    op.execute(f"TRUNCATE TABLE {_TABLE}")
    op.execute(
        f"ALTER TABLE {_TABLE} "
        f"ALTER COLUMN embedding TYPE vector({dim})"
    )
    op.execute(
        f"""
        CREATE INDEX {_HNSW_INDEX}
        ON {_TABLE}
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def upgrade() -> None:
    _upgrade_to_halfvec(_NEW_DIM)


def downgrade() -> None:
    _downgrade_to_vector(_PREV_DIM)
