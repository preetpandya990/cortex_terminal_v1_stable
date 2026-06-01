"""Rebuild all strategy tables to match Phase 3-6 ORM models.

Revision ID: 0021
Revises: 0020
Create Date: 2026-05-06

Migration 0016 created all strategy tables against an earlier schema design.
By Phase 3-6 all five tables had diverged significantly from the ORM models
that the services, FSM, dispatcher, and API layers were written against. All
tables are empty, so drop-and-recreate is safe and cleaner than ALTER chains.

Changes per table
─────────────────
user_preferences
  id           : uuid → BIGSERIAL (autoincrement)
  added        : hard_gate_notify, default_position_sizing_mode,
                 default_risk_per_trade_pct, max_concurrent_activations

strategies
  removed      : promoted_by, promoted_at  (captured in strategy_audit_logs)
  added        : tags (TEXT[]), total_subscribers, avg_daily_return_pct,
                 win_rate_pct, total_trades
  scope values : 'personal'/'global' → 'private'/'shared'
  added checks : win_rate_range, total_trades_non_negative,
                 subscribers_non_negative

user_strategy_subscriptions
  id           : uuid → BIGSERIAL (autoincrement)
  removed      : strategy_version, portfolio_id, auto_execute
  added        : notes, subscribed_at (≈ created_at), unsubscribed_at

strategy_trades
  removed      : subscription_id, side, stop_loss_price, signal_confidence,
                 regime_at_entry, holding_bars, metadata
  added        : direction (LONG/SHORT), strategy_version, hold_duration_seconds,
                 created_at
  activation_id FK: CASCADE → RESTRICT (trade row prevents activation deletion)
  exit_reason  : lowercase values → uppercase (TP1/TP2/SL/MANUAL/…)

strategy_backtest_runs
  requested_by → user_id
  removed      : strategy_version, win_rate, sharpe_ratio, sortino_ratio,
                 calmar_ratio, max_drawdown_pct, total_return_pct, avg_hold_bars,
                 error_message, results_summary
  added        : strategy_snapshot, result_metrics (JSONB), error_detail
  status       : 'queued' → 'pending' (default)
  progress_pct : NUMERIC → INTEGER
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ──────────────────────────────────────────────────────────────────────────────
# UPGRADE
# ──────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. Drop inbound FK constraints from tables NOT being rebuilt ──────────
    # These point INTO strategies; must be removed before dropping strategies.
    op.drop_constraint(
        "strategy_audit_logs_strategy_id_fkey",
        "strategy_audit_logs", type_="foreignkey",
    )
    op.drop_constraint(
        "trade_suggestions_strategy_id_fkey",
        "trade_suggestions", type_="foreignkey",
    )
    op.drop_constraint(
        "fk_activations_strategy_id",
        "strategy_activations", type_="foreignkey",
    )

    # ── 2. Drop tables in dependency order ────────────────────────────────────
    # strategy_trades first: it references strategy_activations and will be rebuilt
    op.drop_table("strategy_trades")
    op.drop_table("strategy_backtest_runs")
    op.drop_table("user_strategy_subscriptions")
    op.drop_table("strategies")
    op.drop_table("user_preferences")

    # ── 3. user_preferences — matches UserPreferences ORM ────────────────────
    op.create_table(
        "user_preferences",

        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_preferences_user_id"),
            nullable=False,
            unique=True,
        ),

        sa.Column(
            "enforcement_mode",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'soft_filter'"),
        ),
        sa.Column(
            "hard_gate_notify",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "default_position_sizing_mode",
            sa.String(15),
            nullable=False,
            server_default=sa.text("'risk_pct'"),
        ),
        sa.Column("default_risk_per_trade_pct", sa.Numeric(4, 2), nullable=True),
        sa.Column(
            "max_concurrent_activations",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),

        sa.CheckConstraint(
            "enforcement_mode IN ('hard_gate','soft_filter','portfolio_opt_in')",
            name="ck_user_prefs_enforcement_mode",
        ),
        sa.CheckConstraint(
            "default_position_sizing_mode IN ('fixed_qty','risk_pct','kelly')",
            name="ck_user_prefs_sizing_mode",
        ),
        sa.CheckConstraint(
            "default_risk_per_trade_pct IS NULL "
            "OR (default_risk_per_trade_pct > 0 AND default_risk_per_trade_pct <= 10)",
            name="ck_user_prefs_risk_pct_range",
        ),
        sa.CheckConstraint(
            "max_concurrent_activations >= 1 AND max_concurrent_activations <= 50",
            name="ck_user_prefs_max_activations",
        ),
    )
    op.create_index("idx_user_prefs_user_id", "user_preferences", ["user_id"])

    # ── 4. strategies — matches Strategy ORM ─────────────────────────────────
    op.create_table(
        "strategies",

        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_strategies_created_by"),
            nullable=False,
        ),

        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("tags", pg.ARRAY(sa.String(50)), nullable=True),

        sa.Column(
            "scope",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'private'"),
        ),
        sa.Column(
            "status",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),

        # Full recursive condition tree + SL/TP/sizing/guardrails — GIN indexed
        sa.Column(
            "definition",
            pg.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),

        # Denormalized performance cache — refreshed by background worker
        sa.Column(
            "total_subscribers",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("avg_daily_return_pct", sa.Numeric(8, 4), nullable=True),
        sa.Column("win_rate_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column(
            "total_trades",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),

        sa.CheckConstraint(
            "status IN ('draft','active','paused','archived')",
            name="ck_strategies_status",
        ),
        sa.CheckConstraint(
            "scope IN ('private','shared')",
            name="ck_strategies_scope",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_strategies_version_positive",
        ),
        sa.CheckConstraint(
            "win_rate_pct IS NULL OR (win_rate_pct >= 0 AND win_rate_pct <= 100)",
            name="ck_strategies_win_rate_range",
        ),
        sa.CheckConstraint(
            "total_trades >= 0",
            name="ck_strategies_total_trades_non_negative",
        ),
        sa.CheckConstraint(
            "total_subscribers >= 0",
            name="ck_strategies_subscribers_non_negative",
        ),
    )
    op.create_index("idx_strategies_created_by_status", "strategies", ["created_by", "status"])
    op.create_index(
        "idx_strategies_scope_status",
        "strategies",
        ["scope", "status"],
        postgresql_where=sa.text("status != 'archived'"),
    )
    # GIN index enables fast @> / ? JSONB path queries on definition
    op.create_index(
        "idx_strategies_definition_gin",
        "strategies",
        ["definition"],
        postgresql_using="gin",
    )

    # ── 5. user_strategy_subscriptions — matches UserStrategySubscription ORM ─
    op.create_table(
        "user_strategy_subscriptions",

        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_subs_user_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE", name="fk_subs_strategy_id"),
            nullable=False,
        ),

        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),

        # subscribed_at is the effective subscription start timestamp
        sa.Column(
            "subscribed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("unsubscribed_at", sa.DateTime(timezone=True), nullable=True),

        sa.CheckConstraint("priority >= 1", name="ck_subs_priority_positive"),
    )
    # Dispatcher loads subscriptions by (user_id, priority ASC) WHERE is_active
    op.create_index(
        "idx_subs_user_id_active",
        "user_strategy_subscriptions",
        ["user_id", "priority"],
        postgresql_where=sa.text("is_active = true"),
    )
    op.create_index(
        "idx_subs_strategy_id_active",
        "user_strategy_subscriptions",
        ["strategy_id"],
        postgresql_where=sa.text("is_active = true"),
    )
    # Prevent two active subscriptions sharing the same priority for one user
    op.create_index(
        "uq_subs_user_priority_active",
        "user_strategy_subscriptions",
        ["user_id", "priority"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )
    # Prevent duplicate active subscriptions for the same user+strategy
    op.create_index(
        "uq_subs_user_strategy_active",
        "user_strategy_subscriptions",
        ["user_id", "strategy_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
    )

    # ── 6. strategy_trades — matches StrategyTrade ORM ───────────────────────
    op.create_table(
        "strategy_trades",

        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE",
                          name="fk_strategy_trades_strategy_id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE",
                          name="fk_strategy_trades_user_id"),
            nullable=False,
        ),
        # RESTRICT: a trade record pins its activation; activation cannot be
        # deleted while a trade row references it.
        sa.Column(
            "activation_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategy_activations.id", ondelete="RESTRICT",
                          name="fk_strategy_trades_activation_id"),
            nullable=False,
            unique=True,
        ),

        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("direction", sa.String(5), nullable=False),   # LONG | SHORT

        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),

        sa.Column("gross_pnl", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "total_charges",
            sa.Numeric(10, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("net_pnl", sa.Numeric(14, 4), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=False),

        sa.Column("exit_reason", sa.String(20), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("hold_duration_seconds", sa.Integer(), nullable=False),

        # TimescaleDB hypertable partition key
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),

        sa.CheckConstraint(
            "direction IN ('LONG','SHORT')",
            name="ck_strategy_trades_direction",
        ),
        sa.CheckConstraint(
            "exit_reason IN ('TP1','TP2','TP3','FULL_TP','SL','MANUAL',"
            "'SIGNAL_EXPIRED','STRATEGY_PAUSED','RULE_VIOLATED')",
            name="ck_strategy_trades_exit_reason",
        ),
        sa.CheckConstraint("quantity > 0", name="ck_strategy_trades_quantity_positive"),
        sa.CheckConstraint("entry_price > 0", name="ck_strategy_trades_entry_price_positive"),
        sa.CheckConstraint("exit_price > 0", name="ck_strategy_trades_exit_price_positive"),
        sa.CheckConstraint(
            "hold_duration_seconds >= 0",
            name="ck_strategy_trades_hold_duration_non_negative",
        ),
        sa.CheckConstraint(
            "strategy_version >= 1",
            name="ck_strategy_trades_version_positive",
        ),
    )
    op.create_index(
        "idx_strategy_trades_strategy_entry",
        "strategy_trades",
        ["strategy_id", sa.text("entry_at DESC")],
    )
    op.create_index(
        "idx_strategy_trades_user_entry",
        "strategy_trades",
        ["user_id", sa.text("entry_at DESC")],
    )
    op.create_index(
        "idx_strategy_trades_symbol_entry",
        "strategy_trades",
        ["symbol", sa.text("entry_at DESC")],
    )

    # Convert to TimescaleDB hypertable (graceful degradation on plain PG)
    op.execute(sa.text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'
            ) THEN
                PERFORM create_hypertable(
                    'strategy_trades', 'entry_at',
                    chunk_time_interval => INTERVAL '1 month',
                    if_not_exists => TRUE
                );
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'TimescaleDB hypertable creation skipped: %', SQLERRM;
        END;
        $$;
    """))

    # ── 7. strategy_backtest_runs — matches StrategyBacktestRun ORM ──────────
    op.create_table(
        "strategy_backtest_runs",

        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE",
                          name="fk_backtest_runs_user_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE",
                          name="fk_backtest_runs_strategy_id"),
            nullable=False,
        ),

        # Frozen strategy definition at queue time — guarantees reproducibility
        sa.Column(
            "strategy_snapshot",
            pg.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),

        sa.Column("mode", sa.String(15), nullable=False),
        sa.Column("symbols", pg.ARRAY(sa.String(50)), nullable=False),
        sa.Column("start_dt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_dt", sa.DateTime(timezone=True), nullable=False),

        sa.Column(
            "status",
            sa.String(10),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "progress_pct",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),

        # Written atomically when status → 'completed'
        sa.Column("result_metrics", pg.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),

        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),

        # Immutable enqueue timestamp — no updated_at (append-only result store)
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),

        sa.CheckConstraint(
            "mode IN ('signal_replay','model_replay','hybrid')",
            name="ck_backtest_runs_mode",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_backtest_runs_status",
        ),
        sa.CheckConstraint(
            "progress_pct >= 0 AND progress_pct <= 100",
            name="ck_backtest_runs_progress_range",
        ),
        sa.CheckConstraint(
            "start_dt < end_dt",
            name="ck_backtest_runs_date_range",
        ),
    )
    op.create_index(
        "idx_backtest_runs_strategy_created",
        "strategy_backtest_runs",
        ["strategy_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_backtest_runs_user_created",
        "strategy_backtest_runs",
        ["user_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_backtest_runs_pending",
        "strategy_backtest_runs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('pending','running')"),
    )

    # ── 8. Restore inbound FK constraints from tables NOT rebuilt ─────────────
    op.create_foreign_key(
        "fk_activations_strategy_id",
        "strategy_activations", "strategies",
        ["strategy_id"], ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "strategy_audit_logs_strategy_id_fkey",
        "strategy_audit_logs", "strategies",
        ["strategy_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "trade_suggestions_strategy_id_fkey",
        "trade_suggestions", "strategies",
        ["strategy_id"], ["id"],
        ondelete="SET NULL",
    )


# ──────────────────────────────────────────────────────────────────────────────
# DOWNGRADE
# ──────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    # Drop inbound FKs
    op.drop_constraint("fk_activations_strategy_id", "strategy_activations", type_="foreignkey")
    op.drop_constraint("strategy_audit_logs_strategy_id_fkey", "strategy_audit_logs", type_="foreignkey")
    op.drop_constraint("trade_suggestions_strategy_id_fkey", "trade_suggestions", type_="foreignkey")

    # Drop rebuilt tables
    op.drop_table("strategy_backtest_runs")
    op.drop_table("strategy_trades")
    op.drop_table("user_strategy_subscriptions")
    op.drop_table("strategies")
    op.drop_table("user_preferences")

    # Restore 0016-era schemas (abbreviated — sufficient for rollback continuity)
    op.create_table(
        "user_preferences",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_user_preferences_user_id"), nullable=False),
        sa.Column("enforcement_mode", sa.String(20), nullable=False, server_default=sa.text("'soft_filter'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("user_id", name="uq_user_preferences_user_id"),
        sa.CheckConstraint("enforcement_mode IN ('hard_gate','soft_filter','portfolio_opt_in')", name="ck_user_preferences_enforcement_mode"),
    )
    op.create_table(
        "strategies",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'draft'")),
        sa.Column("scope", sa.String(20), nullable=False, server_default=sa.text("'personal'")),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_strategies_created_by"), nullable=False),
        sa.Column("promoted_by", sa.Integer(), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("definition", pg.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('draft','active','paused','archived')", name="ck_strategies_status"),
        sa.CheckConstraint("scope IN ('personal','global')", name="ck_strategies_scope"),
        sa.CheckConstraint("version >= 1", name="ck_strategies_version_positive"),
    )
    op.create_table(
        "user_strategy_subscriptions",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_subs_user_id"), nullable=False),
        sa.Column("strategy_id", pg.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE", name="fk_subs_strategy_id"), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default=sa.text("100")),
        sa.Column("portfolio_id", pg.UUID(as_uuid=True), nullable=True),
        sa.Column("auto_execute", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.CheckConstraint("priority >= 1", name="ck_subs_priority_positive"),
    )
    op.create_table(
        "strategy_trades",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("activation_id", pg.UUID(as_uuid=True), sa.ForeignKey("strategy_activations.id", ondelete="CASCADE", name="fk_strategy_trades_activation_id"), nullable=False),
        sa.Column("subscription_id", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("strategy_id", pg.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE", name="fk_strategy_trades_strategy_id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_strategy_trades_user_id"), nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("side", sa.String(5), nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("exit_price", sa.Numeric(12, 4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(14, 4), nullable=False),
        sa.Column("total_charges", sa.Numeric(10, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("net_pnl", sa.Numeric(14, 4), nullable=False),
        sa.Column("pnl_pct", sa.Numeric(8, 4), nullable=False),
        sa.Column("exit_reason", sa.String(30), nullable=False),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "strategy_backtest_runs",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("strategy_id", pg.UUID(as_uuid=True), sa.ForeignKey("strategies.id", ondelete="CASCADE", name="fk_backtest_runs_strategy_id"), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_backtest_runs_requested_by"), nullable=False),
        sa.Column("mode", sa.String(20), nullable=False),
        sa.Column("symbols", pg.ARRAY(sa.Text()), nullable=False),
        sa.Column("start_dt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_dt", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("progress_pct", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )

    # Restore inbound FKs
    op.create_foreign_key("fk_activations_strategy_id", "strategy_activations", "strategies", ["strategy_id"], ["id"], ondelete="CASCADE")
    op.create_foreign_key("strategy_audit_logs_strategy_id_fkey", "strategy_audit_logs", "strategies", ["strategy_id"], ["id"], ondelete="SET NULL")
    op.create_foreign_key("trade_suggestions_strategy_id_fkey", "trade_suggestions", "strategies", ["strategy_id"], ["id"], ondelete="SET NULL")
