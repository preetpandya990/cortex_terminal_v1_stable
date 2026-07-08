"""
ML Feedback Error — ORM Model
==============================
Audit table for ML feedback computation failures.

Written when all retry attempts for compute_ml_feedback() are exhausted.
Append-only by design — never updated or deleted.

Fields are fully denormalized (symbol, portfolio_id, user_id) so that
admin/ops queries require no joins and remain fast even under high cardinality.

The `resolved` flag allows ops to acknowledge errors after manual remediation.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MLFeedbackError(Base):
    """
    Append-only record of a failed ML feedback computation.

    Created exactly once when all retry attempts for a given outcome are
    exhausted.  Provides a durable audit trail for ops teams and enables
    automated alerting dashboards to surface systematically failing symbols
    or portfolio configurations.
    """

    __tablename__ = "ml_feedback_errors"

    __table_args__ = (
        # Sanity floor only. The retry ceiling is a runtime policy owned by
        # app.core.retry._MAX_ATTEMPTS — hardcoding it here (as the original
        # BETWEEN 1 AND 5 did) turns a routine retry-policy retune into a DB
        # CheckViolation inside the audit trail itself. See migration 0050.
        CheckConstraint(
            "attempt_count >= 1",
            name="ck_ml_feedback_errors_attempt_count",
        ),
    )

    # ── Identity ───────────────────────────────────────────────────────────────
    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )

    # outcome_id is SET NULL on delete so the error record survives
    # if the outcome is ever administratively purged.
    outcome_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("paper_trade_outcomes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    # ── Denormalized context (avoids joins in ops queries) ─────────────────────
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    portfolio_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # ── Failure details ────────────────────────────────────────────────────────
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    error_type: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="Python exception class name, e.g. 'sqlalchemy.exc.OperationalError'",
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # ── Timing ─────────────────────────────────────────────────────────────────
    first_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )

    # ── Resolution tracking ────────────────────────────────────────────────────
    resolved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
        index=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="Username or system identifier that resolved the error",
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── Immutable timestamp — no updated_at by design ─────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return (
            f"<MLFeedbackError(id={self.id}, outcome={self.outcome_id}, "
            f"symbol={self.symbol}, attempts={self.attempt_count}, "
            f"resolved={self.resolved})>"
        )
