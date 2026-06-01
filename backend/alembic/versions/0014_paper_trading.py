"""Paper Trading & Audit

Revision ID: 0014
Revises: 0013
Create Date: 2026-05-01 00:00:00.000000

Introduces the full Paper Trading & Audit subsystem:

  portfolios            — per-user paper portfolio with capital + risk settings
  paper_orders          — append-only order intent records (all order actions)
  paper_fills           — immutable execution records (TimescaleDB hypertable)
  paper_positions       — mutable live position state with WAC cost basis
  paper_trade_outcomes  — append-only audit + ML feedback (written on close)
  paper_pnl_snapshots   — daily EOD P&L snapshots (TimescaleDB hypertable)

Design decisions:
  - All monetary values use NUMERIC — never FLOAT — to avoid IEEE 754 drift
  - paper_fills and paper_pnl_snapshots are TimescaleDB hypertables;
    creation is wrapped in a DO block that degrades gracefully on plain Postgres
  - paper_fills and paper_trade_outcomes are append-only (no updated_at);
    immutability is enforced at the application layer via AuditLogger
  - WAC (Weighted Average Cost) is the SEBI-mandated cost basis for NSE equity
  - T+1 settlement is tracked via settlement_date on paper_fills
  - NSE charges (STT, brokerage, exchange, SEBI, GST, stamp duty) are stored
    individually so ML feedback can attribute drag accurately
  - UNIQUE partial index on paper_positions ensures only one OPEN position
    per symbol per portfolio at any time
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0014'
down_revision: Union[str, None] = '0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ──────────────────────────────────────────────────────────────────────────────
# UPGRADE
# ──────────────────────────────────────────────────────────────────────────────

def upgrade() -> None:

    # ── 1. portfolios ─────────────────────────────────────────────────────────
    op.create_table(
        'portfolios',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('user_id', sa.Integer(), nullable=False),

        # Type discriminator — forward-compatible with LIVE trading
        sa.Column('portfolio_type', sa.String(10), nullable=False,
                  server_default=sa.text("'PAPER'")),

        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('initial_capital', sa.Numeric(18, 2), nullable=False),
        sa.Column('current_cash', sa.Numeric(18, 2), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False,
                  server_default=sa.text("'INR'")),

        # Per-portfolio risk settings (user-adjustable)
        sa.Column('risk_per_trade_pct', sa.Numeric(4, 2), nullable=False,
                  server_default=sa.text('2.00')),
        sa.Column('max_open_positions', sa.Integer(), nullable=False,
                  server_default=sa.text('10')),

        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.text('true')),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_portfolios'),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_portfolios_user_id',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "portfolio_type IN ('PAPER', 'LIVE')",
            name='ck_portfolios_type',
        ),
        sa.CheckConstraint(
            'risk_per_trade_pct > 0 AND risk_per_trade_pct <= 10',
            name='ck_portfolios_risk_pct',
        ),
        sa.CheckConstraint(
            'max_open_positions >= 1 AND max_open_positions <= 100',
            name='ck_portfolios_max_positions',
        ),
        sa.CheckConstraint(
            'current_cash >= 0',
            name='ck_portfolios_cash_non_negative',
        ),
        sa.CheckConstraint(
            'initial_capital > 0',
            name='ck_portfolios_initial_capital_positive',
        ),
    )

    # One active portfolio per user (partial unique index)
    op.create_index(
        'uq_portfolios_user_active',
        'portfolios',
        ['user_id'],
        unique=True,
        postgresql_where=sa.text('is_active = true'),
    )
    op.create_index('idx_portfolios_user_id', 'portfolios', ['user_id'])

    # ── 2. paper_orders ───────────────────────────────────────────────────────
    op.create_table(
        'paper_orders',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=True),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('instrument_key', sa.String(100), nullable=False),

        # Order classification (Upstox / Zerodha taxonomy)
        sa.Column('transaction_type', sa.String(4), nullable=False),
        sa.Column('product_type', sa.String(10), nullable=False),
        sa.Column('order_type', sa.String(10), nullable=False),
        sa.Column('validity', sa.String(6), nullable=False,
                  server_default=sa.text("'DAY'")),

        # Quantities & prices
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price', sa.Numeric(12, 4), nullable=True),
        sa.Column('trigger_price', sa.Numeric(12, 4), nullable=True),

        sa.Column('status', sa.String(12), nullable=False,
                  server_default=sa.text("'PENDING'")),
        sa.Column('rejection_reason', sa.String(200), nullable=True),

        sa.Column('placed_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_paper_orders'),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_paper_orders_portfolio_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['suggestion_id'], ['trade_suggestions.suggestion_id'],
            name='fk_paper_orders_suggestion_id',
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            "transaction_type IN ('BUY', 'SELL')",
            name='ck_paper_orders_transaction_type',
        ),
        sa.CheckConstraint(
            "product_type IN ('CNC', 'MIS', 'NRML')",
            name='ck_paper_orders_product_type',
        ),
        sa.CheckConstraint(
            "order_type IN ('MARKET', 'LIMIT', 'SL', 'SL-M')",
            name='ck_paper_orders_order_type',
        ),
        sa.CheckConstraint(
            "validity IN ('DAY', 'IOC')",
            name='ck_paper_orders_validity',
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'OPEN', 'COMPLETE', 'REJECTED', 'CANCELLED')",
            name='ck_paper_orders_status',
        ),
        sa.CheckConstraint(
            'quantity > 0',
            name='ck_paper_orders_quantity_positive',
        ),
    )

    op.create_index(
        'idx_paper_orders_portfolio_status',
        'paper_orders',
        ['portfolio_id', 'status'],
    )
    op.create_index(
        'idx_paper_orders_suggestion_id',
        'paper_orders',
        ['suggestion_id'],
    )
    op.create_index(
        'idx_paper_orders_symbol_placed',
        'paper_orders',
        ['symbol', sa.text('placed_at DESC')],
    )
    # Partial index: open/pending orders needing fill evaluation
    op.create_index(
        'idx_paper_orders_active',
        'paper_orders',
        ['portfolio_id', 'placed_at'],
        postgresql_where=sa.text("status IN ('PENDING', 'OPEN')"),
    )

    # ── 3. paper_fills ────────────────────────────────────────────────────────
    # Append-only, immutable execution records.
    # Converted to a TimescaleDB hypertable below (graceful fallback).
    op.create_table(
        'paper_fills',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('order_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=False),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('fill_quantity', sa.Integer(), nullable=False),
        sa.Column('fill_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('slippage_bps', sa.Numeric(6, 2), nullable=False,
                  server_default=sa.text('0')),

        # NSE charge components stored individually for ML attribution
        sa.Column('brokerage', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('stt', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('exchange_charges', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('sebi_charges', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('gst', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('stamp_duty', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('total_charges', sa.Numeric(10, 4), nullable=False),
        sa.Column('net_amount', sa.Numeric(14, 4), nullable=False),

        # T+1 settlement tracking (CNC delivery)
        sa.Column('settlement_date', sa.Date(), nullable=False),

        # Hypertable partition key — no updated_at (append-only)
        sa.Column('executed_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_paper_fills'),
        sa.ForeignKeyConstraint(
            ['order_id'], ['paper_orders.id'],
            name='fk_paper_fills_order_id',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_paper_fills_portfolio_id',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            'fill_quantity > 0',
            name='ck_paper_fills_quantity_positive',
        ),
        sa.CheckConstraint(
            'fill_price > 0',
            name='ck_paper_fills_price_positive',
        ),
        sa.CheckConstraint(
            'total_charges >= 0',
            name='ck_paper_fills_charges_non_negative',
        ),
    )

    op.create_index(
        'idx_paper_fills_portfolio_executed',
        'paper_fills',
        ['portfolio_id', sa.text('executed_at DESC')],
    )
    op.create_index(
        'idx_paper_fills_order_id',
        'paper_fills',
        ['order_id'],
    )
    op.create_index(
        'idx_paper_fills_symbol_executed',
        'paper_fills',
        ['symbol', sa.text('executed_at DESC')],
    )

    # ── 4. paper_positions ────────────────────────────────────────────────────
    op.create_table(
        'paper_positions',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=True),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('instrument_key', sa.String(100), nullable=False),

        # WAC cost basis (recalculated atomically on each fill)
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('avg_cost_price', sa.Numeric(12, 4), nullable=False),

        # Tick-updated fields (not truth — Redis cache is the live source)
        sa.Column('last_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('unrealized_pnl', sa.Numeric(14, 4), nullable=True),

        # Accumulated realized P&L (from partial closes)
        sa.Column('realized_pnl', sa.Numeric(14, 4), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('total_charges', sa.Numeric(10, 4), nullable=False,
                  server_default=sa.text('0')),

        sa.Column('side', sa.String(5), nullable=False),

        # Targets copied from source suggestion at open time (denormalized
        # to survive suggestion expiry while position remains open)
        sa.Column('target_price_1', sa.Numeric(12, 4), nullable=True),
        sa.Column('target_price_2', sa.Numeric(12, 4), nullable=True),
        sa.Column('target_price_3', sa.Numeric(12, 4), nullable=True),
        sa.Column('stop_loss', sa.Numeric(12, 4), nullable=True),

        sa.Column('status', sa.String(8), nullable=False,
                  server_default=sa.text("'OPEN'")),
        sa.Column('opened_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),
        sa.Column('closed_at', sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_paper_positions'),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_paper_positions_portfolio_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['suggestion_id'], ['trade_suggestions.suggestion_id'],
            name='fk_paper_positions_suggestion_id',
            ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            "side IN ('LONG', 'SHORT')",
            name='ck_paper_positions_side',
        ),
        sa.CheckConstraint(
            "status IN ('OPEN', 'CLOSED')",
            name='ck_paper_positions_status',
        ),
        sa.CheckConstraint(
            'quantity >= 0',
            name='ck_paper_positions_quantity_non_negative',
        ),
        sa.CheckConstraint(
            'avg_cost_price > 0',
            name='ck_paper_positions_cost_price_positive',
        ),
    )

    # Prevents duplicate open positions for the same symbol in one portfolio
    op.create_index(
        'uq_paper_positions_portfolio_symbol_open',
        'paper_positions',
        ['portfolio_id', 'symbol'],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
    )
    op.create_index(
        'idx_paper_positions_portfolio_status',
        'paper_positions',
        ['portfolio_id', 'status'],
    )
    op.create_index(
        'idx_paper_positions_suggestion_id',
        'paper_positions',
        ['suggestion_id'],
    )
    op.create_index(
        'idx_paper_positions_symbol',
        'paper_positions',
        ['symbol'],
        postgresql_where=sa.text("status = 'OPEN'"),
    )

    # ── 5. paper_trade_outcomes ───────────────────────────────────────────────
    # Append-only. Written exactly once on position close.
    # No updated_at — immutability is a design invariant, not just a convention.
    op.create_table(
        'paper_trade_outcomes',

        sa.Column('id', postgresql.UUID(as_uuid=True), nullable=False,
                  server_default=sa.text('gen_random_uuid()')),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('position_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('suggestion_id', postgresql.UUID(as_uuid=True), nullable=True),
        # Denormalized for fast admin/ML queries without joins
        sa.Column('user_id', sa.Integer(), nullable=False),

        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('signal_direction', sa.String(4), nullable=False),

        # Actual execution prices
        sa.Column('entry_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('exit_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),

        # P&L breakdown
        sa.Column('gross_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('total_charges', sa.Numeric(10, 4), nullable=False),
        sa.Column('net_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('pnl_pct', sa.Numeric(8, 4), nullable=False),

        sa.Column('hold_duration_seconds', sa.Integer(), nullable=False),
        sa.Column('exit_reason', sa.String(10), nullable=False),

        # Original suggestion parameters (snapshot at trade entry)
        sa.Column('suggested_entry_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('suggested_stop_loss', sa.Numeric(12, 4), nullable=True),
        sa.Column('suggested_tp1', sa.Numeric(12, 4), nullable=True),
        sa.Column('suggested_tp2', sa.Numeric(12, 4), nullable=True),
        sa.Column('suggested_tp3', sa.Numeric(12, 4), nullable=True),
        sa.Column('suggestion_consensus_score', sa.Numeric(5, 2), nullable=True),
        sa.Column('suggestion_confidence_level', sa.String(10), nullable=True),

        # Execution quality
        sa.Column('entry_slippage_pct', sa.Numeric(6, 4), nullable=True),

        # ML feedback fields — computed by outcome_service after close
        sa.Column('ml_direction_correct', sa.Boolean(), nullable=True),
        sa.Column('hit_tp1', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('hit_tp2', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('hit_tp3', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('hit_sl', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),

        # Market context at entry (from ai_regime_detections snapshot)
        sa.Column('market_regime_at_entry', sa.String(50), nullable=True),

        # Immutable timestamp — no updated_at by design
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_paper_trade_outcomes'),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_outcomes_portfolio_id',
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['position_id'], ['paper_positions.id'],
            name='fk_outcomes_position_id',
            ondelete='RESTRICT',
        ),
        sa.ForeignKeyConstraint(
            ['suggestion_id'], ['trade_suggestions.suggestion_id'],
            name='fk_outcomes_suggestion_id',
            ondelete='SET NULL',
        ),
        sa.ForeignKeyConstraint(
            ['user_id'], ['users.id'],
            name='fk_outcomes_user_id',
            ondelete='CASCADE',
        ),
        sa.CheckConstraint(
            "signal_direction IN ('BUY', 'SELL')",
            name='ck_outcomes_signal_direction',
        ),
        sa.CheckConstraint(
            "exit_reason IN ('TP1', 'TP2', 'TP3', 'SL', 'MANUAL', 'EXPIRED')",
            name='ck_outcomes_exit_reason',
        ),
        sa.CheckConstraint(
            "suggestion_confidence_level IN ('HIGH', 'MEDIUM', 'LOW') "
            "OR suggestion_confidence_level IS NULL",
            name='ck_outcomes_confidence_level',
        ),
        sa.CheckConstraint(
            'quantity > 0',
            name='ck_outcomes_quantity_positive',
        ),
        sa.CheckConstraint(
            'hold_duration_seconds >= 0',
            name='ck_outcomes_hold_duration_non_negative',
        ),
    )

    op.create_index(
        'idx_outcomes_portfolio_created',
        'paper_trade_outcomes',
        ['portfolio_id', sa.text('created_at DESC')],
    )
    op.create_index(
        'idx_outcomes_user_created',
        'paper_trade_outcomes',
        ['user_id', sa.text('created_at DESC')],
    )
    op.create_index(
        'idx_outcomes_suggestion_id',
        'paper_trade_outcomes',
        ['suggestion_id'],
    )
    op.create_index(
        'idx_outcomes_symbol_created',
        'paper_trade_outcomes',
        ['symbol', sa.text('created_at DESC')],
    )
    # ML feedback query indexes
    op.create_index(
        'idx_outcomes_ml_direction',
        'paper_trade_outcomes',
        ['ml_direction_correct', 'suggestion_confidence_level'],
        postgresql_where=sa.text('ml_direction_correct IS NOT NULL'),
    )
    op.create_index(
        'idx_outcomes_exit_reason',
        'paper_trade_outcomes',
        ['exit_reason', sa.text('created_at DESC')],
    )
    op.create_index(
        'idx_outcomes_regime_direction',
        'paper_trade_outcomes',
        ['market_regime_at_entry', 'signal_direction'],
        postgresql_where=sa.text('market_regime_at_entry IS NOT NULL'),
    )

    # ── 6. paper_pnl_snapshots ────────────────────────────────────────────────
    # Daily EOD snapshots — append-only, TimescaleDB hypertable.
    op.create_table(
        'paper_pnl_snapshots',

        sa.Column('id', sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column('portfolio_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('snapshot_date', sa.Date(), nullable=False),
        sa.Column('total_realized_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('total_unrealized_pnl', sa.Numeric(14, 4), nullable=False),
        sa.Column('portfolio_value', sa.Numeric(14, 4), nullable=False),
        sa.Column('cash_balance', sa.Numeric(14, 4), nullable=False),
        sa.Column('num_open_positions', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('num_trades_today', sa.Integer(), nullable=False,
                  server_default=sa.text('0')),
        sa.Column('win_rate', sa.Numeric(5, 2), nullable=True),
        sa.Column('nifty_close', sa.Numeric(12, 4), nullable=True),

        # Hypertable partition key — no updated_at (append-only)
        sa.Column('captured_at', sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text('NOW()')),

        sa.PrimaryKeyConstraint('id', name='pk_paper_pnl_snapshots'),
        sa.ForeignKeyConstraint(
            ['portfolio_id'], ['portfolios.id'],
            name='fk_pnl_snapshots_portfolio_id',
            ondelete='CASCADE',
        ),
        sa.UniqueConstraint(
            'portfolio_id', 'snapshot_date',
            name='uq_pnl_snapshots_portfolio_date',
        ),
        sa.CheckConstraint(
            'num_open_positions >= 0',
            name='ck_pnl_snapshots_open_positions_non_negative',
        ),
        sa.CheckConstraint(
            'num_trades_today >= 0',
            name='ck_pnl_snapshots_trades_today_non_negative',
        ),
        sa.CheckConstraint(
            'win_rate IS NULL OR (win_rate >= 0 AND win_rate <= 100)',
            name='ck_pnl_snapshots_win_rate_range',
        ),
    )

    op.create_index(
        'idx_pnl_snapshots_portfolio_captured',
        'paper_pnl_snapshots',
        ['portfolio_id', sa.text('captured_at DESC')],
    )

    # ── TimescaleDB hypertables ────────────────────────────────────────────────
    # Wrapped in a DO block so the migration succeeds on plain PostgreSQL too.
    op.execute("""
        DO $$ BEGIN
            PERFORM create_hypertable(
                'paper_fills', 'executed_at',
                chunk_time_interval => INTERVAL '1 month',
                if_not_exists => TRUE
            );
            ALTER TABLE paper_fills SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'portfolio_id, symbol',
                timescaledb.compress_orderby   = 'executed_at DESC'
            );
            PERFORM add_compression_policy(
                'paper_fills', INTERVAL '7 days', if_not_exists => TRUE
            );

            PERFORM create_hypertable(
                'paper_pnl_snapshots', 'captured_at',
                chunk_time_interval => INTERVAL '1 year',
                if_not_exists => TRUE
            );
            ALTER TABLE paper_pnl_snapshots SET (
                timescaledb.compress,
                timescaledb.compress_segmentby = 'portfolio_id',
                timescaledb.compress_orderby   = 'captured_at DESC'
            );
            PERFORM add_compression_policy(
                'paper_pnl_snapshots', INTERVAL '30 days', if_not_exists => TRUE
            );
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'TimescaleDB not available, skipping hypertable setup: %', SQLERRM;
        END $$;
    """)


# ──────────────────────────────────────────────────────────────────────────────
# DOWNGRADE
# ──────────────────────────────────────────────────────────────────────────────

def downgrade() -> None:
    # Drop in reverse dependency order
    op.drop_table('paper_pnl_snapshots')
    op.drop_table('paper_trade_outcomes')
    op.drop_table('paper_positions')
    op.drop_table('paper_fills')
    op.drop_table('paper_orders')
    op.drop_table('portfolios')
