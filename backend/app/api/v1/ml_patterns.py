"""
ML Pattern Analysis API — Production-Grade Candlestick Pattern Detection
==========================================================================
Endpoints for real-time pattern detection with historical accuracy tracking.

Architecture:
  - Pattern detection via TA-Lib (61 candlestick patterns)
  - L1 (in-process) + L2 (Redis) caching for <10ms p50 latency
  - Historical accuracy from ml_prediction_outcomes table
  - Rate limiting: 60 req/min per user
  - Comprehensive error handling and observability

Performance targets:
  - p50 latency: <10ms (L1 cache hit)
  - p95 latency: <50ms (L2 cache hit)
  - p99 latency: <100ms (fresh computation)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Integer, cast, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.models.ml_data import MLPredictionOutcome
from app.schemas.pattern_analysis import (
    PatternAnalysisRequest,
    PatternAnalysisResponse,
    PatternDetection,
)
from app.services.pattern_detection_service import PatternDetectionService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ml", tags=["ML Pattern Analysis"])


@router.get(
    "/pattern-analysis",
    response_model=PatternAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Detect candlestick patterns with historical accuracy",
    description="""
    Analyzes OHLCV data to detect 61+ candlestick patterns using TA-Lib.
    
    Returns:
    - Detected patterns with confidence scores
    - Historical accuracy metrics (from past predictions)
    - Pattern-specific statistics (TP/SL hit rates, avg move %)
    
    Performance:
    - Cached results: <10ms response time
    - Fresh computation: <100ms response time
    - Rate limit: 60 requests/minute per user
    """,
    responses={
        200: {
            "description": "Pattern analysis completed successfully",
            "content": {
                "application/json": {
                    "example": {
                        "patterns": [
                            {
                                "name": "HAMMER",
                                "timestamp": "2026-05-11T10:30:00Z",
                                "confidence": 100,
                                "direction": "bullish",
                            }
                        ],
                        "total_detected": 1,
                        "timeframe": "1D",
                        "analyzed_candles": 235,
                        "cache_tier": "L1",
                        "historical_accuracy": 0.73,
                        "historical_stats": {
                            "sample_size": 127,
                            "avg_move_pct": 8.2,
                            "tp1_hit_rate": 0.68,
                            "tp2_hit_rate": 0.42,
                            "sl_hit_rate": 0.27,
                        },
                    }
                }
            },
        },
        400: {"description": "Invalid request parameters"},
        429: {"description": "Rate limit exceeded"},
        500: {"description": "Internal server error"},
        503: {"description": "Service temporarily unavailable"},
    },
)
@limiter.limit("60/minute")
async def get_pattern_analysis(
    request: Request,
    instrument_key: str,
    timeframe: str = "1D",
    lookback_days: int = 365,
    auto_detect: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Detect candlestick patterns and return with historical accuracy.

    Args:
        instrument_key: NSE instrument key (e.g., "NSE_EQ|INE002A01018")
        timeframe: Candle timeframe (e.g. "1D", "1hour") — ignored when auto_detect=True
        lookback_days: Number of days to analyze (30-3650)
        auto_detect: When True, scan ingested timeframes [1D, 1hour] and return the strongest signal
        db: Database session
        current_user: Authenticated user

    Returns:
        Pattern analysis with historical accuracy metrics
    """
    user_id = current_user.get("user_id", "unknown")

    if lookback_days < 30 or lookback_days > 3650:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_lookback_days",
                "message": "lookback_days must be between 30 and 3650",
            },
        )

    if not auto_detect and timeframe not in ["1m", "5m", "15m", "30m", "1h", "4h", "1D", "1W"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_timeframe",
                "message": f"Invalid timeframe: {timeframe}",
            },
        )

    try:
        redis = get_redis()
        pattern_service = PatternDetectionService(db=db, redis=redis)

        if auto_detect:
            detection_result = await pattern_service.detect_strongest_signal(
                instrument_key=instrument_key,
                lookback_days=lookback_days,
            )
        else:
            detection_result = await pattern_service.detect_patterns(
                instrument_key=instrument_key,
                timeframe=timeframe,
                lookback_days=lookback_days,
            )

        # Handle detection errors
        if detection_result.get("error"):
            error_code = detection_result["error"]

            if error_code == "insufficient_data":
                return PatternAnalysisResponse(
                    patterns=[],
                    total_detected=0,
                    timeframe=detection_result.get("timeframe", timeframe),
                    analyzed_candles=0,
                    best_pattern=None,
                    cache_tier=detection_result.get("cache_tier", "MISS"),
                    error="insufficient_data",
                    error_message="Not enough OHLCV data for pattern detection. Minimum 30 candles required.",
                    historical_accuracy=None,
                    historical_stats=None,
                ).model_dump()

            logger.warning(
                "Pattern detection error: user=%s instrument=%s error=%s message=%s",
                user_id, instrument_key, error_code, detection_result.get("error_message"),
            )
            return PatternAnalysisResponse(
                patterns=[],
                total_detected=0,
                timeframe=detection_result.get("timeframe", timeframe),
                analyzed_candles=detection_result.get("analyzed_candles", 0),
                best_pattern=None,
                cache_tier=detection_result.get("cache_tier", "ERROR"),
                error=error_code,
                error_message=detection_result.get("error_message"),
                historical_accuracy=None,
                historical_stats=None,
            ).model_dump()

        # Determine which pattern to use for historical stats
        # In auto_detect mode: use best_pattern (highest confidence across timeframes)
        # In normal mode: use most recent pattern in the returned list
        best_raw = detection_result.get("best_pattern")
        anchor_pattern = best_raw or (
            detection_result["patterns"][0] if detection_result["total_detected"] > 0 else None
        )
        effective_timeframe = detection_result["timeframe"]

        historical_accuracy = None
        historical_stats = None

        if anchor_pattern:
            historical_stats = await _get_pattern_historical_stats(
                db=db,
                pattern_name=anchor_pattern["name"],
                timeframe=effective_timeframe,
            )
            if historical_stats and historical_stats["sample_size"] > 0:
                historical_accuracy = historical_stats["accuracy"]

        logger.info(
            "Pattern analysis: user=%s instrument=%s timeframe=%s patterns=%d auto=%s cache=%s",
            user_id, instrument_key, effective_timeframe,
            detection_result["total_detected"], auto_detect,
            detection_result.get("cache_tier", "MISS"),
        )

        return PatternAnalysisResponse(
            patterns=[PatternDetection(**p) for p in detection_result["patterns"]],
            total_detected=detection_result["total_detected"],
            timeframe=effective_timeframe,
            analyzed_candles=detection_result["analyzed_candles"],
            best_pattern=PatternDetection(**best_raw) if best_raw else None,
            cache_tier=detection_result.get("cache_tier"),
            error=None,
            error_message=None,
            historical_accuracy=historical_accuracy,
            historical_stats=historical_stats,
        ).model_dump()
    
    except HTTPException:
        raise
    
    except Exception as exc:
        logger.error(
            f"Pattern analysis failed: user={user_id} instrument={instrument_key} "
            f"error={exc}",
            exc_info=True,
        )
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "pattern_analysis_failed",
                "message": "Internal server error during pattern analysis",
            },
        )


