"""
Database models.

Import order matters — User must be registered with SQLAlchemy's mapper
before WatchlistItem, because WatchlistItem.user is a forward-reference
relationship("User", ...) that is resolved at mapper initialisation time.
"""

from app.models.ml_data import (
    MLDriftMetric,
    MLPrediction,
    MLModelMetadata,
    MLAuditLog,
    Base,
)
from app.models.user import User, RefreshToken          # must precede WatchlistItem
from app.models.trade_suggestions import (
    TradeSuggestion,
    EventCorrelation,
)
from app.models.watchlist import WatchlistItem

__all__ = [
    "Base",
    "User",
    "RefreshToken",
    "MLDriftMetric",
    "MLPrediction",
    "MLModelMetadata",
    "MLAuditLog",
    "TradeSuggestion",
    "EventCorrelation",
    "WatchlistItem",
]
