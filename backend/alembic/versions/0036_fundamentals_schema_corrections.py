"""Fundamentals schema corrections: extra_ratios, expiry_date, composite index

Revision ID: 0036
Revises: 0035
Create Date: 2026-05-18

Schema additions to company_key_ratios and company_corporate_actions:

  company_key_ratios.extra_ratios (JSONB, nullable)
    Stores the complete raw ratio array returned by the Upstox key-ratios
    endpoint. The six ML-critical columns (pe, pb, roa, roe, roce, ev_ebitda)
    remain as explicit typed columns for query performance and ML joins.
    extra_ratios ensures no Upstox-provided ratio is silently discarded if
    Upstox adds new ratios in future API updates.

  company_corporate_actions.expiry_date (DATE, nullable)
    Parsed from expiry_date_str at write time using the Upstox DD-MMM-YYYY
    format (e.g. "15-Jun-2026"). Enables date-range queries, future/past
    action filtering, and point-in-time correct ML feature joins without
    string parsing at query time.

  idx_cca_instrument_expiry (instrument_key, expiry_date)
    Composite index supporting the primary access pattern: all corporate
    actions for a company ordered or filtered by date.

Fixes issues: A2 (partial), B2, B3 from the fundamentals remediation plan.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── UPGRADE ────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # 1. company_key_ratios — preserve full raw Upstox ratio list
    op.add_column(
        "company_key_ratios",
        sa.Column("extra_ratios", postgresql.JSONB(), nullable=True),
    )

    # 2. company_corporate_actions — queryable date column
    op.add_column(
        "company_corporate_actions",
        sa.Column("expiry_date", sa.Date(), nullable=True),
    )

    # 3. Composite index for instrument + date access pattern
    op.create_index(
        "idx_cca_instrument_expiry",
        "company_corporate_actions",
        ["instrument_key", "expiry_date"],
    )


# ── DOWNGRADE ──────────────────────────────────────────────────────────────────

def downgrade() -> None:
    op.drop_index("idx_cca_instrument_expiry", table_name="company_corporate_actions")
    op.drop_column("company_corporate_actions", "expiry_date")
    op.drop_column("company_key_ratios", "extra_ratios")
