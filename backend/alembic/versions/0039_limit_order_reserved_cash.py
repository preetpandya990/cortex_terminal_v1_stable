"""Add reserved_cash to portfolios and pending-order match indexes

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-20

Schema changes required by the limit-order matching engine:

  1. portfolios — new column:

       reserved_cash NUMERIC(18, 2) NOT NULL DEFAULT 0
         Tracks cash pre-committed by open LIMIT/SL/SL-M BUY orders that have
         not yet filled.  Available cash = current_cash − reserved_cash.
         Released automatically when the order fills, is cancelled, or expires.

         DB-level CHECK (reserved_cash >= 0) guards against service bugs.

  2. paper_orders — two new composite indexes for the matching engine:

       ix_paper_orders_pending_match  (instrument_key, status, order_type)
         Primary dispatch index: given a market tick for instrument X, find all
         OPEN LIMIT/SL/SL-M orders for X in a single B-tree scan.  Used on
         every tick where the cache is stale.

       ix_paper_orders_status_type  (status, order_type)
         Startup / full-cache rebuild: load every OPEN LIMIT/SL/SL-M order
         across all instruments in one pass.  Also used by the DAY expiry sweep
         to locate all OPEN DAY orders past market-close time.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add reserved_cash to portfolios ───────────────────────────────────
    op.add_column(
        "portfolios",
        sa.Column(
            "reserved_cash",
            sa.Numeric(18, 2),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_portfolios_reserved_cash_non_negative",
        "portfolios",
        "reserved_cash >= 0",
    )

    # ── 2. Composite indexes on paper_orders for the matching engine ──────────
    op.create_index(
        "ix_paper_orders_pending_match",
        "paper_orders",
        ["instrument_key", "status", "order_type"],
    )
    op.create_index(
        "ix_paper_orders_status_type",
        "paper_orders",
        ["status", "order_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_paper_orders_status_type",    table_name="paper_orders")
    op.drop_index("ix_paper_orders_pending_match",  table_name="paper_orders")
    op.drop_constraint(
        "ck_portfolios_reserved_cash_non_negative", "portfolios"
    )
    op.drop_column("portfolios", "reserved_cash")
