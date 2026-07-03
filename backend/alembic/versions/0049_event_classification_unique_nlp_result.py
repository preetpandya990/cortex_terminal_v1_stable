"""Add UNIQUE (nlp_result_id) to ai_event_classifications for flush-path upserts.

Revision ID: 0049
Revises: 0048
Create Date: 2026-07-01

Context
-------
The demand-driven AI processing feature needs to UPDATE an existing
``ai_event_classifications`` row in place when a queued item is later
classified via Gemini (the inline heuristic result is persisted first,
then replaced once the deferred LLM call completes). That UPDATE looks
the row up by ``nlp_result_id``, which today is a plain indexed column
with no uniqueness guarantee — a second matching row would make the
lookup ambiguous.

In practice only one call site (``EventClassifier.classify()``) ever
writes a given ``nlp_result_id``, so this is expected to be a no-op on
clean data.

Steps (upgrade)
---------------
1. DELETE duplicate rows by nlp_result_id, keeping the most recently
   created row (``created_at DESC``, tie-break ``id DESC``) for each
   value. No-op if there are no duplicates.
2. ADD CONSTRAINT uq_ai_event_classifications_nlp_result_id UNIQUE (nlp_result_id).

Downgrade
---------
DROP the constraint. The dedup step is not reversible (documented,
matches the one-way-dedup precedent in migration 0047).
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None

_CONSTRAINT = "uq_ai_event_classifications_nlp_result_id"
_TABLE = "ai_event_classifications"


def upgrade() -> None:
    # Keep only the most recently created row per nlp_result_id.
    # DISTINCT ON (nlp_result_id) ... ORDER BY created_at DESC, id DESC
    # picks the winner deterministically even when created_at ties.
    op.execute(
        f"""
        DELETE FROM {_TABLE}
        WHERE id NOT IN (
            SELECT DISTINCT ON (nlp_result_id) id
            FROM {_TABLE}
            ORDER BY nlp_result_id, created_at DESC, id DESC
        )
        """
    )
    op.create_unique_constraint(_CONSTRAINT, _TABLE, ["nlp_result_id"])


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="unique")
