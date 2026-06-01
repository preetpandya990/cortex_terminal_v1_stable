"""Add company_name to ai_trading_signals.

Revision ID: 0026
Revises: 0025
Create Date: 2026-05-13

Summary
-------
Adds a denormalized ``company_name`` column to ``ai_trading_signals``.

Motivation
----------
The Cortex AI page's Trading Signals panel displays ``symbol`` (NSE ticker)
but has no human-readable company name.  Joining ``instrument_master`` on
every signal read is wasteful — the name almost never changes and we already
have the pattern from ``trade_suggestions`` (migration 0025).

This migration mirrors that approach exactly:
  1. Add nullable ``company_name`` VARCHAR(200).
  2. Backfill from ``instrument_master`` via ``trading_symbol = symbol`` join.
  3. The ``signal_assembler`` populates the column at creation time going forward
     via ``SymbolValidatorService.get_company_name()`` (Redis-cached, ~0 overhead).

``pg_trgm`` and the GIN index on ``instrument_master.name`` were already
created in migration 0025 so no extension work is needed here.

Downgrade
---------
Drops the ``company_name`` column.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add column ──────────────────────────────────────────────────────────
    op.add_column(
        "ai_trading_signals",
        sa.Column("company_name", sa.String(200), nullable=True),
    )

    # ── 2. Backfill from instrument_master ─────────────────────────────────────
    # ai_trading_signals.symbol stores the NSE trading symbol (e.g. "RELIANCE").
    # instrument_master.trading_symbol is the same format, so the join is direct.
    op.execute(
        """
        UPDATE ai_trading_signals s
        SET    company_name = im.name
        FROM   instrument_master im
        WHERE  im.trading_symbol = s.symbol
          AND  im.exchange        = 'NSE'
          AND  im.instrument_type = 'EQ'
          AND  im.name            IS NOT NULL
          AND  s.company_name     IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("ai_trading_signals", "company_name")
