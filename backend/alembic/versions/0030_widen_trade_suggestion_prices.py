"""Widen TradeSuggestion price columns from NUMERIC(12,2) to NUMERIC(12,4).

Revision ID: 0030
Revises: 0029
Create Date: 2026-05-15

Problem:
  TradeSuggestion stored stop_loss and take_profit_1/2/3 as NUMERIC(12,2),
  giving ₹0.01 precision.  PaperPosition (the execution record) already uses
  NUMERIC(12,4), creating a precision mismatch across the trade lifecycle:
  a signal written at 4dp is silently truncated when persisted to
  TradeSuggestion, and any downstream comparison against execution records
  may diverge at the sub-₹0.01 level.

Changes:
  Widen the four price columns on trade_suggestions to NUMERIC(12,4):
    • stop_loss
    • take_profit_1
    • take_profit_2
    • take_profit_3

  This is a pure widening — no data is lost and no DEFAULT is required.
  All existing ₹0.01-precision values round-trip identically under the
  wider type (147.35 → 147.3500).

PRODUCTION NOTE:
  PostgreSQL must rewrite the table heap when changing a NUMERIC type's
  scale, even when widening.  On a large table this acquires an
  ACCESS EXCLUSIVE lock for the duration of the rewrite.  For production
  environments with significant data volume, prefer running each ALTER
  COLUMN outside a migration transaction using a lock-timeout guard:

      SET lock_timeout = '2s';
      ALTER TABLE trade_suggestions
          ALTER COLUMN stop_loss TYPE NUMERIC(12,4);
      -- repeat for remaining columns

  and schedule during a low-traffic window.  For dev / staging this
  blocking form is safe.
"""
from alembic import op


revision     = "0030"
down_revision = "0029"
branch_labels = None
depends_on    = None

_TABLE   = "trade_suggestions"
_COLUMNS = ("stop_loss", "take_profit_1", "take_profit_2", "take_profit_3")
_WIDE    = "NUMERIC(12, 4)"
_NARROW  = "NUMERIC(12, 2)"


def upgrade() -> None:
    for col in _COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ALTER COLUMN {col} TYPE {_WIDE}"
        )


def downgrade() -> None:
    # Narrowing truncates sub-₹0.01 digits. Safe only when no 4dp values
    # have been written; otherwise data precision is permanently lost.
    for col in _COLUMNS:
        op.execute(
            f"ALTER TABLE {_TABLE} "
            f"ALTER COLUMN {col} TYPE {_NARROW}"
        )
