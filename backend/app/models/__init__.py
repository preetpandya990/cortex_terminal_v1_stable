"""
Database models.

Import order matters — relationships with forward-references are resolved at
mapper initialisation time, so dependent models must be imported after their
targets:
  User                     → must precede all models that back-reference it
  TradeSuggestion          → must precede PaperOrder, PaperPosition, PaperTradeOutcome
  Portfolio / PaperOrder   → must precede StrategyActivation (entry/exit order FKs)
  Strategy                 → must precede UserStrategySubscription, StrategyActivation
  StrategyActivation       → must precede StrategyTrade
"""

from app.models.ml_data import (
    MLDriftMetric,
    MLPrediction,
    MLModelMetadata,
    MLAuditLog,
    Base,
)
from app.models.ml_feedback import MLFeedbackError
from app.models.user import User, RefreshToken
from app.models.trade_suggestions import TradeSuggestion, EventCorrelation, UserSuggestionCompliance
from app.models.watchlist import WatchlistItem
from app.models.paper_trading import (
    Portfolio,
    PaperOrder,
    PaperFill,
    PaperPosition,
    PaperTradeOutcome,
    PaperPnlSnapshot,
)
from app.models.strategies import (
    UserPreferences,
    Strategy,
    UserStrategySubscription,
    StrategyActivation,
    StrategyTrade,
    StrategyBacktestRun,
    StrategyAuditLog,
)
from app.models.fundamentals import (
    CompanyFundamentalsProfile,
    CompanyKeyRatios,
    CompanyIncomeStatement,
    CompanyBalanceSheet,
    CompanyCashFlow,
    CompanyShareHoldings,
    CompanyCorporateActions,
    CompanyCompetitors,
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
    "MLFeedbackError",
    # Signals
    "TradeSuggestion",
    "EventCorrelation",
    "UserSuggestionCompliance",
    # Watchlist
    "WatchlistItem",
    # Paper Trading
    "Portfolio",
    "PaperOrder",
    "PaperFill",
    "PaperPosition",
    "PaperTradeOutcome",
    "PaperPnlSnapshot",
    # Trading Strategies
    "UserPreferences",
    "Strategy",
    "UserStrategySubscription",
    "StrategyActivation",
    "StrategyTrade",
    "StrategyBacktestRun",
    "StrategyAuditLog",
    # Company Fundamentals
    "CompanyFundamentalsProfile",
    "CompanyKeyRatios",
    "CompanyIncomeStatement",
    "CompanyBalanceSheet",
    "CompanyCashFlow",
    "CompanyShareHoldings",
    "CompanyCorporateActions",
    "CompanyCompetitors",
]
