"""Add ml_model_metadata.feature_version + ml_feature_cross_stats grid table.

Revision ID: 0056
Revises: 0055
Create Date: 2026-07-17

Context
-------
WS2c of the ML Fix & Upgrade plan (ML_FIX_IMPLEMENTATION_PLAN.md). The v2.0.0
feature set replaces the dead broadcast fundamentals (20 constant columns
z-scored to exactly 0) with point-in-time series rank-normalized
cross-sectionally. Two pieces of persistence make that safe to serve:

  ml_model_metadata.feature_version
      Which feature-set contract a model was trained on ('1.0.0' legacy
      69-feature, '2.0.0' PIT 66-feature). ``register_model`` already accepts
      the parameter but silently dropped it — no column existed. Inference
      gates on THIS persisted value, never on the training config flag, so
      live 1.0.0 models keep exact legacy behavior no matter what training
      does. Nullable; NULL is read as '1.0.0' by the registry loader, and
      existing rows are backfilled to '1.0.0' explicitly.

  ml_feature_cross_stats
      Per-(date, feature, version) cross-sectional rank grids: 101 raw-value
      quantiles + median + n_obs. Training writes them; inference maps a raw
      fundamental value through the newest grid with as_of_date <= row date
      (np.interp) to reproduce the training-time rank transform exactly.
      Unique on (as_of_date, feature_name, feature_version) — the upsert key;
      (feature_name, as_of_date) index backs the loader's per-feature
      date-range scans.

Downgrade
---------
Drop the grid table, then the column. Loses grid history and per-model
feature-version stamps — acceptable: downgrade implies reverting to the
legacy pipeline, which never reads either.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0056"
down_revision: Union[str, None] = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ml_model_metadata",
        sa.Column(
            "feature_version",
            sa.String(length=16),
            nullable=True,
            comment="Feature-set contract the model was trained on ('1.0.0' legacy 69-feature, '2.0.0' PIT rank-normalized 66-feature). NULL is read as '1.0.0'. Inference gates on this value, not on training config.",
        ),
    )
    # Every model registered before this migration was trained on the legacy
    # 69-feature set; stamp them explicitly rather than relying on NULL
    # semantics alone.
    op.execute("UPDATE ml_model_metadata SET feature_version = '1.0.0'")

    op.create_table(
        "ml_feature_cross_stats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("feature_name", sa.String(length=64), nullable=False),
        sa.Column("feature_version", sa.String(length=16), nullable=False),
        sa.Column(
            "quantiles",
            JSONB(),
            nullable=False,
            comment="101 raw-value quantiles (p0..p100) of the cross-section on as_of_date; inference rank-transforms via np.interp over this grid.",
        ),
        sa.Column("median", sa.Float(), nullable=False),
        sa.Column("n_obs", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "as_of_date",
            "feature_name",
            "feature_version",
            name="uq_ml_feature_cross_stats_date_feature_version",
        ),
    )
    op.create_index(
        "idx_ml_feature_cross_stats_feature_date",
        "ml_feature_cross_stats",
        ["feature_name", "as_of_date"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ml_feature_cross_stats_feature_date",
        table_name="ml_feature_cross_stats",
    )
    op.drop_table("ml_feature_cross_stats")
    op.drop_column("ml_model_metadata", "feature_version")