async def _get_pattern_historical_stats(
    db: AsyncSession,
    pattern_name: str,
    timeframe: str,
) -> dict[str, Any] | None:
    """
    Query historical accuracy statistics for a specific pattern.
    
    Computes:
    - Overall accuracy (% of predictions where direction was correct)
    - Average price movement (%)
    - TP1/TP2/TP3/SL hit rates
    - Sample size
    
    Args:
        db: Database session
        pattern_name: Pattern name (e.g., "HAMMER", "DOJI")
        timeframe: Timeframe (e.g., "1D", "4H")
        
    Returns:
        Dictionary with historical statistics or None if no data
    """
    try:
        # Query ml_prediction_outcomes for pattern-specific stats
        # Only include completed outcomes (SUCCESS or FAILURE)
        result = await db.execute(
            select(
                func.count(MLPredictionOutcome.id).label("sample_size"),
                func.avg(cast(MLPredictionOutcome.ml_direction_correct, Integer)).label("accuracy"),
                func.avg(MLPredictionOutcome.final_move_pct).label("avg_move_pct"),
                func.avg(cast(MLPredictionOutcome.hit_predicted_tp1, Integer)).label("tp1_hit_rate"),
                func.avg(cast(MLPredictionOutcome.hit_predicted_tp2, Integer)).label("tp2_hit_rate"),
                func.avg(cast(MLPredictionOutcome.hit_predicted_tp3, Integer)).label("tp3_hit_rate"),
                func.avg(cast(MLPredictionOutcome.hit_predicted_sl, Integer)).label("sl_hit_rate"),
            )
            .where(
                MLPredictionOutcome.pattern_name == pattern_name,
                MLPredictionOutcome.pattern_timeframe == timeframe,
                MLPredictionOutcome.outcome_status.in_(["SUCCESS", "FAILURE"]),
                MLPredictionOutcome.confidence_level == "HIGH",
            )
        )
        
        row = result.one()
        
        if row.sample_size == 0:
            return None
        
        return {
            "sample_size": int(row.sample_size),
            "accuracy": float(row.accuracy) if row.accuracy else 0.0,
            "avg_move_pct": float(row.avg_move_pct) if row.avg_move_pct else 0.0,
            "tp1_hit_rate": float(row.tp1_hit_rate) if row.tp1_hit_rate else 0.0,
            "tp2_hit_rate": float(row.tp2_hit_rate) if row.tp2_hit_rate else 0.0,
            "tp3_hit_rate": float(row.tp3_hit_rate) if row.tp3_hit_rate else 0.0,
            "sl_hit_rate": float(row.sl_hit_rate) if row.sl_hit_rate else 0.0,
        }
    
    except Exception as exc:
        logger.warning(
            f"Failed to fetch historical stats: pattern={pattern_name} "
            f"timeframe={timeframe} error={exc}"
        )
        return None


