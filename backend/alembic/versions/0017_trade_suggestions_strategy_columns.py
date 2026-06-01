"""Add strategy linkage columns to trade_suggestions.

Revision ID: 0017
Revises: 0016
Create Date: 2026-05-06

Adds three columns so that a TradeSuggestion can be traced back to the
user strategy (and activation) that matched it:

  strategy_id              — FK to strategies.id (the definition that matched)
  strategy_activation_id   — FK to strategy_activations.id (the live activation)
  strategy_match_result    — JSONB audit trail from StrategyFilterPipeline
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "trade_suggestions",
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "trade_suggestions",
        sa.Column(
            "strategy_activation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategy_activations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "trade_suggestions",
        sa.Column("strategy_match_result", pg.JSONB(astext_type=sa.Text()), nullable=True),
    )

    op.create_index(
        "idx_trade_suggestions_strategy_id",
        "trade_suggestions",
        ["strategy_id"],
        postgresql_where=sa.text("strategy_id IS NOT NULL"),
    )
    op.create_index(
        "idx_trade_suggestions_strategy_activation_id",
        "trade_suggestions",
        ["strategy_activation_id"],
        postgresql_where=sa.text("strategy_activation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_trade_suggestions_strategy_activation_id",
        table_name="trade_suggestions",
    )
    op.drop_index(
        "idx_trade_suggestions_strategy_id",
        table_name="trade_suggestions",
    )
    op.drop_column("trade_suggestions", "strategy_match_result")
    op.drop_column("trade_suggestions", "strategy_activation_id")
    op.drop_column("trade_suggestions", "strategy_id")
