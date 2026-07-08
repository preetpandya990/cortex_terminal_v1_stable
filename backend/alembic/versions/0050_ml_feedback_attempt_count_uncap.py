"""Relax ml_feedback_errors.attempt_count check to a sanity floor.

Revision ID: 0050
Revises: 0049
Create Date: 2026-07-06

Context
-------
``ck_ml_feedback_errors_attempt_count`` was ``attempt_count BETWEEN 1 AND 5``,
hardcoding a retry ceiling that actually lives in application code
(``app.core.retry._MAX_ATTEMPTS``, currently 3).  If that policy were ever
retuned above 5, every legitimately-exhausted run would raise a DB-level
CheckViolation on insert — crashing the audit trail (the last line of defense
for spotting systematically failing symbols) over a routine config change.

The constraint keeps only the invariant the schema truly owns: an error
record must represent at least one attempt.

Steps (upgrade)
---------------
1. DROP CONSTRAINT ck_ml_feedback_errors_attempt_count.
2. ADD CONSTRAINT ck_ml_feedback_errors_attempt_count CHECK (attempt_count >= 1).

Downgrade
---------
Restores ``BETWEEN 1 AND 5``.  Safe as long as no rows with
``attempt_count > 5`` exist; the downgrade will fail loudly (constraint
validation) rather than silently corrupt if they do.
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_ml_feedback_errors_attempt_count"
_TABLE = "ml_feedback_errors"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "attempt_count >= 1")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, "attempt_count BETWEEN 1 AND 5")
