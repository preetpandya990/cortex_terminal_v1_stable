"""R6 — Registry consolidation: formalise ai_ml_models ↔ ml_model_metadata link,
drop the dead unified_model_registry table.

Revision ID: 0040
Revises: 0039
Create Date: 2026-06-02

Background
----------
Migration 0007 created ``unified_model_registry`` as an intended hub linking
``ml_model_metadata`` and ``ai_ml_models``.  It was never populated — no ORM
class and no application code ever wrote to it.  Workstream A8 (2026-05-23)
delivered the real consolidation architecturally: ``ModelPromoter`` became the
sole promotion authority and ``_project_to_ai_ml_models()`` maintains both
tables atomically in every transaction, making structural divergence impossible.

This migration completes the schema cleanup:

  1. Adds ``ml_model_metadata_id`` (FK → ml_model_metadata.id, SET NULL on
     delete) to ``ai_ml_models``.  This makes the implicit join that A8 relies
     on (``ai_ml_models.model_type == ml_model_metadata.model_name``) explicit
     and DB-enforced.  The column is nullable so existing rows are valid before
     the backfill runs and so a deleted ml_model_metadata row never orphan-kills
     the governance row.

  2. Backfills ``ml_model_metadata_id`` for every existing ``ai_ml_models`` row
     by matching on (model_type = model_name) AND (model_version = model_version)
     — the most precise available join key.

  3. Drops ``unified_model_registry`` (three indexes, then the table).  The table
     has always been empty; dropping it removes the only schema element that
     suggests a third, ambiguous authority for model state.

Downgrade is fully reversible: re-creates the unified_model_registry table in
its original 0007 shape and drops the FK column.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Add the FK column to ai_ml_models ─────────────────────────────────
    # Nullable: existing rows are valid before backfill, and a deleted
    # ml_model_metadata record must not cascade-delete the governance row.
    op.add_column(
        "ai_ml_models",
        sa.Column(
            "ml_model_metadata_id",
            sa.Integer(),
            sa.ForeignKey("ml_model_metadata.id", ondelete="SET NULL", name="fk_ai_ml_models_ml_model_metadata"),
            nullable=True,
        ),
    )
    op.create_index(
        "idx_ai_ml_models_ml_metadata_id",
        "ai_ml_models",
        ["ml_model_metadata_id"],
    )

    # ── 2. Backfill for existing rows ─────────────────────────────────────────
    # Join on (model_type = model_name) AND (model_version = model_version).
    # This is the most precise available key — model_type/model_name uniquely
    # identifies the model family and model_version pins the exact training run.
    # Rows with no matching ml_model_metadata record are left NULL (valid; they
    # represent governance rows for models registered before this migration or
    # via the now-deprecated UnifiedModelRegistry.register_model() path).
    op.execute(
        """
        UPDATE ai_ml_models a
        SET    ml_model_metadata_id = m.id
        FROM   ml_model_metadata m
        WHERE  m.model_name    = a.model_type
          AND  m.model_version = a.model_version
        """
    )

    # ── 3. Drop the dead unified_model_registry table ─────────────────────────
    # Indexes must be dropped explicitly before the table; PostgreSQL drops
    # them automatically on DROP TABLE but being explicit avoids "index not
    # found" surprises if the table was partially set up.
    op.drop_index("idx_unified_model_registry_type",   table_name="unified_model_registry", if_exists=True)
    op.drop_index("idx_unified_model_registry_state",  table_name="unified_model_registry", if_exists=True)
    op.drop_index("idx_unified_model_registry_active", table_name="unified_model_registry", if_exists=True)
    op.drop_table("unified_model_registry")


def downgrade() -> None:
    # ── Restore unified_model_registry in its original 0007 shape ─────────────
    op.create_table(
        "unified_model_registry",
        sa.Column("id",                  sa.BigInteger(),     primary_key=True, autoincrement=True),
        sa.Column("model_id",            sa.String(255),      nullable=False, unique=True),
        sa.Column("model_name",          sa.String(255),      nullable=False),
        sa.Column("model_type",          sa.String(50),       nullable=False),
        sa.Column("framework",           sa.String(50),       nullable=False),
        sa.Column("version",             sa.String(50),       nullable=False),
        sa.Column("deployment_state",    sa.String(20),       nullable=False, server_default="shadow"),
        sa.Column("ml_model_metadata_id", sa.Integer(),
                  sa.ForeignKey("ml_model_metadata.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ai_ml_model_id",      sa.BigInteger(),
                  sa.ForeignKey("ai_ml_models.id", ondelete="SET NULL"), nullable=True),
        sa.Column("model_path",          sa.String(500),      nullable=True),
        sa.Column("onnx_path",           sa.String(500),      nullable=True),
        sa.Column("config",              postgresql.JSONB(),  nullable=True),
        sa.Column("metrics",             postgresql.JSONB(),  nullable=True),
        sa.Column("is_active",           sa.Boolean(),        nullable=False, server_default="false"),
        sa.Column("created_at",          sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at",          sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("deployed_at",         sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_unified_model_registry_type",   "unified_model_registry", ["model_type"])
    op.create_index("idx_unified_model_registry_state",  "unified_model_registry", ["deployment_state"])
    op.create_index("idx_unified_model_registry_active", "unified_model_registry", ["is_active"])

    # ── Remove FK column from ai_ml_models ────────────────────────────────────
    op.drop_index("idx_ai_ml_models_ml_metadata_id", table_name="ai_ml_models")
    op.drop_constraint("fk_ai_ml_models_ml_model_metadata", "ai_ml_models", type_="foreignkey")
    op.drop_column("ai_ml_models", "ml_model_metadata_id")
