"""
Trade Suggestions API Schemas
==============================
Pydantic v2 schemas for multi-agent trade suggestion validation and serialization.
"""
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ============================================================================
# Enums
# ============================================================================

class SignalDirection(str, Enum):
    """Trade signal direction."""
    BUY = "BUY"
    SELL = "SELL"


class ConfidenceLevel(str, Enum):
    """Consensus confidence level."""
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class TriggerPathway(str, Enum):
    """Event correlation trigger pathway."""
    TECHNICAL_FIRST = "TECHNICAL_FIRST"
    FUNDAMENTAL_FIRST = "FUNDAMENTAL_FIRST"


class SuggestionStatus(str, Enum):
    """Trade suggestion lifecycle status."""
    ACTIVE = "active"
    EXPIRED = "expired"
    EXECUTED = "executed"
    INVALIDATED = "invalidated"


class TriggerType(str, Enum):
    """Event correlation trigger type."""
    SCANNER_ANOMALY = "SCANNER_ANOMALY"
    NEWS_EVENT = "NEWS_EVENT"


# ============================================================================
# Response Schemas
# ============================================================================

class TradeSuggestionResponse(BaseModel):
    """
    Single trade suggestion response.
    
    Serializes TradeSuggestion ORM model for API responses.
    All Decimal fields are converted to float for JSON compatibility.
    """
    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "suggestion_id": "550e8400-e29b-41d4-a716-446655440000",
                "symbol": "RELIANCE",
                "instrument_key": "NSE_EQ|INE002A01018",
                "trading_symbol": "RELIANCE-EQ",
                "consensus_score": 85.5,
                "confidence_level": "HIGH",
                "signal_direction": "BUY",
                "trigger_pathway": "TECHNICAL_FIRST",
                "scanner_signal": {"score": 8.5, "indicators": ["RSI", "MACD"]},
                "ai_signal": {"sentiment": "bullish", "confidence": 0.92},
                "ml_signal": {"direction": "BUY", "probability": 0.87},
                "entry_price": 2450.50,
                "stop_loss": 2400.00,
                "risk_reward_ratio": 2.5,
                "take_profit_1": 2500.00,
                "take_profit_2": 2550.00,
                "take_profit_3": 2600.00,
                "generated_at": "2026-04-22T00:00:00+05:30",
                "expires_at": "2026-04-22T15:30:00+05:30",
                "status": "active",
                "created_at": "2026-04-22T00:00:00+05:30",
                "updated_at": "2026-04-22T00:00:00+05:30"
            }
        }
    )
    
    # Identifiers
    suggestion_id: UUID
    symbol: str = Field(min_length=1, max_length=50)
    instrument_key: str = Field(min_length=1, max_length=100)
    trading_symbol: str | None = Field(None, max_length=50)
    
    # Consensus metadata
    consensus_score: float = Field(ge=0.0, le=100.0, description="Weighted consensus score (0-100)")
    confidence_level: ConfidenceLevel
    signal_direction: SignalDirection
    trigger_pathway: TriggerPathway
    
    # Contributing signals
    scanner_signal: dict[str, Any] = Field(description="Technical scanner signal data")
    ai_signal: dict[str, Any] = Field(description="AI intelligence signal data")
    ml_signal: dict[str, Any] = Field(description="ML predictor signal data")
    
    # Trade parameters
    entry_price: float | None = Field(None, gt=0, description="Suggested entry price")
    stop_loss: float | None = Field(None, gt=0, description="Stop loss price")
    risk_reward_ratio: float | None = Field(None, gt=0, description="Risk/reward ratio")
    take_profit_1: float | None = Field(None, gt=0, description="First take profit target")
    take_profit_2: float | None = Field(None, gt=0, description="Second take profit target")
    take_profit_3: float | None = Field(None, gt=0, description="Third take profit target")
    
    # Temporal metadata
    generated_at: datetime = Field(description="When suggestion was generated")
    expires_at: datetime = Field(description="When suggestion expires")
    status: SuggestionStatus
    created_at: datetime
    updated_at: datetime
    
    @field_validator("consensus_score", mode="before")
    @classmethod
    def convert_decimal_to_float(cls, v: Any) -> float:
        """Convert Decimal to float for JSON serialization."""
        if isinstance(v, Decimal):
            return float(v)
        return v
    
    @field_validator(
        "entry_price", "stop_loss", "risk_reward_ratio",
        "take_profit_1", "take_profit_2", "take_profit_3",
        mode="before"
    )
    @classmethod
    def convert_optional_decimal_to_float(cls, v: Any) -> float | None:
        """Convert optional Decimal fields to float."""
        if v is None:
            return None
        if isinstance(v, Decimal):
            return float(v)
        return v


