"""
Pydantic schemas for pattern analysis API.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field


class PatternAnalysisRequest(BaseModel):
    """Request schema for pattern analysis."""
    
    instrument_key: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description="NSE instrument key (e.g., NSE_EQ|INE002A01018)",
        examples=["NSE_EQ|INE002A01018"],
    )
    timeframe: str = Field(
        "1D",
        pattern=r"^(1m|5m|15m|30m|1h|4h|1D|1W)$",
        description="Candle timeframe",
    )
    lookback_days: int = Field(
        365,
        ge=30,
        le=3650,
        description="Number of days to analyze (30-3650)",
    )


class PatternDetection(BaseModel):
    """Individual pattern detection result."""
    
    name: str = Field(..., description="Pattern name (e.g., HAMMER, DOJI)")
    timestamp: str = Field(..., description="ISO timestamp when pattern occurred")
    confidence: int = Field(..., ge=0, le=200, description="Pattern confidence (100 or 200)")
    direction: Literal["bullish", "bearish"] = Field(..., description="Pattern direction")


class HistoricalStats(BaseModel):
    """Historical accuracy statistics for a pattern."""
    
    sample_size: int = Field(..., ge=0, description="Number of historical predictions")
    accuracy: float = Field(..., ge=0.0, le=1.0, description="Directional accuracy (0.0-1.0)")
    avg_move_pct: float = Field(..., description="Average price movement percentage")
    tp1_hit_rate: float = Field(..., ge=0.0, le=1.0, description="TP1 hit rate (0.0-1.0)")
    tp2_hit_rate: float = Field(..., ge=0.0, le=1.0, description="TP2 hit rate (0.0-1.0)")
    tp3_hit_rate: float = Field(..., ge=0.0, le=1.0, description="TP3 hit rate (0.0-1.0)")
    sl_hit_rate: float = Field(..., ge=0.0, le=1.0, description="Stop loss hit rate (0.0-1.0)")


class PatternAnalysisResponse(BaseModel):
    """Response schema for pattern analysis."""

    patterns: list[PatternDetection] = Field(
        ...,
        description="List of detected patterns"
    )
    total_detected: int = Field(..., ge=0, description="Total number of patterns detected")
    timeframe: str = Field(..., description="Timeframe analyzed")
    analyzed_candles: int = Field(..., ge=0, description="Number of candles analyzed")
    best_pattern: Optional[PatternDetection] = Field(
        None,
        description="Highest-confidence pattern across all timeframes (auto_detect mode only)",
    )
    cache_tier: Optional[str] = Field(None, description="Cache tier used (L1, L2, MISS)")
    error: Optional[str] = Field(None, description="Error code if detection failed")
    error_message: Optional[str] = Field(None, description="Error message if detection failed")
    historical_accuracy: Optional[float] = Field(None, ge=0.0, le=1.0, description="Overall historical accuracy")
    historical_stats: Optional[HistoricalStats] = Field(None, description="Detailed historical statistics")
