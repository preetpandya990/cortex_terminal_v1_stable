"""Rebuild strategy_activations to match Phase 3 ORM model.

Revision ID: 0020
Revises: 0019
Create Date: 2026-05-06

The migration 0016 created strategy_activations with an earlier schema design
(subscription_id, strategy_version, entry_price, current_stop, current_targets,
setup_started_at, paper_order_id, lowercase state values, etc.).

The Phase 3 ORM model (StrategyActivation), the FSM (activation_fsm.py), the
SL/TP worker (sl_tp_worker.py), and the dispatcher (dispatcher.py) were all
written against a redesigned schema that differs significantly:

  Renamed  : triggered_signal_id → signal_id
  Renamed  : entered_at          → entry_at
  Renamed  : exited_at           → exit_at
  Replaced : paper_order_id (single FK) → entry_order_id + exit_order_id
  Added    : auto_execute (BOOLEAN NOT NULL DEFAULT false)
  Removed  : subscription_id, strategy_version, entry_price, current_stop,
             current_targets, setup_started_at, metadata
  State    : values changed to uppercase (WATCHING / IN_TRADE / …)
  ExitRsn  : values changed to uppercase (SL / TP1 / TP2 / MANUAL / …)

The table is empty (no production activations yet), so it is safe to drop
and recreate. FK constraints from strategy_trades and trade_suggestions are
dropped first and restored after.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Drop inbound FK constraints from dependent tables ─────────────────
    op.drop_constraint(
        "fk_strategy_trades_activation_id",
        "strategy_trades",
        type_="foreignkey",
    )
    op.drop_constraint(
        "trade_suggestions_strategy_activation_id_fkey",
        "trade_suggestions",
        type_="foreignkey",
    )

    # ── 2. Drop the stale table ───────────────────────────────────────────────
    op.drop_table("strategy_activations")

    # ── 3. Recreate with the schema the ORM model and FSM code depend on ─────
    op.create_table(
        "strategy_activations",

        sa.Column(
            "id",
            pg.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),

        # Owner and strategy linkage
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE", name="fk_activations_user_id"),
            nullable=False,
        ),
        sa.Column(
            "strategy_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("strategies.id", ondelete="CASCADE", name="fk_activations_strategy_id"),
            nullable=False,
        ),

        # Immutable snapshot of strategy.definition at activation time.
        # The FSM reads SL/TP config exclusively from this — never from the
        # live Strategy row — so edits never affect open activations.
        sa.Column("strategy_snapshot", pg.JSONB(astext_type=sa.Text()), nullable=False),

        # Originating signal (nullable: manual activations have no signal)
        sa.Column(
            "signal_id",
            sa.BigInteger(),
            sa.ForeignKey("ai_trading_signals.id", ondelete="SET NULL", name="fk_activations_signal_id"),
            nullable=True,
        ),

        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("instrument_key", sa.String(200), nullable=True),

        # FSM state — uppercase values match ActivationState enum in schemas
        sa.Column(
            "state",
            sa.String(15),
            nullable=False,
            server_default=sa.text("'WATCHING'"),
        ),

        # Append-only FSM transition log stored as JSONB array.
        # Every transition appends: {"state": "...", "at": "ISO-8601", ...context}
        # The SL/TP worker reads entry prices exclusively from this array
        # (IN_TRADE entry stores sl_price, tp1_price, tp2_price, tp3_price).
        sa.Column(
            "state_history",
            pg.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),

        # When true the FSM automatically places a MARKET paper order on
        # WATCHING → IN_TRADE and closes it on exit.
        sa.Column(
            "auto_execute",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),

        # Paper order UUIDs — set only when auto_execute=true
        sa.Column(
            "entry_order_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("paper_orders.id", ondelete="SET NULL", name="fk_activations_entry_order_id"),
            nullable=True,
        ),
        sa.Column(
            "exit_order_id",
            pg.UUID(as_uuid=True),
            sa.ForeignKey("paper_orders.id", ondelete="SET NULL", name="fk_activations_exit_order_id"),
            nullable=True,
        ),

        # Closed-state fields
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("entry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_at", sa.DateTime(timezone=True), nullable=True),

        # Audit timestamps
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

        # State values must match ActivationState enum
        sa.CheckConstraint(
            "state IN ('WATCHING','IN_SETUP','IN_TRADE','PARTIAL_EXIT','EXITED','CANCELLED')",
            name="ck_activations_state",
        ),
        # Exit-reason values must match ActivationExitReason enum (NULL = not yet exited)
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN "
            "('TP1','TP2','TP3','FULL_TP','SL','MANUAL',"
            "'SIGNAL_EXPIRED','STRATEGY_PAUSED','RULE_VIOLATED')",
            name="ck_activations_exit_reason",
        ),
    )

    # ── 4. Indexes ────────────────────────────────────────────────────────────
    # SL/TP worker: bulk fetch of all IN_TRADE + PARTIAL_EXIT activations
    op.create_index(
        "idx_activations_active_state",
        "strategy_activations",
        ["state"],
        postgresql_where=sa.text("state IN ('IN_TRADE','PARTIAL_EXIT')"),
    )
    # Service list queries filtered by user + optional state
    op.create_index(
        "idx_activations_user_state",
        "strategy_activations",
        ["user_id", "state"],
        postgresql_where=sa.text("state NOT IN ('EXITED','CANCELLED')"),
    )
    # Strategy performance dashboard
    op.create_index(
        "idx_activations_strategy_created",
        "strategy_activations",
        ["strategy_id", sa.text("created_at DESC")],
    )
    # Signal attribution lookups
    op.create_index(
        "idx_activations_signal_id",
        "strategy_activations",
        ["signal_id"],
        postgresql_where=sa.text("signal_id IS NOT NULL"),
    )
    # SL/TP worker Redis key construction
    op.create_index(
        "idx_activations_instrument_key",
        "strategy_activations",
        ["instrument_key"],
        postgresql_where=sa.text("instrument_key IS NOT NULL"),
    )

    # ── 5. Restore inbound FK constraints from dependent tables ──────────────
    op.create_foreign_key(
        "fk_strategy_trades_activation_id",
        "strategy_trades",
        "strategy_activations",
        ["activation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "trade_suggestions_strategy_activation_id_fkey",
        "trade_suggestions",
        "strategy_activations",
        ["strategy_activation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Drop inbound FKs
    op.drop_constraint(
        "fk_strategy_trades_activation_id",
        "strategy_trades",
        type_="foreignkey",
    )
    op.drop_constraint(
        "trade_suggestions_strategy_activation_id_fkey",
        "trade_suggestions",
        type_="foreignkey",
    )

    # Drop the Phase 3 table
    op.drop_table("strategy_activations")

    # Restore the original 0016 schema (Phase 2 design)
    op.create_table(
        "strategy_activations",

        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("subscription_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("user_strategy_subscriptions.id", ondelete="CASCADE",
                                name="fk_activations_subscription_id"),
                  nullable=False),
        sa.Column("strategy_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("strategies.id", ondelete="CASCADE",
                                name="fk_activations_strategy_id"),
                  nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="CASCADE",
                                name="fk_activations_user_id"),
                  nullable=False),
        sa.Column("symbol", sa.String(50), nullable=False),
        sa.Column("state", sa.String(20), nullable=False,
                  server_default=sa.text("'watching'")),
        sa.Column("triggered_signal_id", sa.BigInteger(),
                  sa.ForeignKey("ai_trading_signals.id", ondelete="SET NULL",
                                name="fk_activations_triggered_signal_id"),
                  nullable=True),
        sa.Column("entry_price", sa.Numeric(12, 4), nullable=True),
        sa.Column("current_stop", sa.Numeric(12, 4), nullable=True),
        sa.Column("current_targets", pg.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("setup_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(30), nullable=True),
        sa.Column("state_history", pg.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("paper_order_id", pg.UUID(as_uuid=True),
                  sa.ForeignKey("paper_orders.id", ondelete="SET NULL",
                                name="fk_activations_paper_order_id"),
                  nullable=True),
        sa.Column("strategy_snapshot", pg.JSONB(astext_type=sa.Text()), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),
        sa.Column("metadata", pg.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("instrument_key", sa.String(200), nullable=True),

        sa.CheckConstraint(
            "state IN ('watching','in_setup','in_trade','partial_exit','exited')",
            name="ck_activations_state",
        ),
        sa.CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN "
            "('stop','target_1','target_2','target_3','signal_reversal','timeout','manual')",
            name="ck_activations_exit_reason",
        ),
        sa.CheckConstraint("strategy_version >= 1", name="ck_activations_version_positive"),
    )

    # Restore original indexes
    op.create_index("idx_activations_strategy_created", "strategy_activations",
                    ["strategy_id", sa.text("created_at DESC")])
    op.create_index("idx_activations_subscription_symbol_state", "strategy_activations",
                    ["subscription_id", "symbol", "state"])
    op.create_index("idx_activations_user_state", "strategy_activations",
                    ["user_id", "state"],
                    postgresql_where=sa.text("state != 'exited'"))
    op.create_index("idx_activations_triggered_signal_id", "strategy_activations",
                    ["triggered_signal_id"],
                    postgresql_where=sa.text("triggered_signal_id IS NOT NULL"))
    op.create_index("idx_strategy_activations_instrument_key", "strategy_activations",
                    ["instrument_key"],
                    postgresql_where=sa.text("instrument_key IS NOT NULL"))

    # Restore inbound FKs
    op.create_foreign_key(
        "fk_strategy_trades_activation_id",
        "strategy_trades", "strategy_activations",
        ["activation_id"], ["id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "trade_suggestions_strategy_activation_id_fkey",
        "trade_suggestions", "strategy_activations",
        ["strategy_activation_id"], ["id"], ondelete="SET NULL",
    )
