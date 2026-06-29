"""Add UNIQUE (content_hash) to ai_document_embeddings for cross-source dedup.

Revision ID: 0047
Revises: 0046
Create Date: 2026-06-20

Context
-------
The existing unique constraint ``uq_ai_doc_embeddings_source`` on
``(source_table, source_id)`` prevents re-embedding the same source row, but
does not prevent two different RSS feeds from producing duplicate embeddings for
articles with identical body text.  Research on multi-feed news corpora shows
~24% byte-level redundancy (arXiv:2605.09611), translating directly to wasted
Gemini embedding API quota and inflated vector storage.

This migration adds ``UNIQUE (content_hash)`` so the ingester can:
1. Skip fetching events whose body text is already indexed (SQL-side EXISTS
   filter in ``_fetch_unembedded_events``).
2. Atomically suppress any insert races via ``ON CONFLICT DO NOTHING`` covering
   both the per-source and per-hash constraints.

The B-tree index created by the unique constraint makes the correlated EXISTS
subquery in the fetch query O(log n) per candidate row — no full-table scan.

Steps (upgrade)
---------------
1. DELETE duplicate rows by content_hash, keeping the row with the smallest id
   (oldest ingest) for each hash value.  This is a no-op if the table is empty
   or has no duplicates (the expected state after migration 0046 truncated it).
2. ADD CONSTRAINT uq_ai_doc_embeddings_content_hash UNIQUE (content_hash).

Downgrade
---------
DROP the constraint.  No data is altered on downgrade — the constraint is
additive and its removal does not invalidate any existing rows.
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0047"
down_revision: Union[str, None] = "0046"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_CONSTRAINT = "uq_ai_doc_embeddings_content_hash"
_TABLE = "ai_document_embeddings"


def upgrade() -> None:
    # Remove any duplicate rows before enforcing uniqueness.
    # The subquery selects the lowest id (earliest ingested) for each
    # content_hash, so the DELETE removes all later duplicates.
    # This is safe to run on an empty table (0 rows deleted).
    op.execute(
        f"""
        DELETE FROM {_TABLE}
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM {_TABLE}
            GROUP BY content_hash
        )
        """
    )
    op.create_unique_constraint(_CONSTRAINT, _TABLE, ["content_hash"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
