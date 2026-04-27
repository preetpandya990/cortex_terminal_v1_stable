"""
Watchlist ORM Models
====================
SQLAlchemy 2.0 models for per-user stock watchlists.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class WatchlistItem(Base):
    """
    User watchlist item for tracking stocks.
    
    Each user can maintain a personalized watchlist of stocks they want to monitor.
    Items are ordered by position for custom arrangement.
    """
    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("user_id", "instrument_key", name="uq_user_instrument"),
        UniqueConstraint("user_id", "position", name="uq_user_position"),
    )
    
    # Primary key
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    
    # Foreign key to user
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True
    )
    
    # Instrument identification
    instrument_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    trading_symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    exchange: Mapped[str | None] = mapped_column(String(20))
    
    # Ordering
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False
    )
    
    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="watchlist_items")
    
    def __repr__(self) -> str:
        return f"<WatchlistItem(id={self.id}, user_id={self.user_id}, symbol={self.trading_symbol})>"
