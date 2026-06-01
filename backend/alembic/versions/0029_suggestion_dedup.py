"""Add suggestion deduplication: superseded status, partial unique index, cleanup.

Revision ID: 0029
Revises: 0028
Create Date: 2026-05-14

Problem:
  The correlation engine had no deduplication guard — the same 5 symbols were
  producing a new TradeSuggestion row every 30 seconds, accumulating 114+
  identical active suggestions.

Changes:
  1. Extend the status check constraint to include 'superseded'.
     A superseded suggestion was replaced by a materially better signal for
     the same (instrument_key, direction) pair.  Distinct from 'expired'
     (TTL) and 'invalidated' (user-dismissed).

  2. Clean up existing duplicate active suggestions by keeping the most
     recently generated row per (instrument_key, signal_direction) and
     marking all older duplicates as 'superseded'.

  3. Add a PARTIAL UNIQUE INDEX on (instrument_key, signal_direction)
     WHERE status = 'active' — the DB-level enforcement that guarantees
     at most one active suggestion per instrument+direction at all times,
     even under concurrent workers.

  4. Add a composite index on (instrument_key, signal_direction, status)
     for the O(1) dedup lookup the correlation engine runs before every
     new suggestion creation.

PRODUCTION NOTE:
  In a live, high-traffic environment, step 3 should be executed outside
  the migration transaction using CREATE UNIQUE INDEX CONCURRENTLY to
  avoid an ACCESS SHARE lock on the table.  For a dev/staging migration
  the blocking form used here is safe.
"""
from alembic import op
import sqlalchemy as sa


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Extend status check constraint to include 'superseded' ────────────
    op.execute("ALTER TABLE trade_suggestions DROP CONSTRAINT trade_suggestions_status_check")
    op.execute("""
        ALTER TABLE trade_suggestions
        ADD CONSTRAINT trade_suggestions_status_check
        CHECK (status IN ('active', 'expired', 'executed', 'invalidated', 'superseded'))
    """)

    # ── 2. Clean up existing duplicate active suggestions ────────────────────
    # Keep the most recently generated suggestion per (instrument_key, direction)
    # and mark all older duplicates as 'superseded'.  Uses a window function so
    # the CTE + UPDATE is a single atomic statement — no interleaved reads.
    op.execute("""
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY instrument_key, signal_direction
                       ORDER BY generated_at DESC, id DESC
                   ) AS rn
            FROM trade_suggestions
            WHERE status = 'active'
        )
        UPDATE trade_suggestions
        SET    status     = 'superseded',
               updated_at = NOW()
        WHERE  id IN (SELECT id FROM ranked WHERE rn > 1)
    """)

    # ── 3. Partial unique index — one active suggestion per instrument+direction
    op.execute("""
        CREATE UNIQUE INDEX idx_trade_suggestions_active_unique
        ON trade_suggestions(instrument_key, signal_direction)
        WHERE status = 'active'
    """)

    # ── 4. Composite index for O(1) dedup lookup in the correlation engine ───
    op.create_index(
        "idx_ts_dedup_lookup",
        "trade_suggestions",
        ["instrument_key", "signal_direction", "status"],
    )


def downgrade() -> None:
    op.drop_index("idx_ts_dedup_lookup", table_name="trade_suggestions")
    op.execute("DROP INDEX IF EXISTS idx_trade_suggestions_active_unique")

    # Restore any 'superseded' rows to 'expired' before tightening the constraint.
    op.execute("""
        UPDATE trade_suggestions
        SET status = 'expired', updated_at = NOW()
        WHERE status = 'superseded'
    """)

    op.execute("ALTER TABLE trade_suggestions DROP CONSTRAINT trade_suggestions_status_check")
    op.execute("""
        ALTER TABLE trade_suggestions
        ADD CONSTRAINT trade_suggestions_status_check
        CHECK (status IN ('active', 'expired', 'executed', 'invalidated'))
    """)
