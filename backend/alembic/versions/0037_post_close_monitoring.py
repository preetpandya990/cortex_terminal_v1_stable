"""Add post-close monitoring config to user_preferences and post_close_monitors table

Revision ID: 0037
Revises: 0036
Create Date: 2026-05-19

Two schema changes required by the post-close counterfactual monitoring feature:

  1. user_preferences — two new columns that store the account-wide monitoring
     preference for each user:

       post_close_monitor_mode VARCHAR(20) NOT NULL DEFAULT 'disabled'
         Allowed: 'disabled' | 'fixed_duration' | 'end_of_session'

       post_close_monitor_duration_hours NUMERIC(4,1) NULL
         Only meaningful when mode = 'fixed_duration'.  Range: (0, 72].
         NULL for all other modes.

  2. post_close_monitors — new table tracking counterfactual price action
     after a position is auto-closed by SL or TP.  Written by the pnl_worker
     immediately after each auto-close when the user has monitoring enabled.

     Indexes:
       ix_pcm_instrument_status  (instrument_key, status)
         — fast O(log n) dispatch in the monitor loop
       ix_pcm_user_status_created  (user_id, status, created_at DESC)
         — paginated list queries in the REST API
       ix_pcm_monitor_until_status  (monitor_until, status)
         — expiry sweep (finds monitors whose window has elapsed)
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add monitoring config columns to user_preferences ─────────────────
    op.add_column(
        "user_preferences",
        sa.Column(
            "post_close_monitor_mode",
            sa.String(20),
            nullable=False,
            server_default="disabled",
        ),
    )
    op.add_column(
        "user_preferences",
        sa.Column(
            "post_close_monitor_duration_hours",
            sa.Numeric(4, 1),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_user_prefs_pcm_mode",
        "user_preferences",
        "post_close_monitor_mode IN ('disabled', 'fixed_duration', 'end_of_session')",
    )
    op.create_check_constraint(
        "ck_user_prefs_pcm_duration",
        "user_preferences",
        (
            "post_close_monitor_duration_hours IS NULL "
            "OR (post_close_monitor_duration_hours > 0 "
            "    AND post_close_monitor_duration_hours <= 72)"
        ),
    )

    # ── 2. Create post_close_monitors table ───────────────────────────────────
    op.create_table(
        "post_close_monitors",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        # One monitor per outcome (1-to-1)
        sa.Column(
            "outcome_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_trade_outcomes.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "position_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("paper_positions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "portfolio_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("portfolios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized for fast single-table queries
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol",         sa.String(50),  nullable=False),
        sa.Column("instrument_key", sa.String(100), nullable=False),
        sa.Column("direction",      sa.String(5),   nullable=False),
        sa.Column("close_reason",   sa.String(10),  nullable=False),

        # Reference prices captured at close time — immutable
        sa.Column("close_price",  sa.Numeric(12, 4), nullable=False),
        sa.Column("entry_price",  sa.Numeric(12, 4), nullable=False),
        sa.Column("stop_loss",    sa.Numeric(12, 4), nullable=True),

        # Remaining targets at close time
        sa.Column("remaining_tp1", sa.Numeric(12, 4), nullable=True),
        sa.Column("remaining_tp2", sa.Numeric(12, 4), nullable=True),
        sa.Column("remaining_tp3", sa.Numeric(12, 4), nullable=True),

        sa.Column(
            "monitor_until",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(10),
            nullable=False,
            server_default="ACTIVE",
        ),

        # Outcome fields — written as monitoring progresses
        sa.Column("peak_favorable_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("peak_adverse_price",   sa.Numeric(12, 4), nullable=True),
        sa.Column(
            "recovered_above_sl",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "recovered_above_entry",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("tp1_hit_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("tp2_hit_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("tp3_hit_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("sl_recovery_at",  sa.DateTime(timezone=True), nullable=True),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),

        # ── Constraints ────────────────────────────────────────────────────────
        sa.CheckConstraint(
            "close_reason IN ('SL', 'TP1', 'TP2', 'TP3')",
            name="ck_pcm_close_reason",
        ),
        sa.CheckConstraint(
            "direction IN ('LONG', 'SHORT')",
            name="ck_pcm_direction",
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'COMPLETED', 'EXPIRED')",
            name="ck_pcm_status",
        ),
        sa.CheckConstraint(
            "close_price > 0",
            name="ck_pcm_close_price_positive",
        ),
        sa.CheckConstraint(
            "entry_price > 0",
            name="ck_pcm_entry_price_positive",
        ),
    )

    # ── Composite indexes ─────────────────────────────────────────────────────
    op.create_index(
        "ix_pcm_instrument_status",
        "post_close_monitors",
        ["instrument_key", "status"],
    )
    op.create_index(
        "ix_pcm_user_status_created",
        "post_close_monitors",
        ["user_id", "status", "created_at"],
        postgresql_ops={"created_at": "DESC"},
    )
    op.create_index(
        "ix_pcm_monitor_until_status",
        "post_close_monitors",
        ["monitor_until", "status"],
    )


def downgrade() -> None:
    # ── Drop post_close_monitors ──────────────────────────────────────────────
    op.drop_index("ix_pcm_monitor_until_status", table_name="post_close_monitors")
    op.drop_index("ix_pcm_user_status_created",  table_name="post_close_monitors")
    op.drop_index("ix_pcm_instrument_status",    table_name="post_close_monitors")
    op.drop_table("post_close_monitors")

    # ── Drop columns from user_preferences ───────────────────────────────────
    op.drop_constraint("ck_user_prefs_pcm_duration", "user_preferences")
    op.drop_constraint("ck_user_prefs_pcm_mode",     "user_preferences")
    op.drop_column("user_preferences", "post_close_monitor_duration_hours")
    op.drop_column("user_preferences", "post_close_monitor_mode")
