"""Add company_name to trade_suggestions; pg_trgm GIN index on instrument_master.

Revision ID: 0025
Revises: 0024
Create Date: 2026-05-13

Summary of changes
-------------------
1. trade_suggestions
   • Add ``company_name`` VARCHAR(200) nullable.
     Denormalized from ``instrument_master.name`` — populated by the correlation
     engine at suggestion-creation time to avoid a live JOIN on every API read.
   • Backfill existing rows from ``instrument_master`` (best-effort via
     trading_symbol match; rows with no match stay NULL).

2. instrument_master
   • Enable the ``pg_trgm`` extension (idempotent — IF NOT EXISTS).
   • Add a GIN trigram index on ``upper(name)`` for efficient ILIKE / similarity
     searches used by ``normalize_and_validate_symbols`` in the event classifier.
     Index creation uses a regular CREATE INDEX (not CONCURRENTLY) because
     Alembic runs inside a transaction.  For large production tables (> 100k rows)
     this can be replaced with a manual CONCURRENTLY index after deployment.

Downgrade
---------
Drops the ``company_name`` column and the trigram index.
The ``pg_trgm`` extension is intentionally left in place on downgrade — it may
be used by other parts of the schema, and removing extensions requires DBA review.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add company_name column ─────────────────────────────────────────────
    op.add_column(
        "trade_suggestions",
        sa.Column("company_name", sa.String(200), nullable=True),
    )

    # ── 2. Backfill from instrument_master ─────────────────────────────────────
    # Joins on trading_symbol (NSE ticker) — covers both Pathway 1 (where
    # suggestion.symbol may be an instrument_key) and Pathway 2 (where it is
    # the trading symbol).  The trading_symbol column is always the human-readable
    # NSE ticker so it's the most reliable join key.
    #
    # We update via symbol first, then via trading_symbol for rows where symbol
    # contains the NSE ticker directly (Pathway 2 / majority of rows).
    op.execute(
        """
        UPDATE trade_suggestions ts
        SET    company_name = im.name
        FROM   instrument_master im
        WHERE  im.trading_symbol = ts.symbol
          AND  im.exchange        = 'NSE'
          AND  im.instrument_type = 'EQ'
          AND  im.name            IS NOT NULL
          AND  ts.company_name    IS NULL
        """
    )

    # For Pathway 1 rows where symbol is an instrument_key (e.g. NSE_EQ|INE…),
    # join on instrument_key directly.
    op.execute(
        """
        UPDATE trade_suggestions ts
        SET    company_name = im.name
        FROM   instrument_master im
        WHERE  im.instrument_key = ts.symbol
          AND  im.name            IS NOT NULL
          AND  ts.company_name    IS NULL
        """
    )

    # ── 3. pg_trgm extension ──────────────────────────────────────────────────
    # Enables trigram-based ILIKE searches.  IF NOT EXISTS is fully idempotent.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── 4. GIN trigram index on instrument_master.name ────────────────────────
    # Used by normalize_and_validate_symbols() for company-name → symbol
    # resolution when the LLM returns a full company name instead of a ticker.
    # Functional index on upper(name) so case-insensitive LIKE '%…%' hits the index.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_instrument_name_trgm
        ON instrument_master USING GIN ((upper(name)) gin_trgm_ops)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_instrument_name_trgm")
    op.drop_column("trade_suggestions", "company_name")
    # pg_trgm extension intentionally retained — may be used elsewhere.