class EventCorrelationResponse(BaseModel):
    """
    Event correlation tracking response.
    
    Serializes EventCorrelation ORM model for API responses.
    """
    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=(),
        json_schema_extra={
            "example": {
                "correlation_id": "660e8400-e29b-41d4-a716-446655440001",
                "suggestion_id": "550e8400-e29b-41d4-a716-446655440000",
                "trigger_type": "SCANNER_ANOMALY",
                "trigger_timestamp": "2026-04-22T00:00:00+05:30",
                "scanner_response_ms": 45,
                "ai_response_ms": 120,
                "ml_response_ms": 85,
                "total_latency_ms": 250,
                "consensus_reached": True,
                "rejection_reason": None,
                "created_at": "2026-04-22T00:00:00+05:30"
            }
        }
    )
    
    correlation_id: UUID
    suggestion_id: UUID | None = Field(None, description="Linked suggestion (null if rejected)")
    trigger_type: TriggerType
    trigger_timestamp: datetime
    
    # Performance metrics
    scanner_response_ms: int | None = Field(None, ge=0, description="Scanner response time (ms)")
    ai_response_ms: int | None = Field(None, ge=0, description="AI response time (ms)")
    ml_response_ms: int | None = Field(None, ge=0, description="ML response time (ms)")
    total_latency_ms: int | None = Field(None, ge=0, description="Total correlation latency (ms)")
    
    # Consensus result
    consensus_reached: bool
    rejection_reason: str | None = Field(None, max_length=200)
    
    # Agent outputs (optional for debugging)
    scanner_output: dict[str, Any] | None = None
    ai_output: dict[str, Any] | None = None
    ml_output: dict[str, Any] | None = None
    
    created_at: datetime


class TradeSuggestionsListResponse(BaseModel):
    """
    Paginated list of trade suggestions.
    
    Standard response format for GET /suggestions endpoint.
    Supports both offset-based and cursor-based pagination.
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "suggestions": [],
                "total": 42,
                "page": 1,
                "page_size": 50,
                "has_more": False,
                "next_cursor": None,
                "estimated_total": 42
            }
        }
    )
    
    suggestions: list[TradeSuggestionResponse] = Field(description="List of suggestions")
    
    # Offset pagination (backward compatibility)
    total: int | None = Field(None, ge=0, description="Total matching suggestions (offset mode only)")
    page: int | None = Field(None, ge=1, description="Current page number (offset mode only)")
    page_size: int | None = Field(None, ge=1, le=100, description="Results per page (offset mode only)")
    
    # Cursor pagination
    next_cursor: str | None = Field(None, description="Cursor for next page (cursor mode)")
    has_more: bool = Field(description="Whether more pages exist")
    limit: int | None = Field(None, ge=1, le=100, description="Results limit (cursor mode)")
    estimated_total: int | None = Field(None, ge=0, description="Estimated total count (cursor mode)")


# ============================================================================
# Request/Filter Schemas
# ============================================================================

class SuggestionFilters(BaseModel):
    """
    Query filters for listing trade suggestions.
    
    Used as query parameters in GET /suggestions endpoint.
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "direction": "BUY",
                "confidence_level": "HIGH",
                "min_confidence": 80.0,
                "status": "active",
                "symbol": "RELIANCE",
                "page": 1,
                "page_size": 50
            }
        }
    )
    
    # Filters
    direction: SignalDirection | None = Field(None, description="Filter by signal direction")
    confidence_level: ConfidenceLevel | None = Field(None, description="Filter by confidence level")
    min_confidence: float | None = Field(None, ge=0.0, le=100.0, description="Minimum consensus score")
    status: SuggestionStatus | None = Field(None, description="Filter by status")
    symbol: str | None = Field(None, max_length=50, description="Filter by symbol")
    
    # Pagination
    page: int = Field(1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(50, ge=1, le=100, description="Results per page")
    
    @field_validator("symbol")
    @classmethod
    def uppercase_symbol(cls, v: str | None) -> str | None:
        """Normalize symbol to uppercase."""
        return v.upper() if v else None


class SuggestionDetailResponse(BaseModel):
    """
    Detailed suggestion response with correlation history.
    
    Used for GET /suggestions/{suggestion_id} endpoint.
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "suggestion": {},
                "correlations": [],
                "correlation_count": 1
            }
        }
    )
    
    suggestion: TradeSuggestionResponse
    correlations: list[EventCorrelationResponse] = Field(
        description="Related event correlations"
    )
    correlation_count: int = Field(ge=0, description="Total correlation events")


# ============================================================================
# Statistics Schemas
# ============================================================================

class SuggestionStatsResponse(BaseModel):
    """
    Aggregate statistics for trade suggestions.
    
    Used for monitoring and dashboard displays.
    """
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "total_active": 15,
                "total_today": 42,
                "avg_consensus_score": 78.5,
                "high_confidence_count": 8,
                "buy_count": 10,
                "sell_count": 5,
                "avg_latency_ms": 95.3,
                "consensus_rate": 0.85
            }
        }
    )
    
    # Suggestion counts
    total_active: int = Field(ge=0, description="Currently active suggestions")
    total_today: int = Field(ge=0, description="Suggestions generated today")
    
    # Quality metrics
    avg_consensus_score: float = Field(ge=0.0, le=100.0, description="Average consensus score")
    high_confidence_count: int = Field(ge=0, description="High confidence suggestions")
    
    # Direction distribution
    buy_count: int = Field(ge=0, description="BUY signals")
    sell_count: int = Field(ge=0, description="SELL signals")
    
    # Performance metrics
    avg_latency_ms: float = Field(ge=0, description="Average correlation latency")
    consensus_rate: float = Field(ge=0.0, le=1.0, description="Consensus success rate")
