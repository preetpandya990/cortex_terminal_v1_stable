"""Add suggested_action columns (learning-phase, SEBI RA-adjacent feature).

Revision ID: 0054
Revises: 0053
Create Date: 2026-07-12

Context
-------
The explanation pipeline has, until now, deliberately never stated an
actionable recommendation — every system prompt in explanation_worker.py
says "Describe; do NOT advise." This migration backs a new, explicitly
higher-risk feature: an additional "suggested action" generated alongside
the existing explanation, gated behind Settings.SUGGESTED_ACTION_ENABLED
(default False, MUST stay False until legal/compliance sign-off — see
core/config.py). Two nullable Text columns, one per explanation pathway:

  trade_suggestions.llm_suggested_action   — active trade-signal path,
                                              real entry/stop/target/direction.
  ai_instrument_context.suggested_action   — no-signal path, a conditional
                                              monitoring trigger instead
                                              (no real trade parameters exist
                                              for an instrument with no
                                              active signal).

Both nullable: existing rows have none, and new rows only populate this
column when the feature flag is on — matching the existing precedent for
llm_ml_assessment (migration 0053) and requires_risk_disclaimer's underlying
data.

Downgrade
---------
DROP both columns.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0054"
down_revision: Union[str, None] = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_suggestions",
        sa.Column("llm_suggested_action", sa.Text(), nullable=True),
    )
    op.add_column(
        "ai_instrument_context",
        sa.Column("suggested_action", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_instrument_context", "suggested_action")
    op.drop_column("trade_suggestions", "llm_suggested_action")
