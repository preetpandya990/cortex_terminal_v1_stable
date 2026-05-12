"""
AI Sentiment Analysis API
=========================
Production-grade endpoint for financial news sentiment analysis.

Endpoint:
  GET /api/v1/ai/sentiment

Features:
  - FinBERT ONNX GPU inference (18-22ms per article)
  - L1 + L2 (Redis, 2-min TTL) caching
  - Rate limiting: 60 req/min per user
  - JWT authentication
  - Graceful degradation: returns neutral on failure

Performance targets:
  - p50: <15ms  (L1 cache hit)
  - p95: <50ms  (L2 cache hit)
  - p99: <500ms (fresh inference on 50 events)
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.schemas.sentiment_analysis import SentimentAnalysisResponse
from app.services.sentiment_analysis_service import SentimentAnalysisService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["AI Sentiment Analysis"])


@router.get(
    "/sentiment",
    response_model=SentimentAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Analyse financial news sentiment for an instrument",
    description="""
    Fetches recent news from configured RSS + NSE/BSE feeds, runs FinBERT
    ONNX GPU inference on each headline, and returns an aggregated impact score.

    **Impact score**: -100 (very bearish) to +100 (very bullish).
    Weighted by event recency and source credibility (exchange feeds > news sites).

    **Cache**: Results cached 2 minutes in Redis (L2) and in-process (L1).
    **Rate limit**: 60 requests/minute per user.
    """,
    responses={
        200: {"description": "Sentiment analysis completed successfully"},
        400: {"description": "Invalid request parameters"},
        401: {"description": "Authentication required"},
        429: {"description": "Rate limit exceeded"},
        500: {"description": "Internal server error"},
    },
)
@limiter.limit("60/minute")
async def get_sentiment_analysis(
    request: Request,
    instrument_key: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="NSE instrument key (e.g. NSE_EQ|INE002A01018)",
        examples=["NSE_EQ|INE002A01018"],
    ),
    symbol: str | None = Query(
        None,
        min_length=1,
        max_length=20,
        description="NSE trading symbol for news filtering (e.g. RELIANCE). "
                    "If omitted, general market sentiment is returned.",
        examples=["RELIANCE"],
    ),
    lookback_hours: int = Query(
        24,
        ge=1,
        le=168,
        description="Lookback window in hours (1-168). Default 24h.",
    ),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> dict[str, Any]:
    """
    Return aggregated news sentiment for an instrument.

    Uses FinBERT ONNX GPU for sentiment classification + recency/credibility
    weighting to compute a -100 to +100 impact score.
    """
    user_id = current_user.get("user_id", "unknown")

    try:
        redis = get_redis()
        service = SentimentAnalysisService(db=db, redis=redis)
        result = await service.analyze(
            instrument_key=instrument_key,
            symbol=symbol,
            lookback_hours=lookback_hours,
        )

        logger.info(
            "Sentiment analysis: user=%s instrument=%s symbol=%s "
            "events=%d impact=%.1f cache=%s",
            user_id, instrument_key, symbol,
            result.breakdown.total, result.impact_score, result.cache_tier,
        )

        return result.model_dump()

    except Exception as exc:
        logger.error(
            "Sentiment analysis failed: user=%s instrument=%s error=%s",
            user_id, instrument_key, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "sentiment_analysis_failed",
                "message": "Internal server error during sentiment analysis",
            },
        )
