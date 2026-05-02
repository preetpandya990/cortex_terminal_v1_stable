"""
Trade Suggestions ORM Models
=============================
SQLAlchemy 2.0 models for multi-agent validated trade suggestions.
"""
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class TradeSuggestion(Base):
    """
    Multi-agent validated trade suggestion.
    
    Represents high-conviction trade opportunities validated across
    technical scanner, AI intelligence, and ML predictor agents.
    """
    __tablename__ = "trade_suggestions"
    
    # Primary keys
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    suggestion_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid4,
        server_default=text("gen_random_uuid()")
    )
    
    # Instrument identification
    symbol: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False)
    trading_symbol: Mapped[str | None] = mapped_column(String(50))
    
    # Consensus metadata
    consensus_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    confidence_level: Mapped[str] = mapped_column(String(20), nullable=False)
    signal_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    trigger_pathway: Mapped[str] = mapped_column(String(20), nullable=False)
    
    # Contributing signals (JSONB for flexibility)
    scanner_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ai_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    ml_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)
    
    # Trade parameters
    entry_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    risk_reward_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    take_profit_1: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    take_profit_2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    take_profit_3: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    
    # Temporal metadata
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        server_default=text("'active'")
    )
    
    # Audit trail
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()")
    )
    
    # Relationships
    correlations: Mapped[list["EventCorrelation"]] = relationship(
        "EventCorrelation",
        back_populates="suggestion",
        cascade="all, delete-orphan"
    )
    paper_orders: Mapped[list["PaperOrder"]] = relationship(  # type: ignore[name-defined]
        "PaperOrder",
        back_populates="suggestion",
        cascade="save-update, merge",
        passive_deletes=True,
        lazy="select",
    )
    paper_positions: Mapped[list["PaperPosition"]] = relationship(  # type: ignore[name-defined]
        "PaperPosition",
        back_populates="suggestion",
        cascade="save-update, merge",
        passive_deletes=True,
        lazy="select",
    )
    paper_outcomes: Mapped[list["PaperTradeOutcome"]] = relationship(  # type: ignore[name-defined]
        "PaperTradeOutcome",
        back_populates="suggestion",
        cascade="save-update, merge",
        passive_deletes=True,
        lazy="select",
    )
    
    # Table constraints (defined in migration, documented here)
    __table_args__ = (
        CheckConstraint(
            "consensus_score >= 0 AND consensus_score <= 100",
            name="trade_suggestions_consensus_score_check"
        ),
        CheckConstraint(
            "confidence_level IN ('HIGH', 'MEDIUM', 'LOW')",
            name="trade_suggestions_confidence_level_check"
        ),
        CheckConstraint(
            "signal_direction IN ('BUY', 'SELL')",
            name="trade_suggestions_signal_direction_check"
        ),
        CheckConstraint(
            "trigger_pathway IN ('TECHNICAL_FIRST', 'FUNDAMENTAL_FIRST')",
            name="trade_suggestions_trigger_pathway_check"
        ),
        CheckConstraint(
            "status IN ('active', 'expired', 'executed', 'invalidated')",
            name="trade_suggestions_status_check"
        ),
    )
    
    def __repr__(self) -> str:
        return (
            f"<TradeSuggestion(id={self.id}, "
            f"symbol={self.symbol}, "
            f"direction={self.signal_direction}, "
            f"score={self.consensus_score}, "
            f"status={self.status})>"
        )


class EventCorrelation(Base):
    """
    Event correlation audit trail.
    
    Tracks bidirectional signal validation events including agent
    response times, consensus decisions, and rejection reasons.
    """
    __tablename__ = "event_correlations"
    
    # Primary keys
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    correlation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        unique=True,
        nullable=False,
        default=uuid4,
        server_default=text("gen_random_uuid()")
    )
    suggestion_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_suggestions.suggestion_id", ondelete="SET NULL"),
        index=True
    )
    
    # Trigger metadata
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False)
    trigger_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True
    )
    
    # Agent response times (for latency monitoring)
    scanner_response_ms: Mapped[int | None] = mapped_column(Integer)
    ai_response_ms: Mapped[int | None] = mapped_column(Integer)
    ml_response_ms: Mapped[int | None] = mapped_column(Integer)
    total_latency_ms: Mapped[int | None] = mapped_column(Integer)
    
    # Consensus decision
    consensus_reached: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(200))
    
    # Raw agent outputs (for debugging)
    scanner_output: Mapped[dict | None] = mapped_column(JSONB)
    ai_output: Mapped[dict | None] = mapped_column(JSONB)
    ml_output: Mapped[dict | None] = mapped_column(JSONB)
    
    # Audit trail
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("NOW()")
    )
    
    # Relationships
    suggestion: Mapped["TradeSuggestion | None"] = relationship(
        "TradeSuggestion",
        back_populates="correlations"
    )
    
    # Table constraints (defined in migration, documented here)
    __table_args__ = (
        CheckConstraint(
            "trigger_type IN ('SCANNER_ANOMALY', 'NEWS_EVENT')",
            name="event_correlations_trigger_type_check"
        ),
    )
    
    def __repr__(self) -> str:
        return (
            f"<EventCorrelation(id={self.id}, "
            f"trigger_type={self.trigger_type}, "
            f"consensus_reached={self.consensus_reached}, "
            f"latency={self.total_latency_ms}ms)>"
        )
