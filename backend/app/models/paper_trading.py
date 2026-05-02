"""
Paper Trading & Audit ORM Models
==================================
SQLAlchemy 2.0 declarative models for the Paper Trading subsystem.

Model hierarchy (ownership / FK direction):
  User
  └─ Portfolio          (one active paper portfolio per user)
     ├─ PaperOrder      (append-only order intent records)
     │  └─ PaperFill    (immutable execution records — TimescaleDB hypertable)
     ├─ PaperPosition   (mutable live position state, WAC cost basis)
     │  └─ PaperTradeOutcome  (append-only audit + ML feedback, written on close)
     └─ PaperPnlSnapshot      (daily EOD snapshots — TimescaleDB hypertable)

Invariants enforced here and in the service layer:
  - All monetary columns use Decimal / NUMERIC — never float — to avoid drift
  - PaperFill and PaperTradeOutcome are append-only: no updated_at column exists
  - WAC (Weighted Average Cost) is the SEBI-mandated cost basis for NSE equity
  - T+1 settlement is tracked via PaperFill.settlement_date
  - At most one OPEN PaperPosition per (portfolio, symbol) — partial unique index
    uq_paper_positions_portfolio_symbol_open enforced in the DB migration
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ──────────────────────────────────────────────────────────────────────────────
# Portfolio
# ──────────────────────────────────────────────────────────────────────────────

class Portfolio(Base):
    """
    Per-user paper (or live) trading portfolio.

    A single active PAPER portfolio exists per user. The portfolio_type column
    is a forward-compatibility discriminator so LIVE portfolios can share the
    same schema and all downstream P&L / position code without duplication.

    Risk settings (risk_per_trade_pct, max_open_positions) are user-adjustable
    and drive the system-suggested quantity calculation:
        suggested_qty = floor(current_cash * risk_per_trade_pct / 100
                              / (entry_price - stop_loss))
    """

    __tablename__ = "portfolios"

    __table_args__ = (
        CheckConstraint(
            "portfolio_type IN ('PAPER', 'LIVE')",
            name="ck_portfolios_type",
        ),
        CheckConstraint(
            "risk_per_trade_pct > 0 AND risk_per_trade_pct <= 10",
            name="ck_portfolios_risk_pct",
        ),
        CheckConstraint(
            "max_open_positions >= 1 AND max_open_positions <= 100",
            name="ck_portfolios_max_positions",
        ),
        CheckConstraint(
            "current_cash >= 0",
            name="ck_portfolios_cash_non_negative",
        ),
        CheckConstraint(
            "initial_capital > 0",
            name="ck_portfolios_initial_capital_positive",
        ),
        # Partial unique index (one active portfolio per user) is in the
        # migration: uq_portfolios_user_active WHERE is_active = true.
        # Cannot be expressed as a UniqueConstraint here.
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    portfolio_type: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="PAPER",
        server_default=text("'PAPER'"),
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    initial_capital: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    current_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="INR",
        server_default=text("'INR'"),
    )

    risk_per_trade_pct: Mapped[Decimal] = mapped_column(
        Numeric(4, 2),
        nullable=False,
        default=Decimal("2.00"),
        server_default=text("2.00"),
    )
    max_open_positions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        server_default=text("10"),
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="portfolios",
    )
    orders: Mapped[list["PaperOrder"]] = relationship(
        "PaperOrder",
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="PaperOrder.placed_at.desc()",
        lazy="select",
    )
    positions: Mapped[list["PaperPosition"]] = relationship(
        "PaperPosition",
        back_populates="portfolio",
        cascade="all, delete-orphan",
        lazy="select",
    )
    outcomes: Mapped[list["PaperTradeOutcome"]] = relationship(
        "PaperTradeOutcome",
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="PaperTradeOutcome.created_at.desc()",
        lazy="select",
    )
    pnl_snapshots: Mapped[list["PaperPnlSnapshot"]] = relationship(
        "PaperPnlSnapshot",
        back_populates="portfolio",
        cascade="all, delete-orphan",
        order_by="PaperPnlSnapshot.snapshot_date.desc()",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<Portfolio(id={self.id}, user_id={self.user_id}, "
            f"type={self.portfolio_type}, cash={self.current_cash}, "
            f"active={self.is_active})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# PaperOrder
# ──────────────────────────────────────────────────────────────────────────────

class PaperOrder(Base):
    """
    Paper order intent record.

    Append-only in practice: order lifecycle is tracked via status transitions
    (PENDING → OPEN → COMPLETE / REJECTED / CANCELLED). Records are never
    deleted while fills exist (FK ON DELETE RESTRICT on paper_fills.order_id).

    Uses Upstox / Zerodha order taxonomy:
      transaction_type : BUY | SELL
      product_type     : CNC (delivery) | MIS (intraday) | NRML (F&O carry)
      order_type       : MARKET | LIMIT | SL | SL-M
      validity         : DAY | IOC
    """

    __tablename__ = "paper_orders"

    __table_args__ = (
        CheckConstraint(
            "transaction_type IN ('BUY', 'SELL')",
            name="ck_paper_orders_transaction_type",
        ),
        CheckConstraint(
            "product_type IN ('CNC', 'MIS', 'NRML')",
            name="ck_paper_orders_product_type",
        ),
        CheckConstraint(
            "order_type IN ('MARKET', 'LIMIT', 'SL', 'SL-M')",
            name="ck_paper_orders_order_type",
        ),
        CheckConstraint(
            "validity IN ('DAY', 'IOC')",
            name="ck_paper_orders_validity",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'OPEN', 'COMPLETE', 'REJECTED', 'CANCELLED')",
            name="ck_paper_orders_status",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_paper_orders_quantity_positive",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )
    suggestion_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_suggestions.suggestion_id", ondelete="SET NULL"),
        nullable=True,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False)

    transaction_type: Mapped[str] = mapped_column(String(4), nullable=False)
    product_type: Mapped[str] = mapped_column(String(10), nullable=False)
    order_type: Mapped[str] = mapped_column(String(10), nullable=False)
    validity: Mapped[str] = mapped_column(
        String(6),
        nullable=False,
        default="DAY",
        server_default=text("'DAY'"),
    )

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    trigger_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))

    status: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default="PENDING",
        server_default=text("'PENDING'"),
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(200))

    placed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio",
        back_populates="orders",
    )
    suggestion: Mapped["TradeSuggestion | None"] = relationship(  # type: ignore[name-defined]
        "TradeSuggestion",
        back_populates="paper_orders",
    )
    fills: Mapped[list["PaperFill"]] = relationship(
        "PaperFill",
        back_populates="order",
        # ON DELETE RESTRICT on the FK — fills cannot be orphaned via cascade.
        # passive_deletes lets the DB handle constraint enforcement.
        cascade="save-update, merge",
        passive_deletes=True,
        order_by="PaperFill.executed_at.asc()",
        lazy="select",
    )

    @property
    def is_active(self) -> bool:
        return self.status in ("PENDING", "OPEN")

    @property
    def is_terminal(self) -> bool:
        return self.status in ("COMPLETE", "REJECTED", "CANCELLED")

    def __repr__(self) -> str:
        return (
            f"<PaperOrder(id={self.id}, symbol={self.symbol}, "
            f"type={self.transaction_type}/{self.order_type}, "
            f"qty={self.quantity}, status={self.status})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# PaperFill
# ──────────────────────────────────────────────────────────────────────────────

class PaperFill(Base):
    """
    Immutable execution record.

    Written exactly once when an order is filled against the Upstox tick feed.
    Never updated or deleted — the DB schema has no updated_at column by design.

    NSE charge columns are stored individually (not just total_charges) so the
    ML feedback pipeline can attribute performance drag to specific cost components.

    T+1 settlement is tracked via settlement_date: for CNC delivery, shares
    purchased are not available for re-sale until T+1 (next trading day). The
    service layer enforces this when evaluating available quantity for new SELL
    orders.

    This table is a TimescaleDB hypertable partitioned by executed_at when
    TimescaleDB is available; otherwise a standard PostgreSQL table.
    """

    __tablename__ = "paper_fills"

    __table_args__ = (
        CheckConstraint(
            "fill_quantity > 0",
            name="ck_paper_fills_quantity_positive",
        ),
        CheckConstraint(
            "fill_price > 0",
            name="ck_paper_fills_price_positive",
        ),
        CheckConstraint(
            "total_charges >= 0",
            name="ck_paper_fills_charges_non_negative",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="RESTRICT"),
        nullable=False,
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    fill_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    slippage_bps: Mapped[Decimal] = mapped_column(
        Numeric(6, 2),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
    )

    # NSE charge breakdown — stored individually for ML attribution accuracy
    brokerage: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    stt: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    exchange_charges: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    sebi_charges: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    gst: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    stamp_duty: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, server_default=text("0")
    )
    total_charges: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)

    settlement_date: Mapped[date] = mapped_column(Date, nullable=False)

    # Hypertable partition key — no updated_at (append-only)
    executed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    order: Mapped["PaperOrder"] = relationship(
        "PaperOrder",
        back_populates="fills",
    )
    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio",
        foreign_keys=[portfolio_id],
    )

    @property
    def gross_amount(self) -> Decimal:
        return self.fill_price * self.fill_quantity

    def __repr__(self) -> str:
        return (
            f"<PaperFill(id={self.id}, symbol={self.symbol}, "
            f"qty={self.fill_quantity}, price={self.fill_price}, "
            f"charges={self.total_charges}, at={self.executed_at})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# PaperPosition
# ──────────────────────────────────────────────────────────────────────────────

class PaperPosition(Base):
    """
    Mutable live position state.

    Tracks a single open (or closed) position in a portfolio. At most one OPEN
    position per (portfolio_id, symbol) is enforced by the partial unique index
    uq_paper_positions_portfolio_symbol_open in the DB.

    avg_cost_price uses WAC (Weighted Average Cost) — the SEBI-mandated method
    for NSE equity delivery trades. The service layer recalculates it atomically
    within a DB transaction on every fill:
        new_avg = (old_qty * old_avg + fill_qty * fill_price) / (old_qty + fill_qty)

    last_price and unrealized_pnl are tick-cache values, not the source of truth.
    The P&L worker writes them here for snapshot queries; the authoritative live
    value lives in Redis (HSET positions:{portfolio_id} {symbol} ...).

    target_price_1/2/3 and stop_loss are denormalized from the source suggestion
    at position open time so they survive suggestion expiry while the position
    remains open.
    """

    __tablename__ = "paper_positions"

    __table_args__ = (
        CheckConstraint(
            "side IN ('LONG', 'SHORT')",
            name="ck_paper_positions_side",
        ),
        CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name="ck_paper_positions_status",
        ),
        CheckConstraint(
            "quantity >= 0",
            name="ck_paper_positions_quantity_non_negative",
        ),
        CheckConstraint(
            "avg_cost_price > 0",
            name="ck_paper_positions_cost_price_positive",
        ),
        # Partial unique index in migration:
        #   uq_paper_positions_portfolio_symbol_open
        #   ON paper_positions (portfolio_id, symbol) WHERE status = 'OPEN'
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )
    suggestion_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_suggestions.suggestion_id", ondelete="SET NULL"),
        nullable=True,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_cost_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)

    # Tick-updated cache — not the source of truth for realized P&L
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    unrealized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))

    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
    )
    total_charges: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
    )

    side: Mapped[str] = mapped_column(String(5), nullable=False)

    # Snapshot of suggestion targets at open time (survives suggestion expiry)
    target_price_1: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    target_price_2: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    target_price_3: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))

    status: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="OPEN",
        server_default=text("'OPEN'"),
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio",
        back_populates="positions",
    )
    suggestion: Mapped["TradeSuggestion | None"] = relationship(  # type: ignore[name-defined]
        "TradeSuggestion",
        back_populates="paper_positions",
    )
    outcome: Mapped["PaperTradeOutcome | None"] = relationship(
        "PaperTradeOutcome",
        back_populates="position",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="select",
    )

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"

    @property
    def net_pnl(self) -> Decimal:
        """Realized P&L after charges. Does not include unrealized."""
        return self.realized_pnl - self.total_charges

    def __repr__(self) -> str:
        return (
            f"<PaperPosition(id={self.id}, symbol={self.symbol}, "
            f"qty={self.quantity}, avg_cost={self.avg_cost_price}, "
            f"side={self.side}, status={self.status})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# PaperTradeOutcome
# ──────────────────────────────────────────────────────────────────────────────

class PaperTradeOutcome(Base):
    """
    Append-only audit record and ML feedback entry.

    Written exactly once when a PaperPosition is fully closed. Never updated
    or deleted — the absence of an updated_at column is intentional and signals
    this invariant to every reader of the schema.

    ML feedback fields (ml_direction_correct, hit_tp1/2/3, hit_sl) are computed
    by the outcome_service after close by comparing the actual post-exit price
    path against the original suggestion targets. They may be NULL briefly while
    the background computation is in flight.

    Snapshot columns (suggested_entry_price, suggested_tp1..3, etc.) are copied
    from TradeSuggestion at close time so the audit record is fully self-contained
    even after the source suggestion is expired or invalidated.

    The user_id column is denormalized (also reachable via portfolio → user) to
    allow fast single-table admin queries without joins.
    """

    __tablename__ = "paper_trade_outcomes"

    __table_args__ = (
        CheckConstraint(
            "signal_direction IN ('BUY', 'SELL')",
            name="ck_outcomes_signal_direction",
        ),
        CheckConstraint(
            "exit_reason IN ('TP1', 'TP2', 'TP3', 'SL', 'MANUAL', 'EXPIRED')",
            name="ck_outcomes_exit_reason",
        ),
        CheckConstraint(
            "suggestion_confidence_level IN ('HIGH', 'MEDIUM', 'LOW') "
            "OR suggestion_confidence_level IS NULL",
            name="ck_outcomes_confidence_level",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_outcomes_quantity_positive",
        ),
        CheckConstraint(
            "hold_duration_seconds >= 0",
            name="ck_outcomes_hold_duration_non_negative",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )
    position_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("paper_positions.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    suggestion_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("trade_suggestions.suggestion_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Denormalized — avoids join in admin/ML queries
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    signal_direction: Mapped[str] = mapped_column(String(4), nullable=False)

    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    total_charges: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    pnl_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    hold_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(10), nullable=False)

    # Snapshot of suggestion parameters at trade close (self-contained audit)
    suggested_entry_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    suggested_stop_loss: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    suggested_tp1: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    suggested_tp2: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    suggested_tp3: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    suggestion_consensus_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    suggestion_confidence_level: Mapped[str | None] = mapped_column(String(10))

    entry_slippage_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))

    # ML feedback — computed async after position close, may be NULL briefly
    ml_direction_correct: Mapped[bool | None] = mapped_column(Boolean)
    hit_tp1: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    hit_tp2: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    hit_tp3: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    hit_sl: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )

    market_regime_at_entry: Mapped[str | None] = mapped_column(String(50))

    # Immutable timestamp — no updated_at by design
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio",
        back_populates="outcomes",
    )
    position: Mapped["PaperPosition"] = relationship(
        "PaperPosition",
        back_populates="outcome",
    )
    suggestion: Mapped["TradeSuggestion | None"] = relationship(  # type: ignore[name-defined]
        "TradeSuggestion",
        back_populates="paper_outcomes",
    )
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="paper_trade_outcomes",
    )

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    def __repr__(self) -> str:
        return (
            f"<PaperTradeOutcome(id={self.id}, symbol={self.symbol}, "
            f"net_pnl={self.net_pnl}, exit_reason={self.exit_reason}, "
            f"ml_correct={self.ml_direction_correct})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# PaperPnlSnapshot
# ──────────────────────────────────────────────────────────────────────────────

class PaperPnlSnapshot(Base):
    """
    Daily EOD P&L snapshot.

    Append-only records captured once per portfolio per trading day at market
    close (15:30 IST). The unique constraint uq_pnl_snapshots_portfolio_date
    ensures exactly one snapshot per day.

    nifty_close is stored for benchmark comparison — allows the dashboard to
    show portfolio alpha against the Nifty 50 index.

    This table is a TimescaleDB hypertable partitioned by captured_at when
    TimescaleDB is available; otherwise a standard PostgreSQL table.
    """

    __tablename__ = "paper_pnl_snapshots"

    __table_args__ = (
        UniqueConstraint(
            "portfolio_id",
            "snapshot_date",
            name="uq_pnl_snapshots_portfolio_date",
        ),
        CheckConstraint(
            "num_open_positions >= 0",
            name="ck_pnl_snapshots_open_positions_non_negative",
        ),
        CheckConstraint(
            "num_trades_today >= 0",
            name="ck_pnl_snapshots_trades_today_non_negative",
        ),
        CheckConstraint(
            "win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)",
            name="ck_pnl_snapshots_win_rate_range",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True
    )
    portfolio_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("portfolios.id", ondelete="CASCADE"),
        nullable=False,
    )

    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_realized_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    total_unrealized_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    portfolio_value: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    cash_balance: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)

    num_open_positions: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    num_trades_today: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    win_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    nifty_close: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))

    # Hypertable partition key — no updated_at (append-only)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    portfolio: Mapped["Portfolio"] = relationship(
        "Portfolio",
        back_populates="pnl_snapshots",
    )

    def __repr__(self) -> str:
        return (
            f"<PaperPnlSnapshot(portfolio={self.portfolio_id}, "
            f"date={self.snapshot_date}, value={self.portfolio_value}, "
            f"realized={self.total_realized_pnl})>"
        )
