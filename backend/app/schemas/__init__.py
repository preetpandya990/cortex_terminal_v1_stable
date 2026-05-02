# Pydantic Schemas Package

from app.schemas.trade_suggestions import (
    ConfidenceLevel,
    EventCorrelationResponse,
    SignalDirection,
    SuggestionDetailResponse,
    SuggestionFilters,
    SuggestionStatsResponse,
    SuggestionStatus,
    TradeSuggestionResponse,
    TradeSuggestionsListResponse,
    TriggerPathway,
    TriggerType,
)
from app.schemas.paper_trading import (
    # Enums
    PortfolioType,
    TransactionType,
    ProductType,
    OrderType,
    OrderValidity,
    OrderStatus,
    PositionSide,
    PositionStatus,
    ExitReason,
    # Requests
    CreatePortfolioRequest,
    UpdatePortfolioSettingsRequest,
    PlaceOrderRequest,
    ClosePositionRequest,
    # Responses
    PortfolioResponse,
    PortfolioSummaryResponse,
    PaperOrderResponse,
    PaperFillResponse,
    PaperPositionResponse,
    PaperPositionDetailResponse,
    PaperTradeOutcomeResponse,
    PaperPnlSnapshotResponse,
    # List responses
    OrdersListResponse,
    PositionsListResponse,
    OutcomesListResponse,
    PnlSnapshotsListResponse,
    # Utility
    QtySuggestionResponse,
    OutcomeStatsBreakdown,
    OutcomeStatsResponse,
    # Real-time
    LivePositionPnL,
    LivePnLUpdate,
)

__all__ = [
    # Trade Suggestions
    "TradeSuggestionResponse",
    "EventCorrelationResponse",
    "TradeSuggestionsListResponse",
    "SuggestionDetailResponse",
    "SuggestionFilters",
    "SuggestionStatsResponse",
    "SignalDirection",
    "ConfidenceLevel",
    "TriggerPathway",
    "SuggestionStatus",
    "TriggerType",
    # Paper Trading — Enums
    "PortfolioType",
    "TransactionType",
    "ProductType",
    "OrderType",
    "OrderValidity",
    "OrderStatus",
    "PositionSide",
    "PositionStatus",
    "ExitReason",
    # Paper Trading — Requests
    "CreatePortfolioRequest",
    "UpdatePortfolioSettingsRequest",
    "PlaceOrderRequest",
    "ClosePositionRequest",
    # Paper Trading — Responses
    "PortfolioResponse",
    "PortfolioSummaryResponse",
    "PaperOrderResponse",
    "PaperFillResponse",
    "PaperPositionResponse",
    "PaperPositionDetailResponse",
    "PaperTradeOutcomeResponse",
    "PaperPnlSnapshotResponse",
    # Paper Trading — List responses
    "OrdersListResponse",
    "PositionsListResponse",
    "OutcomesListResponse",
    "PnlSnapshotsListResponse",
    # Paper Trading — Utility
    "QtySuggestionResponse",
    "OutcomeStatsBreakdown",
    "OutcomeStatsResponse",
    # Paper Trading — Real-time
    "LivePositionPnL",
    "LivePnLUpdate",
]
