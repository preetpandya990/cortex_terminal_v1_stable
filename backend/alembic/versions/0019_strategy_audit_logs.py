"""Add strategy_audit_logs table for admin promote/demote audit trail.

Revision ID: 0019
Revises: 0018
Create Date: 2026-05-06

Creates an append-only audit table that records every admin action that
changes a strategy's scope (private ↔ shared).  Rows are never updated
or deleted — they form an immutable chain of custody.

Table: strategy_audit_logs
  id             BIGSERIAL PK
  strategy_id    UUID FK → strategies.id (SET NULL on cascade so audit is retained)
  admin_user_id  INTEGER FK → users.id (SET NULL on cascade)
  action         VARCHAR(20)  — 'promote' | 'demote'
  reason         TEXT         — required human-readable justification
  scope_before   VARCHAR(10)  — scope value before the action
  scope_after    VARCHAR(10)  — scope value after the action
  strategy_name  VARCHAR(100) — denormalised snapshot (survives strategy deletion)
  created_at     TIMESTAMPTZ  — server-side now()

Indexes:
  idx_strategy_audit_strategy   — strategy_id (listing audit history per strategy)
  idx_strategy_audit_admin      — admin_user_id (listing actions by admin)
  idx_strategy_audit_created_at — created_at   (chronological listing)
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True, nullable=False),
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "admin_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("scope_before", sa.String(10), nullable=False),
        sa.Column("scope_after", sa.String(10), nullable=False),
        sa.Column("strategy_name", sa.String(100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
            index=True,
        ),
    )

    op.create_check_constraint(
        "ck_strategy_audit_action",
        "strategy_audit_logs",
        "action IN ('promote', 'demote')",
    )
    op.create_check_constraint(
        "ck_strategy_audit_scope_before",
        "strategy_audit_logs",
        "scope_before IN ('private', 'shared')",
    )
    op.create_check_constraint(
        "ck_strategy_audit_scope_after",
        "strategy_audit_logs",
        "scope_after IN ('private', 'shared')",
    )


def downgrade() -> None:
    op.drop_table("strategy_audit_logs")
