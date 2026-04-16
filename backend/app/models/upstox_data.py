"""
Cortex AI — ORM Models
========================
All models use datetime.now(timezone.utc) via server_default=func.now()
for timezone-aware timestamps — no deprecated datetime.utcnow().
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StockOHLCV(Base):
    """
    Stock OHLCV data stored in TimescaleDB hypertable.
    
    This table uses a composite primary key (timestamp, symbol, exchange, timeframe)
    and is partitioned by time for optimal query performance on time-series data.
    """

    __tablename__ = "stock_ohlcv"
    __table_args__ = (
        PrimaryKeyConstraint(
            "timestamp", "symbol", "exchange", "timeframe",
            name="stock_ohlcv_pkey"
        ),
        Index("idx_ohlcv_symbol_timeframe_ts", "symbol", "timeframe", "timestamp"),
        Index("idx_ohlcv_exchange_ts", "exchange", "timestamp"),
        Index("stock_ohlcv_timestamp_idx", "timestamp", postgresql_using="btree"),
    )

    # Composite primary key columns
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False
    )
    symbol: Mapped[str] = mapped_column(
        String(20),
        primary_key=True,
        nullable=False,
        index=True
    )
    exchange: Mapped[str] = mapped_column(
        String(10),
        primary_key=True,
        nullable=False
    )
    timeframe: Mapped[str] = mapped_column(
        String(10),
        primary_key=True,
        nullable=False
    )

    # OHLCV data columns
    open: Mapped[float | None] = mapped_column(Numeric(14, 4))
    high: Mapped[float | None] = mapped_column(Numeric(14, 4))
    low: Mapped[float | None] = mapped_column(Numeric(14, 4))
    close: Mapped[float | None] = mapped_column(Numeric(14, 4))
    volume: Mapped[int | None] = mapped_column(BigInteger)
    adj_close: Mapped[float | None] = mapped_column(Numeric(14, 4))

    def __repr__(self) -> str:
        return (
            f"<StockOHLCV(symbol={self.symbol!r}, exchange={self.exchange!r}, "
            f"timeframe={self.timeframe!r}, timestamp={self.timestamp!r}, "
            f"close={self.close})>"
        )


# Legacy model - kept for backward compatibility but renamed to avoid conflicts
class UpstoxOHLCV(Base):
    """OHLCV candle data from Upstox with composite unique constraint."""

    __tablename__ = "upstox_ohlcv"  # Actual table name in database
    __table_args__ = (
        UniqueConstraint(
            "instrument_key", "timeframe", "timestamp",
            name="uq_stock_ohlcv",
        ),
        Index("idx_upstox_instrument_timeframe_ts", "instrument_key", "timeframe", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(10), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    open: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False)
    oi: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class UpstoxTick(Base):
    """Real-time tick data from the WebSocket feed."""

    __tablename__ = "upstox_ticks"
    __table_args__ = (
        Index("idx_upstox_tick_instrument_ts", "instrument_key", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_price: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=True)
    oi: Mapped[int] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InstrumentMaster(Base):
    """Upstox instrument master — synced periodically."""

    __tablename__ = "instrument_master"
    __table_args__ = (
        Index("idx_instrument_symbol", "symbol"),
        Index("idx_instrument_exchange", "exchange_segment"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    instrument_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False, index=True)
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    exchange_segment: Mapped[str] = mapped_column(String(20), nullable=False)
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )