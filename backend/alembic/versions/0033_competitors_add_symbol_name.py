"""Add trading_symbol and name columns to company_competitors

Revision ID: 0033
Revises: 0032
Create Date: 2026-05-17

The Upstox competitors endpoint returns no company name or trading symbol —
only instrument_key (containing the ISIN), a long company_profile text, and
sector/market-cap data. These two columns are populated at write time by
joining against instrument_master, making the competitor list readable.

Existing rows will have NULL for both columns until the next backfill or
on-demand fetch triggers a re-upsert of competitors.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_competitors",
        sa.Column("trading_symbol", sa.String(50), nullable=True),
    )
    op.add_column(
        "company_competitors",
        sa.Column("name", sa.String(200), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("company_competitors", "name")
    op.drop_column("company_competitors", "trading_symbol")
