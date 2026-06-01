"""Add is_nse_eligible to ai_trading_signals.

Revision ID: 0027
Revises: 0026
Create Date: 2026-05-13

Summary
-------
Adds ``is_nse_eligible`` to ``ai_trading_signals`` to support the NSE / Non-NSE
signal segmentation feature on the Cortex AI Trading Signals panel.

Motivation
----------
Signals are generated from multiple sources (RSS events, ML scheduler, on-demand API).
RSS-driven signals can reference companies not listed on NSE (foreign equities, indices,
debt instruments, etc.) that are absent from ``instrument_master``.  Previously these
were hard-rejected during signal assembly.  With this change the assembler stores them
with ``is_nse_eligible = FALSE`` instead, making them available as a separate
"Non-NSE" informational feed on the frontend while keeping the NSE tab unaffected.

Schema change
-------------
- ``is_nse_eligible BOOLEAN NOT NULL DEFAULT TRUE``
  All existing rows pre-date this feature: they were either assembled with
  ENABLE_SYMBOL_VALIDATION=True (validated eligible) or with validation disabled
  (assumed eligible).  Default TRUE is therefore the correct backfill value.

- Composite index ``idx_ai_trading_signals_eligible_ts`` on
  ``(is_nse_eligible, signal_timestamp DESC)`` — the primary index used by the
  tab-filtered GET /fusion/signals query (WHERE is_nse_eligible = ? ORDER BY
  signal_timestamp DESC LIMIT ?).  Created CONCURRENTLY so it does not lock the table.

Downgrade
---------
Drops the column (cascade-drops the index).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add column ──────────────────────────────────────────────────────────
    op.add_column(
        "ai_trading_signals",
        sa.Column(
            "is_nse_eligible",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("TRUE"),
        ),
    )

    # ── 2. Composite index for tab-filtered listing queries ────────────────────
    # CONCURRENTLY avoids an exclusive table lock on a potentially large table.
    # Alembic wraps upgrades in a transaction by default; CONCURRENTLY cannot
    # run inside a transaction block, so we step outside it here.
    op.execute("COMMIT")
    op.execute(
        """
        CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_ai_trading_signals_eligible_ts
        ON ai_trading_signals (is_nse_eligible, signal_timestamp DESC)
        """
    )


def downgrade() -> None:
    op.execute("COMMIT")
    op.execute(
        "DROP INDEX CONCURRENTLY IF EXISTS idx_ai_trading_signals_eligible_ts"
    )
    op.drop_column("ai_trading_signals", "is_nse_eligible")
