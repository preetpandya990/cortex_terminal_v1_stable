"""Intelligence Layer — LLM explanation fields on trade_suggestions.

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-03

Background
----------
The Cortex Intelligence Layer (CORTEX_LLM_UPGRADE_PLAN.md §6.2) generates a
plain-English explanation for every trade suggestion asynchronously via the LLM
explanation worker.  This migration adds four columns to ``trade_suggestions``
to persist that output:

  llm_summary
      2–3 sentence plain-English summary suitable for inline display on the
      TradeSuggestionCard.  Populated by the explanation worker; NULL while the
      worker is in progress or if generation failed.  The frontend renders a
      loading skeleton when this is NULL.

  llm_explanation
      Full narrative explanation including signal rationale, news context, and
      risk commentary.  Displayed in the AI Analysis Cards panel.  Will be longer
      than llm_summary; TEXT (unbounded) is appropriate.

  explanation_model
      The model identifier that generated the explanation (e.g.
      ``nim/qwen3.6-35b-a3b`` or ``ollama/llama3.1:8b``).  Required for audit
      purposes and model-version tracking per CORTEX_LLM_UPGRADE_PLAN.md §8.3.

  explanation_generated_at
      Wall-clock timestamp (UTC) when the explanation was written.  Used by the
      frontend to show staleness and by the audit log join.

Index
-----
A partial B-tree index on ``created_at DESC`` filtered to rows where
``llm_summary IS NULL AND status = 'active'`` lets the explanation worker
efficiently poll for suggestions that still need processing without a full
table scan as the suggestion backlog grows.

Downgrade
---------
Drops the partial index then the four columns.  No data migration required —
the columns are additive and nullable.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add explanation columns to trade_suggestions ───────────────────────
    # All four columns are nullable — existing suggestions have no explanation
    # and new suggestions receive NULL until the async worker completes.
    op.add_column(
        "trade_suggestions",
        sa.Column("llm_summary", sa.Text(), nullable=True),
    )
    op.add_column(
        "trade_suggestions",
        sa.Column("llm_explanation", sa.Text(), nullable=True),
    )
    op.add_column(
        "trade_suggestions",
        sa.Column(
            "explanation_model",
            sa.String(100),
            nullable=True,
            comment="Model identifier that generated the explanation (e.g. nim/qwen3.6-35b-a3b)",
        ),
    )
    op.add_column(
        "trade_suggestions",
        sa.Column(
            "explanation_generated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    # ── 2. Partial index for the explanation worker's pending-queue query ──────
    # Targets: active suggestions without an explanation, ordered newest-first.
    # The partial condition keeps the index small — it automatically shrinks as
    # the worker processes the backlog.
    op.execute(
        """
        CREATE INDEX idx_trade_suggestions_explanation_pending
        ON trade_suggestions (created_at DESC)
        WHERE llm_summary IS NULL
          AND status = 'active'
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS idx_trade_suggestions_explanation_pending"
    )
    op.drop_column("trade_suggestions", "explanation_generated_at")
    op.drop_column("trade_suggestions", "explanation_model")
    op.drop_column("trade_suggestions", "llm_explanation")
    op.drop_column("trade_suggestions", "llm_summary")