@router.get(
    "/pattern-accuracy/{pattern_name}",
    status_code=status.HTTP_200_OK,
    summary="Get historical accuracy for a specific pattern",
    description="""
    Returns detailed historical accuracy metrics for a specific candlestick pattern.
    
    Useful for:
    - Backtesting pattern reliability
    - Confidence calibration
    - Pattern selection for trading strategies
    """,
)
@limiter.limit("60/minute")
async def get_pattern_accuracy(
    request: Request,
    pattern_name: str,
    timeframe: str = "1D",
    confidence_level: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Get historical accuracy statistics for a specific pattern.
    
    Args:
        pattern_name: Pattern name (e.g., "HAMMER", "DOJI")
        timeframe: Timeframe filter (optional)
        confidence_level: Filter by confidence (HIGH, MEDIUM, LOW)
        db: Database session
        current_user: Authenticated user
        
    Returns:
        Historical accuracy statistics
    """
    try:
        # Build query
        query = select(
            func.count(MLPredictionOutcome.id).label("total_predictions"),
            func.avg(cast(MLPredictionOutcome.ml_direction_correct, Integer)).label("accuracy"),
            func.avg(MLPredictionOutcome.final_move_pct).label("avg_move_pct"),
            func.avg(MLPredictionOutcome.max_favorable_move_pct).label("avg_best_move"),
            func.avg(MLPredictionOutcome.max_adverse_move_pct).label("avg_worst_move"),
            func.avg(cast(MLPredictionOutcome.hit_predicted_tp1, Integer)).label("tp1_hit_rate"),
            func.avg(cast(MLPredictionOutcome.hit_predicted_tp2, Integer)).label("tp2_hit_rate"),
            func.avg(cast(MLPredictionOutcome.hit_predicted_tp3, Integer)).label("tp3_hit_rate"),
            func.avg(cast(MLPredictionOutcome.hit_predicted_sl, Integer)).label("sl_hit_rate"),
        ).where(
            MLPredictionOutcome.pattern_name == pattern_name,
            MLPredictionOutcome.outcome_status.in_(["SUCCESS", "FAILURE"]),
        )
        
        if timeframe:
            query = query.where(MLPredictionOutcome.pattern_timeframe == timeframe)
        
        if confidence_level:
            query = query.where(MLPredictionOutcome.confidence_level == confidence_level.upper())
        
        result = await db.execute(query)
        row = result.one()
        
        if row.total_predictions == 0:
            return {
                "pattern_name": pattern_name,
                "timeframe": timeframe,
                "confidence_level": confidence_level,
                "total_predictions": 0,
                "message": "No historical data available for this pattern",
            }
        
        return {
            "pattern_name": pattern_name,
            "timeframe": timeframe,
            "confidence_level": confidence_level,
            "total_predictions": int(row.total_predictions),
            "accuracy": float(row.accuracy) if row.accuracy else 0.0,
            "avg_move_pct": float(row.avg_move_pct) if row.avg_move_pct else 0.0,
            "avg_best_move": float(row.avg_best_move) if row.avg_best_move else 0.0,
            "avg_worst_move": float(row.avg_worst_move) if row.avg_worst_move else 0.0,
            "tp1_hit_rate": float(row.tp1_hit_rate) if row.tp1_hit_rate else 0.0,
            "tp2_hit_rate": float(row.tp2_hit_rate) if row.tp2_hit_rate else 0.0,
            "tp3_hit_rate": float(row.tp3_hit_rate) if row.tp3_hit_rate else 0.0,
            "sl_hit_rate": float(row.sl_hit_rate) if row.sl_hit_rate else 0.0,
        }
    
    except Exception as exc:
        logger.error(
            f"Failed to fetch pattern accuracy: pattern={pattern_name} error={exc}",
            exc_info=True,
        )
        
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "query_failed",
                "message": "Failed to fetch pattern accuracy statistics",
            },
        )
