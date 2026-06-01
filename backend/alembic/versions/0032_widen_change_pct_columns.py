"""Widen change_pct columns in fundamentals tables from NUMERIC(8,4) to NUMERIC

Revision ID: 0032
Revises: 0031
Create Date: 2026-05-17

NUMERIC(8,4) overflows for legitimate YoY percentage changes on companies with
near-zero base values (e.g. cash flow going -0.15 → -18.25 crore produces a
-12,066% change). NUMERIC (unlimited precision) has no overflow risk.

Tables affected:
  company_income_statement : revenue_change_pct, op_profit_change_pct, net_profit_change_pct
  company_cash_flow        : operating_cf_change_pct, investing_cf_change_pct, financing_cf_change_pct
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_IS_COLS = [
    "revenue_change_pct",
    "op_profit_change_pct",
    "net_profit_change_pct",
]

_CF_COLS = [
    "operating_cf_change_pct",
    "investing_cf_change_pct",
    "financing_cf_change_pct",
]


def upgrade() -> None:
    for col in _IS_COLS:
        op.alter_column(
            "company_income_statement",
            col,
            type_=sa.Numeric(),
            existing_type=sa.Numeric(8, 4),
            existing_nullable=True,
        )
    for col in _CF_COLS:
        op.alter_column(
            "company_cash_flow",
            col,
            type_=sa.Numeric(),
            existing_type=sa.Numeric(8, 4),
            existing_nullable=True,
        )


def downgrade() -> None:
    for col in _IS_COLS:
        op.alter_column(
            "company_income_statement",
            col,
            type_=sa.Numeric(8, 4),
            existing_type=sa.Numeric(),
            existing_nullable=True,
        )
    for col in _CF_COLS:
        op.alter_column(
            "company_cash_flow",
            col,
            type_=sa.Numeric(8, 4),
            existing_type=sa.Numeric(),
            existing_nullable=True,
        )
