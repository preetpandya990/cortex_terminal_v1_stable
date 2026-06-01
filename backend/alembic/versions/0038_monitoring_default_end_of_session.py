"""Change post_close_monitor_mode default from 'disabled' to 'end_of_session'

Revision ID: 0038
Revises: 0037
Create Date: 2026-05-20

Background
----------
Post-close monitoring shipped (0037) with 'disabled' as the server default.
Every user who never opened Portfolio Settings had monitoring silently off,
meaning no counterfactual price data was being collected for ML calibration.

The correct default is 'end_of_session': track until market close (15:30 IST)
at zero extra cost, giving the ML pipeline maximum signal.  Users who do not
want tracking can turn it off explicitly via Portfolio Settings.

Changes
-------
1. ALTER TABLE user_preferences — flip the column server default from
   'disabled' to 'end_of_session' so all future rows are monitored by default.

2. UPDATE user_preferences — flip existing rows where the value is still
   'disabled'.  These rows were populated by the column default rather than
   by an explicit user action, so correcting them is safe.  Rows that a user
   explicitly set to 'fixed_duration' or 'end_of_session' are untouched.

Downgrade
---------
Restores the server default to 'disabled'.  Existing row values are not
reversed — we cannot distinguish which rows were flipped by this migration
from those a user may have explicitly changed after the upgrade.  Existing
'end_of_session' rows remain valid under the 0037 CHECK constraint.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Flip the server default — affects all future INSERT statements.
    op.alter_column(
        "user_preferences",
        "post_close_monitor_mode",
        server_default=sa.text("'end_of_session'"),
        existing_type=sa.String(20),
        existing_nullable=False,
    )

    # 2. Back-fill existing rows that still carry the old 'disabled' default.
    #    Uses execute() with a bound text() — safe against SQL injection,
    #    consistent with Alembic's bulk-data-migration convention.
    op.execute(
        sa.text(
            "UPDATE user_preferences"
            " SET post_close_monitor_mode = 'end_of_session'"
            " WHERE post_close_monitor_mode = 'disabled'"
        )
    )


def downgrade() -> None:
    # Restore the server default only.  Row values are intentionally left as-is
    # because we cannot distinguish migration-flipped rows from user changes.
    op.alter_column(
        "user_preferences",
        "post_close_monitor_mode",
        server_default=sa.text("'disabled'"),
        existing_type=sa.String(20),
        existing_nullable=False,
    )
