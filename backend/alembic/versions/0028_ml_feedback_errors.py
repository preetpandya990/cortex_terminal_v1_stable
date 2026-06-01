"""Create ml_feedback_errors table for ML feedback retry audit trail.

Revision ID: 0028
Revises: 0027
Create Date: 2026-05-13

Introduces ml_feedback_errors — an append-only audit table written when all
retry attempts for compute_ml_feedback() are exhausted for a given outcome.

Design decisions:
  - Append-only: no updated_at column; resolved/resolved_at/resolved_by
    are the only mutable fields (ops acknowledgement workflow).
  - outcome_id uses SET NULL on delete so error records survive
    administrative outcome purges — preserving the ops audit trail.
  - symbol/portfolio_id/user_id are denormalized for O(1) admin queries.
  - attempt_count CHECK constraint (1–5) future-proofs against config changes.
  - Indexes on (symbol, created_at DESC) and (resolved, created_at DESC) for
    the two primary admin query patterns: per-symbol forensics and
    unresolved error dashboards.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ml_feedback_errors",

        # ── Identity ──────────────────────────────────────────────────────────
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),

        # outcome_id — SET NULL so the error record outlives the outcome row
        sa.Column(
            "outcome_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),

        # ── Denormalized context ───────────────────────────────────────────────
        sa.Column("symbol",       sa.String(50),              nullable=False),
        sa.Column("portfolio_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id",      sa.Integer(),               nullable=False),

        # ── Failure details ────────────────────────────────────────────────────
        sa.Column("error_message", sa.Text(),        nullable=False),
        sa.Column("error_type",    sa.String(200),   nullable=True),
        sa.Column("attempt_count", sa.Integer(),     nullable=False),

        # ── Timing ────────────────────────────────────────────────────────────
        sa.Column("first_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at",  sa.DateTime(timezone=True), nullable=False),

        # ── Resolution tracking ────────────────────────────────────────────────
        sa.Column(
            "resolved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("resolved_at",      sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by",      sa.String(100),             nullable=True),
        sa.Column("resolution_note",  sa.Text(),                  nullable=True),

        # ── Immutable timestamp ────────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),

        # ── Constraints ───────────────────────────────────────────────────────
        sa.PrimaryKeyConstraint("id", name="pk_ml_feedback_errors"),
        sa.CheckConstraint(
            "attempt_count BETWEEN 1 AND 5",
            name="ck_ml_feedback_errors_attempt_count",
        ),
        sa.ForeignKeyConstraint(
            ["outcome_id"],
            ["paper_trade_outcomes.id"],
            ondelete="SET NULL",
            name="fk_ml_feedback_errors_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_ml_feedback_errors_user",
        ),
    )

    # ── Indexes ────────────────────────────────────────────────────────────────

    # Fast lookup by outcome (FK queries, admin forensics)
    op.create_index(
        "idx_ml_feedback_errors_outcome_id",
        "ml_feedback_errors",
        ["outcome_id"],
        postgresql_using="btree",
    )

    # Per-symbol forensics: most recent first
    op.create_index(
        "idx_ml_feedback_errors_symbol_created",
        "ml_feedback_errors",
        ["symbol", sa.text("created_at DESC")],
        postgresql_using="btree",
    )

    # Unresolved error dashboard: ops queue
    op.create_index(
        "idx_ml_feedback_errors_unresolved",
        "ml_feedback_errors",
        ["resolved", sa.text("created_at DESC")],
        postgresql_using="btree",
        postgresql_where=sa.text("resolved = false"),
    )

    # Per-user error history
    op.create_index(
        "idx_ml_feedback_errors_user_id",
        "ml_feedback_errors",
        ["user_id"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("idx_ml_feedback_errors_user_id",      table_name="ml_feedback_errors")
    op.drop_index("idx_ml_feedback_errors_unresolved",   table_name="ml_feedback_errors")
    op.drop_index("idx_ml_feedback_errors_symbol_created", table_name="ml_feedback_errors")
    op.drop_index("idx_ml_feedback_errors_outcome_id",   table_name="ml_feedback_errors")
    op.drop_table("ml_feedback_errors")
