# PATH: backend/app/models/strategies.py
"""
Trading Strategies ORM Models
==============================
SQLAlchemy 2.0 declarative models for the Trading Strategies rule engine.

Model hierarchy:
  User
  ├─ UserPreferences           (one per user — enforcement mode + sizing defaults)
  ├─ UserStrategySubscription  (N active subscriptions, ordered by priority)
  │  └─ Strategy               (authored by user or promoted to global library)
  ├─ StrategyActivation        (lifecycle of a single signal/strategy match)
  │  └─ StrategyTrade          (written on FSM exit — immutable audit record)
  └─ StrategyBacktestRun       (async backtest task — append-only result store)

Invariants enforced here and in the service layer:
  - All monetary columns use Decimal / NUMERIC — never float
  - strategy_snapshot JSONB is frozen at activation time; mutations to the
    parent Strategy never retroactively alter historical activations
  - StrategyTrade and StrategyBacktestRun rows are append-only (no updated_at)
  - At most one active subscription per (user_id, strategy_id) is enforced by
    the partial unique index uq_subs_user_strategy_active in the migration
  - No two active subscriptions for the same user may share the same priority,
    enforced by uq_subs_user_priority_active partial unique index
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


# ──────────────────────────────────────────────────────────────────────────────
# UserPreferences
# ──────────────────────────────────────────────────────────────────────────────

class UserPreferences(Base):
    """
    Per-user trading strategy enforcement preferences.

    Exactly one row per user — upserted by the preferences service, never
    created directly. The enforcement_mode column drives the StrategyDispatcher:

      hard_gate      — strategy-approved signals only; pass-through with banner
                       if the user has no active subscriptions
      soft_filter    — all signals surface; strategy matches are visually tagged
      portfolio_opt_in — enforcement is scoped per-portfolio rather than global

    hard_gate_notify controls whether a persistent UI banner is shown when
    hard_gate is active but no strategies are subscribed.

    default_position_sizing_mode and default_risk_per_trade_pct are per-user
    defaults that pre-fill the sizing step in the strategy builder wizard.
    Strategy-level overrides take precedence when set.
    """

    __tablename__ = "user_preferences"

    __table_args__ = (
        CheckConstraint(
            "enforcement_mode IN ('hard_gate', 'soft_filter', 'portfolio_opt_in')",
            name="ck_user_prefs_enforcement_mode",
        ),
        CheckConstraint(
            "default_position_sizing_mode IN ('fixed_qty', 'risk_pct', 'kelly')",
            name="ck_user_prefs_sizing_mode",
        ),
        CheckConstraint(
            "default_risk_per_trade_pct IS NULL "
            "OR (default_risk_per_trade_pct > 0 AND default_risk_per_trade_pct <= 10)",
            name="ck_user_prefs_risk_pct_range",
        ),
        CheckConstraint(
            "max_concurrent_activations >= 1 AND max_concurrent_activations <= 50",
            name="ck_user_prefs_max_activations",
        ),
        # uq_user_prefs_user_id: one row per user — enforced in the migration
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    enforcement_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="soft_filter",
        server_default=text("'soft_filter'"),
    )
    hard_gate_notify: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    default_position_sizing_mode: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        default="risk_pct",
        server_default=text("'risk_pct'"),
    )
    default_risk_per_trade_pct: Mapped[Decimal | None] = mapped_column(Numeric(4, 2))

    max_concurrent_activations: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=10,
        server_default=text("10"),
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
        back_populates="preferences",
    )

    @property
    def is_hard_gate(self) -> bool:
        return self.enforcement_mode == "hard_gate"

    def __repr__(self) -> str:
        return (
            f"<UserPreferences(user_id={self.user_id}, "
            f"mode={self.enforcement_mode})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Strategy
# ──────────────────────────────────────────────────────────────────────────────

class Strategy(Base):
    """
    User-authored trading strategy definition.

    A strategy encodes a set of declarative rules — stored as a recursive JSONB
    condition tree — that must be satisfied before a signal qualifies for the
    user's portfolio. The definition column is GIN-indexed in the migration for
    fast JSONB path operators (@>, ?, ?|).

    Versioning: every PUT to an active strategy increments version. Activations
    store a frozen strategy_snapshot so historical attribution is never corrupted
    by in-place edits.

    Scope lifecycle:
      private  → user-owned, only visible to the creator
      shared   → admin has promoted it to the global library
      archived → soft-deleted; retains all historical activations

    Performance metrics (avg_daily_return_pct, win_rate_pct, total_trades) are
    denormalized aggregates refreshed by the strategy_daily_performance
    continuous aggregate worker. They are approximate at any given moment but
    are never the source of truth for per-trade P&L.
    """

    __tablename__ = "strategies"

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name="ck_strategies_status",
        ),
        CheckConstraint(
            "scope IN ('private', 'shared')",
            name="ck_strategies_scope",
        ),
        CheckConstraint(
            "version >= 1",
            name="ck_strategies_version_positive",
        ),
        CheckConstraint(
            "win_rate_pct IS NULL OR (win_rate_pct >= 0 AND win_rate_pct <= 100)",
            name="ck_strategies_win_rate_range",
        ),
        CheckConstraint(
            "total_trades >= 0",
            name="ck_strategies_total_trades_non_negative",
        ),
        CheckConstraint(
            "total_subscribers >= 0",
            name="ck_strategies_subscribers_non_negative",
        ),
        # GIN index on definition — in migration: idx_strategies_definition_gin
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    created_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)))

    scope: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="private",
        server_default=text("'private'"),
    )
    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="draft",
        server_default=text("'draft'"),
    )
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default=text("1"),
    )

    # Full strategy definition — recursive ConditionGroup / ConditionLeaf tree
    # plus stop-loss, take-profit, position sizing, and guardrail configs.
    # GIN indexed for fast @> / ? queries.
    definition: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    # Denormalized performance cache — refreshed by continuous aggregate worker
    total_subscribers: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )
    avg_daily_return_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    win_rate_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    total_trades: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
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
    creator: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="strategies",
        foreign_keys=[created_by],
    )
    subscriptions: Mapped[list["UserStrategySubscription"]] = relationship(
        "UserStrategySubscription",
        back_populates="strategy",
        cascade="all, delete-orphan",
        lazy="select",
    )
    activations: Mapped[list["StrategyActivation"]] = relationship(
        "StrategyActivation",
        back_populates="strategy",
        cascade="all, delete-orphan",
        order_by="StrategyActivation.created_at.desc()",
        lazy="select",
    )
    trades: Mapped[list["StrategyTrade"]] = relationship(
        "StrategyTrade",
        back_populates="strategy",
        cascade="all, delete-orphan",
        order_by="StrategyTrade.entry_at.desc()",
        lazy="select",
    )
    backtest_runs: Mapped[list["StrategyBacktestRun"]] = relationship(
        "StrategyBacktestRun",
        back_populates="strategy",
        cascade="all, delete-orphan",
        order_by="StrategyBacktestRun.created_at.desc()",
        lazy="select",
    )

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_public(self) -> bool:
        return self.scope == "shared"

    def __repr__(self) -> str:
        return (
            f"<Strategy(id={self.id}, name={self.name!r}, "
            f"scope={self.scope}, status={self.status}, v={self.version})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# UserStrategySubscription
# ──────────────────────────────────────────────────────────────────────────────

class UserStrategySubscription(Base):
    """
    A user's active subscription to a strategy.

    Priority controls evaluation order inside StrategyDispatcher. Lower
    integer = higher priority (1 evaluated before 2 before 3 …). The
    migration enforces a partial unique index so no two active subscriptions
    for the same user can share the same priority number:
      uq_subs_user_priority_active ON (user_id, priority) WHERE is_active = true

    A second partial unique index prevents the same user from subscribing to
    the same strategy twice simultaneously:
      uq_subs_user_strategy_active ON (user_id, strategy_id) WHERE is_active = true

    Historical subscriptions (is_active = false) are retained for audit and
    performance attribution. unsubscribed_at records the deactivation moment.
    """

    __tablename__ = "user_strategy_subscriptions"

    __table_args__ = (
        CheckConstraint(
            "priority >= 1",
            name="ck_subs_priority_positive",
        ),
        # Partial unique indexes expressed in migration only — cannot be
        # represented as UniqueConstraint with a WHERE clause in SQLAlchemy.
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )

    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )

    notes: Mapped[str | None] = mapped_column(Text)

    subscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relationships ──────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="strategy_subscriptions",
    )
    strategy: Mapped["Strategy"] = relationship(
        "Strategy",
        back_populates="subscriptions",
    )

    def __repr__(self) -> str:
        return (
            f"<UserStrategySubscription(user={self.user_id}, "
            f"strategy={self.strategy_id}, priority={self.priority}, "
            f"active={self.is_active})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# StrategyActivation
# ──────────────────────────────────────────────────────────────────────────────

class StrategyActivation(Base):
    """
    Lifecycle record for a single strategy/signal match.

    Created by StrategyDispatcher when a signal passes all 7 pipeline gates
    for a given user+strategy. The FSM transitions are:

      WATCHING → IN_SETUP → IN_TRADE → PARTIAL_EXIT → EXITED

    strategy_snapshot is a frozen JSON copy of the strategy definition at
    the moment of activation. It is the authoritative source of truth for
    SL/TP thresholds and condition rules for this specific activation; edits
    to the parent Strategy never mutate it.

    state_history is an append-only JSONB array recording every FSM
    transition with timestamps:
      [{"state": "WATCHING", "at": "2026-05-01T09:15:00Z"}, ...]

    auto_execute, when true, triggers PaperOrderService to place an order
    automatically on IN_TRADE entry without requiring user confirmation.

    entry_order_id and exit_order_id link back to paper_orders (UUID) when
    auto-execute fires. They are NULL for manual / soft_filter activations
    that only produce tagged suggestions.
    """

    __tablename__ = "strategy_activations"

    __table_args__ = (
        CheckConstraint(
            "state IN ('WATCHING', 'IN_SETUP', 'IN_TRADE', "
            "'PARTIAL_EXIT', 'EXITED', 'CANCELLED')",
            name="ck_activations_state",
        ),
        CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN "
            "('TP1', 'TP2', 'TP3', 'FULL_TP', 'SL', 'MANUAL', "
            "'SIGNAL_EXPIRED', 'STRATEGY_PAUSED', 'RULE_VIOLATED')",
            name="ck_activations_exit_reason",
        ),
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
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Immutable snapshot of strategy.definition at activation time
    strategy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
    )

    # FK to ai_trading_signals.id (BigInteger) — the originating signal
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("ai_trading_signals.id", ondelete="SET NULL"),
        nullable=True,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    state: Mapped[str] = mapped_column(
        String(15),
        nullable=False,
        default="WATCHING",
        server_default=text("'WATCHING'"),
    )

    # Append-only FSM transition log: [{"state": "...", "at": "..."}]
    state_history: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
        default=list,
        server_default=text("'[]'::jsonb"),
    )

    auto_execute: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )

    # paper_orders.id (UUID) — set when auto_execute fires
    entry_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    exit_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="SET NULL"),
        nullable=True,
    )

    exit_reason: Mapped[str | None] = mapped_column(String(20))

    entry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
        back_populates="strategy_activations",
    )
    strategy: Mapped["Strategy"] = relationship(
        "Strategy",
        back_populates="activations",
    )
    entry_order: Mapped["PaperOrder | None"] = relationship(  # type: ignore[name-defined]
        "PaperOrder",
        foreign_keys=[entry_order_id],
        lazy="select",
    )
    exit_order: Mapped["PaperOrder | None"] = relationship(  # type: ignore[name-defined]
        "PaperOrder",
        foreign_keys=[exit_order_id],
        lazy="select",
    )
    trade: Mapped["StrategyTrade | None"] = relationship(
        "StrategyTrade",
        back_populates="activation",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="select",
    )

    @property
    def is_terminal(self) -> bool:
        return self.state in ("EXITED", "CANCELLED")

    @property
    def is_in_trade(self) -> bool:
        return self.state in ("IN_TRADE", "PARTIAL_EXIT")

    def __repr__(self) -> str:
        return (
            f"<StrategyActivation(id={self.id}, user={self.user_id}, "
            f"symbol={self.symbol}, state={self.state})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# StrategyTrade
# ──────────────────────────────────────────────────────────────────────────────

class StrategyTrade(Base):
    """
    Immutable audit record for a completed strategy activation trade.

    Written exactly once when a StrategyActivation FSM transitions to EXITED
    or CANCELLED (with a partial fill). Never updated or deleted — the absence
    of an updated_at column is intentional and signals this invariant.

    This table is a TimescaleDB hypertable partitioned by entry_at (7-day
    chunks, compressed after 7 days, segmented by strategy_id + user_id +
    symbol). The strategy_daily_performance continuous aggregate is built on
    top of it.

    strategy_version is denormalized from the parent Strategy at write time
    for performance attribution without joining back through the snapshot.

    hold_duration_seconds is pre-computed so the performance aggregate can
    avoid expensive per-row timestamp arithmetic.
    """

    __tablename__ = "strategy_trades"

    __table_args__ = (
        CheckConstraint(
            "direction IN ('LONG', 'SHORT')",
            name="ck_strategy_trades_direction",
        ),
        CheckConstraint(
            "exit_reason IN ('TP1', 'TP2', 'TP3', 'FULL_TP', 'SL', "
            "'MANUAL', 'SIGNAL_EXPIRED', 'STRATEGY_PAUSED', 'RULE_VIOLATED')",
            name="ck_strategy_trades_exit_reason",
        ),
        CheckConstraint(
            "quantity > 0",
            name="ck_strategy_trades_quantity_positive",
        ),
        CheckConstraint(
            "entry_price > 0",
            name="ck_strategy_trades_entry_price_positive",
        ),
        CheckConstraint(
            "exit_price > 0",
            name="ck_strategy_trades_exit_price_positive",
        ),
        CheckConstraint(
            "hold_duration_seconds >= 0",
            name="ck_strategy_trades_hold_duration_non_negative",
        ),
        CheckConstraint(
            "strategy_version >= 1",
            name="ck_strategy_trades_version_positive",
        ),
        # Hypertable partition key: entry_at — TimescaleDB DO block in migration
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
        server_default=text("gen_random_uuid()"),
    )
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    activation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategy_activations.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )

    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    direction: Mapped[str] = mapped_column(String(5), nullable=False)

    entry_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(12, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    total_charges: Mapped[Decimal] = mapped_column(
        Numeric(10, 4),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
    )
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    pnl_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)

    exit_reason: Mapped[str] = mapped_column(String(20), nullable=False)
    strategy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    hold_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)

    # Hypertable partition key — no updated_at (append-only)
    entry_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    exit_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    # created_at is distinct from entry_at: a trade row may be written
    # slightly after the fill occurs (async FSM flush)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    strategy: Mapped["Strategy"] = relationship(
        "Strategy",
        back_populates="trades",
    )
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="strategy_trades",
    )
    activation: Mapped["StrategyActivation"] = relationship(
        "StrategyActivation",
        back_populates="trade",
    )

    @property
    def is_winner(self) -> bool:
        return self.net_pnl > 0

    @property
    def r_multiple(self) -> Decimal | None:
        """Net P&L expressed as a multiple of risk (entry − stop_loss × qty)."""
        snapshot = self.activation.strategy_snapshot if self.activation else None
        if snapshot is None:
            return None
        risk_cfg = snapshot.get("stop_loss", {})
        stop_pct = risk_cfg.get("value")
        if stop_pct is None:
            return None
        risk_per_unit = self.entry_price * Decimal(str(stop_pct)) / Decimal("100")
        total_risk = risk_per_unit * self.quantity
        if total_risk == 0:
            return None
        return self.net_pnl / total_risk

    def __repr__(self) -> str:
        return (
            f"<StrategyTrade(id={self.id}, symbol={self.symbol}, "
            f"net_pnl={self.net_pnl}, exit={self.exit_reason})>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# StrategyBacktestRun
# ──────────────────────────────────────────────────────────────────────────────

class StrategyBacktestRun(Base):
    """
    Async backtest task record.

    Append-only status store for a background backtest job. The task runner
    writes progress updates (progress_pct, status) and, on completion, stores
    the full result_metrics JSONB blob. Error details land in error_detail.

    Modes:
      signal_replay  — replay historical ai_trading_signals rows through the
                       strategy rule engine; fast, bounded by signal history
      model_replay   — re-run EnsemblePredictor on historical OHLCV; slower,
                       can reach further back in time
      hybrid         — auto-splice: signal_replay up to the signal history
                       boundary, model_replay for deeper history

    symbols is a PostgreSQL text array — a run may cover multiple symbols.

    strategy_snapshot is frozen at task creation time so the backtest is always
    reproducible against the exact rule set that was active when it was queued,
    even if the user edits the strategy before the job finishes.
    """

    __tablename__ = "strategy_backtest_runs"

    __table_args__ = (
        CheckConstraint(
            "mode IN ('signal_replay', 'model_replay', 'hybrid')",
            name="ck_backtest_runs_mode",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="ck_backtest_runs_status",
        ),
        CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_backtest_runs_progress_range",
        ),
        CheckConstraint(
            "start_dt < end_dt",
            name="ck_backtest_runs_date_range",
        ),
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
    strategy_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Frozen strategy definition at queue time — guarantees reproducibility
    strategy_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB(astext_type=Text()),
        nullable=False,
    )

    mode: Mapped[str] = mapped_column(String(15), nullable=False)
    symbols: Mapped[list[str]] = mapped_column(ARRAY(String(50)), nullable=False)

    start_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_dt: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="pending",
        server_default=text("'pending'"),
    )
    progress_pct: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default=text("0"),
    )

    # Full result payload — written atomically on status → 'completed'
    result_metrics: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(astext_type=Text())
    )
    error_detail: Mapped[str | None] = mapped_column(Text)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Immutable enqueue timestamp — no updated_at (append-only result store)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    # ── Relationships ──────────────────────────────────────────────────────────
    user: Mapped["User"] = relationship(  # type: ignore[name-defined]
        "User",
        back_populates="backtest_runs",
    )
    strategy: Mapped["Strategy"] = relationship(
        "Strategy",
        back_populates="backtest_runs",
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "cancelled")

    @property
    def duration_seconds(self) -> int | None:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds())
        return None

    def __repr__(self) -> str:
        return (
            f"<StrategyBacktestRun(id={self.id}, strategy={self.strategy_id}, "
            f"mode={self.mode}, status={self.status}, progress={self.progress_pct}%)>"
        )


# ──────────────────────────────────────────────────────────────────────────────
# StrategyAuditLog
# ──────────────────────────────────────────────────────────────────────────────

class StrategyAuditLog(Base):
    """
    Immutable append-only audit record for admin scope changes (promote/demote).

    One row is written for every promote or demote action.  Rows are never
    updated or deleted — they form a tamper-evident chain of custody.

    strategy_id and admin_user_id use SET NULL on FK cascade so the audit log
    survives strategy or user deletion with the denormalised strategy_name as
    a readable reference.
    """

    __tablename__ = "strategy_audit_logs"

    __table_args__ = (
        CheckConstraint(
            "action IN ('promote', 'demote')",
            name="ck_strategy_audit_action",
        ),
        CheckConstraint(
            "scope_before IN ('private', 'shared')",
            name="ck_strategy_audit_scope_before",
        ),
        CheckConstraint(
            "scope_after IN ('private', 'shared')",
            name="ck_strategy_audit_scope_after",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    strategy_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("strategies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    admin_user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    action: Mapped[str] = mapped_column(String(20), nullable=False)        # "promote" | "demote"
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    scope_before: Mapped[str] = mapped_column(String(10), nullable=False)  # "private" | "shared"
    scope_after: Mapped[str] = mapped_column(String(10), nullable=False)   # "private" | "shared"
    strategy_name: Mapped[str] = mapped_column(String(100), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        index=True,
    )

    def __repr__(self) -> str:
        return (
            f"<StrategyAuditLog(id={self.id}, action={self.action!r}, "
            f"strategy={self.strategy_id}, admin={self.admin_user_id})>"
        )
