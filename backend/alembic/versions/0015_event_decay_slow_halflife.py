"""Add decay_slow_half_life_hours to ai_event_classifications

Revision ID: 0015
Revises: 0014
Create Date: 2026-05-03 00:00:00.000000

Adds a second decay half-life column to support the two-component Hawkes-kernel
temporal decay model validated on NSE equity data.

Previously, a single-component exponential decay was applied to all event types
with a flat 24-hour half-life regardless of event category.  Research shows that
high-impact events (earnings, M&A) have both a fast intraday component and a
persistent multi-day fundamental component that a single exponential misses.

Two-component model:
    decay(t) = 0.7 · 0.5^(t / fast_hl) + 0.3 · 0.5^(t / slow_hl)

The 0.7 / 0.3 fast / slow split is the NSE-calibrated constant from the plan.

Schema change:
  - ADD COLUMN decay_slow_half_life_hours INTEGER NOT NULL DEFAULT 72
  - Backfill existing rows using per-event-type slow half-life lookup
  - Fully backward compatible: the existing decay_half_life_hours column
    becomes the fast half-life and is untouched by this migration

Downgrade removes the column; existing rows revert to single-component decay.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0015'
down_revision: Union[str, None] = '0014'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ──────────────────────────────────────────────────────────────────────────────
# UPGRADE
# ──────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # Adding a NOT NULL column with a constant server_default is an instant
    # metadata-only operation on PostgreSQL 11+ — no table rewrite.
    op.add_column(
        'ai_event_classifications',
        sa.Column(
            'decay_slow_half_life_hours',
            sa.Integer(),
            nullable=False,
            server_default=sa.text('72'),
        ),
    )

    # Backfill existing rows with per-event-type slow half-lives.
    # The ELSE branch covers any event_type not in the known taxonomy.
    op.execute(
        """
        UPDATE ai_event_classifications
        SET    decay_slow_half_life_hours = CASE event_type
                   WHEN 'earnings'           THEN  72
                   WHEN 'fed_announcement'   THEN  48
                   WHEN 'merger_acquisition' THEN 120
                   WHEN 'regulatory'         THEN 168
                   WHEN 'geopolitical'       THEN  72
                   WHEN 'market_data'        THEN  24
                   WHEN 'company_news'       THEN  48
                   WHEN 'sector_news'        THEN  72
                   ELSE                            48
               END
        """
    )


# ──────────────────────────────────────────────────────────────────────────────
# DOWNGRADE
# ──────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    op.drop_column('ai_event_classifications', 'decay_slow_half_life_hours')
