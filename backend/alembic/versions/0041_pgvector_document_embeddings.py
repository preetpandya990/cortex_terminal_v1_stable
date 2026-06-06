"""Intelligence Layer — pgvector extension + ai_document_embeddings table.

Revision ID: 0041
Revises: 0040
Create Date: 2026-06-03

Background
----------
Phase 0 of the Cortex Intelligence Layer upgrade (CORTEX_LLM_UPGRADE_PLAN.md)
introduces a finance-aware RAG pipeline that retrieves symbol-scoped news and
event context at inference time.  Two prerequisites are delivered here:

  1. The ``vector`` PostgreSQL extension (pgvector 0.8.2, pre-installed in the
     timescale/timescaledb-ha:pg16 image) is enabled in the database.

  2. The ``ai_document_embeddings`` table stores 1024-dimensional embeddings
     (nvidia/nv-embedqa-e5-v5) for chunked content from ``ai_raw_events``.
     Each row carries its source reference, symbol, and an as-of timestamp so
     the retriever can enforce temporal freshness — a hard requirement in finance.

Index design
------------
  * HNSW index (m=16, ef_construction=64) on the embedding column with cosine
    distance operator (``vector_cosine_ops``).  HNSW gives sub-millisecond ANN
    search at recall > 95% for this embedding dimension and expected corpus size.
    Parameters are the pgvector-recommended production defaults for a 1024-dim
    embedding at this scale; revisit m and ef_construction if corpus exceeds 5M
    rows or recall targets change.

  * B-tree index on (symbol, as_of_timestamp DESC) for the common filtered-
    retrieval pattern: ``WHERE symbol = $1 AND as_of_timestamp >= $2``.

  * Unique constraint on (source_table, source_id) prevents duplicate embeddings
    for the same source row.

Downgrade
---------
Drops the table (cascades both indexes) then drops the extension.  If other
objects depend on the ``vector`` type at downgrade time the extension drop will
raise; that is the correct behaviour — do not use CASCADE on the DROP EXTENSION.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Enable the pgvector extension ─────────────────────────────────────
    # IF NOT EXISTS is safe on re-runs and on environments where the extension
    # was already created manually (e.g. the timescaledb-ha container creates it
    # automatically for some configurations).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 2. Create the document embedding store ────────────────────────────────
    op.create_table(
        "ai_document_embeddings",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        # Source provenance — which table and which row this embedding was built from.
        sa.Column("source_table",     sa.String(50),   nullable=False),
        sa.Column("source_id",        sa.BigInteger(), nullable=False),
        # Optional symbol scope.  NULL means the chunk is market-general (not
        # instrument-specific) and is included in every retrieval pass.
        sa.Column("symbol",           sa.String(20),   nullable=True),
        # SHA-256 of the chunk text — used by the ingester to skip unchanged content.
        sa.Column("content_hash",     sa.String(64),   nullable=False),
        # First 200 characters of the chunk for debugging / audit purposes.
        sa.Column("content_preview",  sa.Text(),       nullable=True),
        # 1024-dimensional embedding vector (nvidia/nv-embedqa-e5-v5).
        # Dimension is pinned here; changing the embedding model requires a
        # full re-ingestion and a new migration to change the dimension.
        sa.Column("embedding",        Vector(1024),    nullable=False),
        # The as-of timestamp of the source event — not the ingestion time.
        # The retriever filters on this to enforce freshness windows.
        sa.Column("as_of_timestamp",  sa.DateTime(timezone=True), nullable=False),
        # Arbitrary metadata: source_name, source_url, event_type, etc.
        sa.Column("metadata",         postgresql.JSONB(), nullable=True),
        sa.Column("created_at",       sa.DateTime(timezone=True),
                  nullable=False, server_default=sa.text("NOW()")),
        # Prevent duplicate embeddings for the same source row.
        sa.UniqueConstraint("source_table", "source_id",
                            name="uq_ai_doc_embeddings_source"),
    )

    # ── 3. HNSW index for approximate nearest-neighbour search ────────────────
    # Created via raw SQL — Alembic's op.create_index() does not support the
    # USING hnsw syntax or the WITH (m, ef_construction) storage parameters.
    # m=16, ef_construction=64 are the pgvector-recommended defaults for
    # balanced recall/speed at 1024 dimensions.
    op.execute(
        """
        CREATE INDEX idx_ai_doc_embeddings_hnsw
        ON ai_document_embeddings
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )

    # ── 4. B-tree index for filtered retrieval (symbol + time window) ─────────
    op.create_index(
        "idx_ai_doc_embeddings_symbol_time",
        "ai_document_embeddings",
        ["symbol", sa.text("as_of_timestamp DESC")],
    )


def downgrade() -> None:
    # Table drop cascades both indexes.
    op.drop_table("ai_document_embeddings")
    # Drop the extension only if no other objects depend on the vector type.
    # Intentionally not CASCADE — a failure here surfaces a real dependency.
    op.execute("DROP EXTENSION IF EXISTS vector")
