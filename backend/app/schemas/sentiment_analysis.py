"""
Pydantic schemas for AI Sentiment Analysis API.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class TopEvent(BaseModel):
    """The single highest-impact news event in the lookback window."""

    title: str = Field(..., description="Article headline")
    sentiment: Literal["positive", "negative", "neutral"] = Field(
        ..., description="FinBERT-classified sentiment"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Classification confidence (0-1)")
    source: str = Field(..., description="Feed source name (e.g., 'Economic Times Markets')")
    published_at: Optional[str] = Field(None, description="ISO 8601 publish timestamp")
    impact_contribution: float = Field(
        ..., description="This event's contribution to the overall impact score"
    )


class SentimentBreakdown(BaseModel):
    """Count of positive / negative / neutral events in the lookback window."""

    positive: int = Field(..., ge=0)
    negative: int = Field(..., ge=0)
    neutral: int = Field(..., ge=0)
    total: int = Field(..., ge=0)
    positive_pct: float = Field(..., ge=0.0, le=100.0, description="% positive")
    negative_pct: float = Field(..., ge=0.0, le=100.0, description="% negative")
    neutral_pct: float = Field(..., ge=0.0, le=100.0, description="% neutral")


class SentimentAnalysisResponse(BaseModel):
    """Full sentiment analysis response for a single instrument."""

    instrument_key: str = Field(..., description="NSE instrument key")
    symbol: Optional[str] = Field(None, description="NSE trading symbol if resolved")

    # ── Sentiment metrics ──────────────────────────────────────────────────────
    breakdown: SentimentBreakdown = Field(..., description="Event count by sentiment label")
    impact_score: float = Field(
        ..., ge=-100.0, le=100.0,
        description="Aggregated sentiment score: -100 (very bearish) to +100 (very bullish)"
    )
    sentiment_label: Literal["bullish", "bearish", "neutral"] = Field(
        ..., description="Human-readable overall sentiment"
    )
    top_event: Optional[TopEvent] = Field(None, description="Highest-impact news event")

    # ── Query parameters reflected back ───────────────────────────────────────
    lookback_hours: int = Field(..., ge=1, le=168, description="Lookback window used")

    # ── Metadata ──────────────────────────────────────────────────────────────
    cache_tier: Optional[str] = Field(None, description="Cache tier used: L1, L2, or MISS")
    computed_at: str = Field(..., description="ISO 8601 timestamp when analysis was computed")

    # ── Error fields (populated only on failure) ──────────────────────────────
    error: Optional[str] = Field(None, description="Error code if analysis failed")
    error_message: Optional[str] = Field(None, description="Human-readable error detail")
