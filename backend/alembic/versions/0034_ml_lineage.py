"""Add lineage JSONB column to ml_model_metadata

Revision ID: 0034
Revises: 0033
Create Date: 2026-05-18

Adds a nullable JSONB `lineage` column to ml_model_metadata for end-to-end
reproducibility of the gated model-promotion pipeline (ML overhaul, Workstream
A0). It stores the provenance needed to reproduce and audit any promoted model:
git_commit, data_window{start,end}, config_hash, feature_manifest_sha,
calibrator_sha, cpcv{n_folds,n_test_folds,purge,embargo}, dsr, pbo.

Nullable with no backfill: existing rows keep NULL until their next
(re)training run writes lineage. No data migration is required, and the
change is fully reversible.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ml_model_metadata",
        sa.Column("lineage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ml_model_metadata", "lineage")
