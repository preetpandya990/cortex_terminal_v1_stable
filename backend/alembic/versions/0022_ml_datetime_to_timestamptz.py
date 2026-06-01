"""Convert ML table timestamp columns from TIMESTAMP to TIMESTAMPTZ.

All existing values were written by Python's datetime.utcnow() — plain UTC
with no timezone info.  The USING clause re-labels each value as UTC so no
data is altered.

DB-side DEFAULT NOW() is added to NOT NULL columns that previously relied
solely on a Python-side default, making the schema self-consistent and
resilient to direct SQL inserts or future ORM changes.

Affected tables
───────────────
ml_model_metadata : trained_at, deployed_at, created_at, updated_at
ml_drift_metrics  : timestamp, alert_sent_at
ml_audit_logs     : timestamp
ml_predictions    : timestamp

ml_features is excluded — it was created with TIMESTAMPTZ in migration 0010.
ml_experiments does not exist in this database.

Revision ID: 0022
Revises: 0021
Create Date: 2026-05-10
"""
from alembic import op

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

# Columns that need a DB-side DEFAULT NOW() added (they had no DB default;
# Python was the sole source of the timestamp).
_ADD_DEFAULT: dict[str, list[str]] = {
    "ml_model_metadata": ["trained_at"],
    "ml_drift_metrics":  ["timestamp"],
    "ml_audit_logs":     ["timestamp"],
}

# Columns where we only change the type — either already have a DB default
# (created_at, updated_at) or are nullable / caller-supplied (deployed_at,
# alert_sent_at, ml_predictions.timestamp).
_TYPE_ONLY: dict[str, list[str]] = {
    "ml_model_metadata": ["deployed_at", "created_at", "updated_at"],
    "ml_drift_metrics":  ["alert_sent_at"],
    "ml_predictions":    ["timestamp"],
}

_ALTER = (
    "ALTER TABLE {table} ALTER COLUMN {col} "
    "TYPE TIMESTAMPTZ USING {col} AT TIME ZONE 'UTC'"
)
_SET_DEFAULT = "ALTER TABLE {table} ALTER COLUMN {col} SET DEFAULT NOW()"
_DROP_DEFAULT = "ALTER TABLE {table} ALTER COLUMN {col} DROP DEFAULT"
_REVERT = (
    "ALTER TABLE {table} ALTER COLUMN {col} "
    "TYPE TIMESTAMP WITHOUT TIME ZONE USING {col} AT TIME ZONE 'UTC'"
)


def upgrade() -> None:
    for table, cols in _ADD_DEFAULT.items():
        for col in cols:
            op.execute(_ALTER.format(table=table, col=col))
            op.execute(_SET_DEFAULT.format(table=table, col=col))

    for table, cols in _TYPE_ONLY.items():
        for col in cols:
            op.execute(_ALTER.format(table=table, col=col))


def downgrade() -> None:
    # Reverse type changes — values are converted back to naive UTC.
    for table, cols in _ADD_DEFAULT.items():
        for col in cols:
            op.execute(_REVERT.format(table=table, col=col))
            op.execute(_DROP_DEFAULT.format(table=table, col=col))

    for table, cols in _TYPE_ONLY.items():
        for col in cols:
            op.execute(_REVERT.format(table=table, col=col))
