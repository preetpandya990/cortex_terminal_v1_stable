"""
Database models.

Import order matters — relationships with forward-references are resolved at
mapper initialisation time, so dependent models must be imported after their
targets:
  User            → must precede WatchlistItem, Portfolio, PaperTradeOutcome
  TradeSuggestion → must precede PaperOrder, PaperPosition, PaperTradeOutcome
  Portfolio       → must precede PaperOrder, PaperPosition, PaperTradeOutcome,
                    PaperPnlSnapshot
  PaperPosition   → must precede PaperTradeOutcome
"""

from app.models.ml_data import (
    MLDriftMetric,
    MLPrediction,
    MLModelMetadata,
    MLAuditLog,
    Base,
)
from app.models.user import User, RefreshToken
from app.models.trade_suggestions import TradeSuggestion, EventCorrelation
from app.models.watchlist import WatchlistItem
from app.models.paper_trading import (
    Portfolio,
    PaperOrder,
    PaperFill,
    PaperPosition,
    PaperTradeOutcome,
    PaperPnlSnapshot,
)

__all__ = [
    "Base",
    # Auth
    "User",
    "RefreshToken",
    # ML
    "MLDriftMetric",
    "MLPrediction",
    "MLModelMetadata",
    "MLAuditLog",
    # Signals
    "TradeSuggestion",
    "EventCorrelation",
    # Watchlist
    "WatchlistItem",
    # Paper Trading
    "Portfolio",
    "PaperOrder",
    "PaperFill",
    "PaperPosition",
    "PaperTradeOutcome",
    "PaperPnlSnapshot",
]
