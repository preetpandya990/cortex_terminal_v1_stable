"""Add instrument_key to strategy_activations.

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-06

The SL/TP monitoring worker needs the Upstox instrument_key to read LTP
from Redis (cai:ltp:{instrument_key}).  Storing it at activation creation
time avoids a repeated instrument_master JOIN every 500ms in the hot path.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_activations",
        sa.Column("instrument_key", sa.String(200), nullable=True),
    )
    op.create_index(
        "idx_strategy_activations_instrument_key",
        "strategy_activations",
        ["instrument_key"],
        postgresql_where=sa.text("instrument_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_strategy_activations_instrument_key",
        table_name="strategy_activations",
    )
    op.drop_column("strategy_activations", "instrument_key")
