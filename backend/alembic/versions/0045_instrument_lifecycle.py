"""Instrument lifecycle — soft-delete + reconciliation columns on instrument_master.

Revision ID: 0045
Revises: 0044
Create Date: 2026-06-10

Background
----------
``instrument_master`` is synced from Upstox's daily begin-of-day (BOD)
``NSE.json.gz`` file.  That file only contains *currently listed, tradeable*
instruments — the next trading day's file silently drops delisted stocks and
expired contracts.  The previous sync logic was upsert-only, so once an
instrument disappeared from the file its row lingered forever and continued to
resolve as a valid, tradeable symbol.  For a trading system that is a
correctness and safety defect.

This migration adds the columns needed to reconcile the table against each
day's file and to *soft-delete* instruments that drop out — preserving history
and any foreign references (OHLCV, suggestions, positions) rather than hard
deleting them.

Design decisions
----------------
  is_active (NOT NULL, default true)
      Derived state, not supplied by Upstox: ``true`` iff the instrument was
      present in the most recent successful sync.  Consumers that need the live
      tradeable universe filter ``WHERE is_active``.

  last_seen_at (NOT NULL, default now())
      Watermark stamped to the sync's start time for every instrument present
      in the downloaded file.  Reconciliation marks any row whose watermark is
      older than the current run's start as delisted — this avoids a
      ``WHERE instrument_key NOT IN (... thousands of keys ...)`` scan and lets
      a single indexed UPDATE reconcile the whole table.

  delisted_at (NULL)
      Timestamp the instrument was first observed missing from the file.
      Cleared back to NULL on reactivation (a relisted instrument reappearing
      in a later file).

Indexes
-------
  idx_instrument_active — partial index on (trading_symbol) WHERE is_active.
      Active-universe lookups (the hot path) stay fast and the index does not
      bloat with delisted rows.

Backfill
--------
The 2,553 pre-existing rows were all present in the last manual sync, so they
are backfilled as active with last_seen_at = now().

Downgrade
---------
Drops the partial index and all three columns.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── New lifecycle columns ────────────────────────────────────────────────
    # Added with server_default so the backfill of existing rows is atomic and
    # NOT NULL is satisfiable in a single statement.
    op.add_column(
        "instrument_master",
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment="True iff present in the most recent successful sync (live tradeable universe).",
        ),
    )
    op.add_column(
        "instrument_master",
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            comment="Sync-start watermark; reconciliation delists rows older than the current run.",
        ),
    )
    op.add_column(
        "instrument_master",
        sa.Column(
            "delisted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the instrument was first observed missing from the file; NULL while active.",
        ),
    )

    # ── Partial index for the active-universe hot path ───────────────────────
    op.create_index(
        "idx_instrument_active",
        "instrument_master",
        ["trading_symbol"],
        postgresql_where=sa.text("is_active"),
    )


def downgrade() -> None:
    op.drop_index("idx_instrument_active", table_name="instrument_master")
    op.drop_column("instrument_master", "delisted_at")
    op.drop_column("instrument_master", "last_seen_at")
    op.drop_column("instrument_master", "is_active")
