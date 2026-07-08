"""Add ai_event_classifications.sentiment (directional read, finally persisted).

Revision ID: 0052
Revises: 0051
Create Date: 2026-07-06

Context
-------
The event classifier has always computed a directional sentiment
(bullish/bearish/neutral) — in its Gemini response schema, its keyword
heuristic, and its rule-based fallback — but no path ever persisted it.
Downstream, Pathway-2 consensus needed a direction for symbols outside the
scanner cache and improvised one from ``impact_score > 0`` … but impact_score
is an UNSIGNED severity magnitude (0-100, default 50), so the synthetic
direction was effectively hardcoded "buy": a fraud investigation with
impact 85 produced a BUY-leaning signal.

This migration adds the column; the classifier writes it from this release
on, and Pathway-2 derives direction from it (neutral/NULL → no synthetic
direction, scanner marked unavailable).

No backfill: recomputing sentiment for historical rows would need the source
article text (a join away) for a heuristic pass at best, and classifications
older than ~48h have fully decayed out of consensus relevance. NULL simply
means "classified before 0052" and is treated as neutral.

Downgrade
---------
DROP the column.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0052"
down_revision: Union[str, None] = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_event_classifications",
        sa.Column("sentiment", sa.String(10), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_event_classifications", "sentiment")
