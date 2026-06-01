"""Trading Strategies Engine

Revision ID: 0016
Revises: 0015
Create Date: 2026-05-06 00:00:00.000000

Introduces the full Trading Strategies Engine — a production-grade rule engine
that enforces user-defined trading rules on all AI and ML signals before they
surface as trade suggestions.

New tables:

  user_preferences           — per-user settings (enforcement mode, etc.)
  strategies                 — versioned, JSONB strategy definitions
  user_strategy_subscriptions — user → strategy links with priority ordering
  strategy_activations       — per-(subscription, symbol) FSM state machine
  strategy_trades            — execution history (TimescaleDB hypertable)
  strategy_backtest_runs     — backtest job tracking and results
  strategy_daily_performance — continuous aggregate (TimescaleDB, if available)

Design decisions:

  VERSIONING
  Updating a strategy definition bumps its `version` counter in-place.
  Each `strategy_activation` stores a full `strategy_snapshot` (JSONB) at the
  time of activation, so historical attribution is accurate even after
  definition changes. The `strategy_version` column enables correlation.

  ENFORCEMENT MODES
  Three modes govern how the strategy engine interacts with the signal
  pipeline per user:
    hard_gate      — only strategy-approved signals surface (pass-through
                     if no active subscriptions, with visible notification)
    soft_filter    — all signals surface, strategies tag/annotate them
    portfolio_opt_in — only portfolios linked to a strategy are affected

  PRIORITY ORDERING
  Each user subscription has an integer priority (lower = higher priority).
  The evaluator walks subscriptions in ascending priority order; first match
  wins. The `uq_strategy_subs_user_priority_active` partial unique index
  prevents two active subscriptions from sharing the same priority.

  MONETARY COLUMNS
  All monetary values use NUMERIC — never FLOAT — to avoid IEEE 754 drift.
  This matches the convention established in migrations 0014 and earlier.

  TIMESCALEDB
  `strategy_trades` is converted to a hypertable partitioned by `entry_at`
  (1-month chunks) with column-store compression after 7 days. A continuous
  aggregate `strategy_daily_performance` is created for fast dashboard queries.
  All TimescaleDB DDL is wrapped in a gracefully-degrading DO block so the
  migration succeeds on plain PostgreSQL for development environments.

  STATE MACHINE AUDIT TRAIL
  `strategy_activations.state_history` is a JSONB array where every state
  transition is appended:
    [{"from": "watching", "to": "in_setup", "trigger": "...",
      "ts": "ISO-8601", "context": {...}}, ...]
  This provides a full audit trail at zero join cost.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0016'
down_revision: Union[str, None] = '0015'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ──────────────────────────────────────────────────────────────────────────────
# UPGRADE
# ──────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. user_preferences ───────────────────────────────────────────────────
    # One row per user. Created lazily on first access (GET /users/me/preferences).
    op.create_table(
        'user_preferences',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', sa.Integer(), nullable=False),

        sa.Column(
            'enforcement_mode', sa.String(20), nullable=False,
            server_default=sa.text("'soft_filter'"),
            comment='hard_gate | soft_filter | portfolio_opt_in',
        ),

        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_user_preferences'),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_user_preferences_user_id',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint('user_id', name='uq_user_preferences_user_id'),
        sa.CheckConstraint(
            "enforcement_mode IN ('hard_gate', 'soft_filter', 'portfolio_opt_in')",
            name='ck_user_preferences_enforcement_mode',
        ),
    )

    op.create_index('idx_user_preferences_user_id', 'user_preferences', ['user_id'])

    # ── 2. strategies ─────────────────────────────────────────────────────────
    # Versioned strategy definitions. The `definition` JSONB column holds a
    # complete, validated StrategyDefinition object (serialised by Pydantic).
    # GIN index on `definition` enables fast JSONB path queries (e.g. find all
    # strategies that trade a specific symbol or require a specific regime).
    op.create_table(
        'strategies',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('name', sa.Text(), nullable=False),
        sa.Column('description', sa.Text(), nullable=False,
                  server_default=sa.text("''")),

        # Monotonically increasing. Bumped in-place whenever the definition
        # changes. Existing activations store a snapshot, so they are unaffected.
        sa.Column('version', sa.Integer(), nullable=False,
                  server_default=sa.text('1')),

        sa.Column('status', sa.String(20), nullable=False,
                  server_default=sa.text("'draft'")),
        sa.Column('scope', sa.String(20), nullable=False,
                  server_default=sa.text("'personal'")),

        # Ownership and governance
        sa.Column('created_by', sa.Integer(), nullable=False),
        sa.Column('promoted_by', sa.Integer(), nullable=True),
        sa.Column('promoted_at', sa.TIMESTAMP(timezone=True), nullable=True),

        # The full StrategyDefinition as validated JSONB
        sa.Column('definition', postgresql.JSONB(), nullable=False),

        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_strategies'),
        sa.ForeignKeyConstraint(
            ['created_by'], ['users.id'],
            name='fk_strategies_created_by',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['promoted_by'], ['users.id'],
            name='fk_strategies_promoted_by',
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'paused', 'archived')",
            name='ck_strategies_status',
        ),
        sa.CheckConstraint(
            "scope IN ('personal', 'global')",
            name='ck_strategies_scope',
        ),
        sa.CheckConstraint(
            'version >= 1',
            name='ck_strategies_version_positive',
        ),
    )

    # GIN index for JSONB content queries (e.g., find strategies by symbol)
    op.create_index(
        'idx_strategies_definition_gin',
        'strategies',
        ['definition'],
        postgresql_using='gin',
    )
    op.create_index(
        'idx_strategies_created_by_status',
        'strategies',
        ['created_by', 'status'],
    )
    op.create_index(
        'idx_strategies_scope_status',
        'strategies',
        ['scope', 'status'],
        postgresql_where=sa.text("status != 'archived'"),
    )

    # ── 3. user_strategy_subscriptions ────────────────────────────────────────
    # Links a user to a strategy with a configurable priority. Lower priority
    # values are evaluated first (1 = highest priority). The evaluator walks
    # subscriptions in ascending priority order; first match wins.
    op.create_table(
        'user_strategy_subscriptions',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), nullable=False),

        # Snapshot of the strategy version at subscription time.
        # Used to detect when the user is running against a stale version.
        sa.Column('strategy_version', sa.Integer(), nullable=False),

        # Lower number = higher priority. Unique per user when active.
        sa.Column('priority', sa.Integer(), nullable=False,
                  server_default=sa.text('100')),

        # portfolio_opt_in mode: this FK scopes the subscription to one portfolio
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=True),

        # When true, a qualifying signal automatically places a paper trade
        sa.Column('auto_execute', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.text('true')),

        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_user_strategy_subscriptions'),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_subs_user_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['strategy_id'], ['strategies.id'],
            name='fk_subs_strategy_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_subs_portfolio_id',
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            'priority >= 1',
            name='ck_subs_priority_positive',
        ),
        sa.CheckConstraint(
            'strategy_version >= 1',
            name='ck_subs_strategy_version_positive',
        ),
    )

    # One active subscription per (user, strategy)
    op.create_index(
        'uq_subs_user_strategy_active',
        'user_strategy_subscriptions',
        ['user_id', 'strategy_id'],
        unique=True,
        postgresql_where=sa.text('is_active = true'),
    )
    # One active subscription per priority level per user
    op.create_index(
        'uq_subs_user_priority_active',
        'user_strategy_subscriptions',
        ['user_id', 'priority'],
        unique=True,
        postgresql_where=sa.text('is_active = true'),
    )
    op.create_index(
        'idx_subs_user_id_active',
        'user_strategy_subscriptions',
        ['user_id', 'priority'],
        postgresql_where=sa.text('is_active = true'),
    )
    op.create_index(
        'idx_subs_strategy_id_active',
        'user_strategy_subscriptions',
        ['strategy_id'],
        postgresql_where=sa.text('is_active = true'),
    )

    # ── 4. strategy_activations ───────────────────────────────────────────────
    # One row per (subscription, symbol) represents the FSM state for that
    # strategy+symbol pair. Rows are reused across the lifecycle:
    #   watching → in_setup → in_trade → partial_exit → exited → watching ...
    # `state_history` is an append-only JSONB array for audit; the current
    # state is always in the `state` column for fast indexed queries.
    op.create_table(
        'strategy_activations',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('subscription_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('strategy_version', sa.Integer(), nullable=False),

        # Denormalised for fast per-user queries without joining subscriptions
        sa.Column('user_id', sa.Integer(), nullable=False),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('state', sa.String(20), nullable=False,
                  server_default=sa.text("'watching'")),

        # The signal that triggered the IN_SETUP transition (nullable — set on setup entry)
        sa.Column('triggered_signal_id', sa.BigInteger(), nullable=True),

        # Trade parameters — populated on IN_TRADE entry, updated on partial exits
        sa.Column('entry_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('current_stop', sa.Numeric(12, 4), nullable=True),
        sa.Column('current_targets', postgresql.JSONB(), nullable=True,
                  comment='Remaining TakeProfitTarget list as JSON array'),

        # Temporal markers
        sa.Column('setup_started_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('entered_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('exited_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('exit_reason', sa.String(30), nullable=True),

        # Full state-transition audit trail
        sa.Column('state_history', postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),

        # Paper order placed when auto_execute = true and state = in_trade
        sa.Column('paper_order_id', postgresql.UUID(as_uuid=True), nullable=True),

        # Full strategy definition snapshot at activation time. Ensures historical
        # trade attribution is correct even after the strategy is updated.
        sa.Column('strategy_snapshot', postgresql.JSONB(), nullable=False,
                  server_default=sa.text("'{}'::jsonb")),

        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_strategy_activations'),
        sa.ForeignKeyConstraint(
            ['subscription_id'], ['user_strategy_subscriptions.id'],
            name='fk_activations_subscription_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['strategy_id'], ['strategies.id'],
            name='fk_activations_strategy_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_activations_user_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['triggered_signal_id'], ['ai_trading_signals.id'],
            name='fk_activations_triggered_signal_id',
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['paper_order_id'], ['paper_orders.id'],
            name='fk_activations_paper_order_id',
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            "state IN ('watching', 'in_setup', 'in_trade', 'partial_exit', 'exited')",
            name='ck_activations_state',
        ),
        sa.CheckConstraint(
            "exit_reason IN ('stop', 'target_1', 'target_2', 'target_3', "
            "'signal_reversal', 'timeout', 'manual') OR exit_reason IS NULL",
            name='ck_activations_exit_reason',
        ),
        sa.CheckConstraint(
            'strategy_version >= 1',
            name='ck_activations_version_positive',
        ),
    )

    # Hot path: find non-exited activations for a user (dashboard, evaluator)
    op.create_index(
        'idx_activations_user_state',
        'strategy_activations',
        ['user_id', 'state'],
        postgresql_where=sa.text("state != 'exited'"),
    )
    # Evaluator: find watching/in_setup activations for a given symbol
    op.create_index(
        'idx_activations_subscription_symbol_state',
        'strategy_activations',
        ['subscription_id', 'symbol', 'state'],
    )
    # Governance/admin: all activations for a strategy, newest first
    op.create_index(
        'idx_activations_strategy_created',
        'strategy_activations',
        ['strategy_id', sa.text('created_at DESC')],
    )
    op.create_index(
        'idx_activations_triggered_signal_id',
        'strategy_activations',
        ['triggered_signal_id'],
        postgresql_where=sa.text('triggered_signal_id IS NOT NULL'),
    )

    # ── 5. strategy_trades ────────────────────────────────────────────────────
    # Append-only execution record written on every strategy trade close.
    # Converted to a TimescaleDB hypertable (partitioned by entry_at) below.
    op.create_table(
        'strategy_trades',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('activation_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('subscription_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), nullable=False),
        # Denormalised for fast per-user queries
        sa.Column('user_id', sa.Integer(), nullable=False),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('side', sa.String(5), nullable=False),

        # Execution prices
        sa.Column('entry_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('exit_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),

        # P&L
        sa.Column('gross_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('total_charges', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('net_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('pnl_pct', sa.Numeric(8, 4), nullable=False),

        # Risk params at entry
        sa.Column('stop_loss_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('exit_reason', sa.String(30), nullable=False),

        # Signal context at entry
        sa.Column('signal_confidence', sa.Numeric(5, 2), nullable=True),
        sa.Column('regime_at_entry', sa.String(50), nullable=True),

        # Temporal — entry_at is the hypertable partition key
        sa.Column('entry_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('exit_at', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('holding_bars', sa.Integer(), nullable=True),

        sa.Column('metadata', postgresql.JSONB(), nullable=True),

        sa.PrimaryKeyConstraint('id', name='pk_strategy_trades'),
        sa.ForeignKeyConstraint(
            ['activation_id'], ['strategy_activations.id'],
            name='fk_strategy_trades_activation_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['subscription_id'], ['user_strategy_subscriptions.id'],
            name='fk_strategy_trades_subscription_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['strategy_id'], ['strategies.id'],
            name='fk_strategy_trades_strategy_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_strategy_trades_user_id',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "side IN ('long', 'short')",
            name='ck_strategy_trades_side',
        ),
        sa.CheckConstraint(
            "exit_reason IN ('stop', 'target_1', 'target_2', 'target_3', "
            "'signal_reversal', 'timeout', 'manual')",
            name='ck_strategy_trades_exit_reason',
        ),
        sa.CheckConstraint(
            'quantity > 0',
            name='ck_strategy_trades_quantity_positive',
        ),
    )

    op.create_index(
        'idx_strategy_trades_strategy_entry',
        'strategy_trades',
        ['strategy_id', sa.text('entry_at DESC')],
    )
    op.create_index(
        'idx_strategy_trades_user_entry',
        'strategy_trades',
        ['user_id', sa.text('entry_at DESC')],
    )
    op.create_index(
        'idx_strategy_trades_symbol_entry',
        'strategy_trades',
        ['symbol', sa.text('entry_at DESC')],
    )

    # ── 6. strategy_backtest_runs ─────────────────────────────────────────────
    op.create_table(
        'strategy_backtest_runs',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('strategy_version', sa.Integer(), nullable=False),
        sa.Column('requested_by', sa.Integer(), nullable=False),

        sa.Column('mode', sa.String(20), nullable=False),
        # Stored as a PostgreSQL text array: {RELIANCE,INFY,TCS}
        sa.Column('symbols', postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column('start_dt', sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column('end_dt', sa.TIMESTAMP(timezone=True), nullable=False),

        sa.Column('status', sa.String(20), nullable=False,
                  server_default=sa.text("'queued'")),
        sa.Column('progress_pct', sa.Numeric(5, 2), nullable=False,
                  server_default=sa.text('0')),

        # Performance metrics — populated on completion
        sa.Column('total_trades', sa.Integer(), nullable=True),
        sa.Column('win_rate', sa.Numeric(5, 2), nullable=True),
        sa.Column('sharpe_ratio', sa.Numeric(8, 4), nullable=True),
        sa.Column('sortino_ratio', sa.Numeric(8, 4), nullable=True),
        sa.Column('calmar_ratio', sa.Numeric(8, 4), nullable=True),
        sa.Column('max_drawdown_pct', sa.Numeric(5, 2), nullable=True),
        sa.Column('total_return_pct', sa.Numeric(8, 4), nullable=True),
        sa.Column('avg_hold_bars', sa.Numeric(8, 2), nullable=True),

        # Full trade-level breakdown for the results page
        sa.Column('results_summary', postgresql.JSONB(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),

        sa.Column('started_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('completed_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_strategy_backtest_runs'),
        sa.ForeignKeyConstraint(
            ['strategy_id'], ['strategies.id'],
            name='fk_backtest_runs_strategy_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['requested_by'], ['users.id'],
            name='fk_backtest_runs_requested_by',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "mode IN ('signal_replay', 'model_replay', 'hybrid')",
            name='ck_backtest_runs_mode',
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name='ck_backtest_runs_status',
        ),
        sa.CheckConstraint(
            'progress_pct >= 0 AND progress_pct <= 100',
            name='ck_backtest_runs_progress_range',
        ),
        sa.CheckConstraint(
            'start_dt < end_dt',
            name='ck_backtest_runs_date_order',
        ),
    )

    op.create_index(
        'idx_backtest_runs_strategy_created',
        'strategy_backtest_runs',
        ['strategy_id', sa.text('created_at DESC')],
    )
    op.create_index(
        'idx_backtest_runs_requested_by',
        'strategy_backtest_runs',
        ['requested_by', sa.text('created_at DESC')],
    )
    # Hot index for the queue worker to poll for pending jobs
    op.create_index(
        'idx_backtest_runs_pending',
        'strategy_backtest_runs',
        ['created_at'],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    # ── TimescaleDB hypertable + continuous aggregate ─────────────────────────
    # Wrapped in a DO block; degrades gracefully on plain PostgreSQL.
    op.execute("""
        DO $$ BEGIN
            PERFORM create_hypertable(
                'strategy_trades', 'entry_at',
                chunk_time_interval => INTERVAL '1 month',
                if_not_exists       => TRUE
            );
            ALTER TABLE strategy_trades SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'strategy_id, user_id, symbol',
                timescaledb.compress_orderby   = 'entry_at DESC'
            );
            PERFORM add_compression_policy(
                'strategy_trades', INTERVAL '7 days', if_not_exists => TRUE
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE
                'TimescaleDB unavailable, skipping hypertable setup: %', SQLERRM;
        END $$;
    """)

    # Continuous aggregate for O(1) dashboard performance queries.
    # Uses EXECUTE inside a DO block so the DDL string is never reached by the
    # PostgreSQL parser when TimescaleDB is absent (avoiding a parse-time error).
    op.execute("""
        DO $$ BEGIN
            EXECUTE $inner$
                CREATE MATERIALIZED VIEW strategy_daily_performance
                WITH (timescaledb.continuous) AS
                SELECT
                    time_bucket('1 day', entry_at)                        AS bucket,
                    strategy_id,
                    user_id,
                    COUNT(*)                                               AS total_trades,
                    SUM(net_pnl)                                           AS daily_net_pnl,
                    AVG(CASE WHEN net_pnl > 0 THEN 1.0 ELSE 0.0 END)     AS win_rate,
                    MAX(net_pnl)                                           AS best_trade,
                    MIN(net_pnl)                                           AS worst_trade,
                    AVG(pnl_pct)                                           AS avg_pnl_pct
                FROM strategy_trades
                GROUP BY bucket, strategy_id, user_id
            $inner$;

            PERFORM add_continuous_aggregate_policy(
                'strategy_daily_performance',
                start_offset      => INTERVAL '3 days',
                end_offset        => INTERVAL '1 hour',
                schedule_interval => INTERVAL '1 hour',
                if_not_exists     => TRUE
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE
                'Continuous aggregate creation skipped (no TimescaleDB): %', SQLERRM;
        END $$;
    """)


# ──────────────────────────────────────────────────────────────────────────────
# DOWNGRADE
# ──────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    # Drop continuous aggregate first (depends on strategy_trades hypertable)
    op.execute("""
        DO $$ BEGIN
            EXECUTE 'DROP MATERIALIZED VIEW IF EXISTS strategy_daily_performance';
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END $$;
    """)

    # Drop in reverse dependency order
    op.drop_table('strategy_backtest_runs')
    op.drop_table('strategy_trades')
    op.drop_table('strategy_activations')
    op.drop_table('user_strategy_subscriptions')
    op.drop_table('strategies')
    op.drop_table('user_preferences')
