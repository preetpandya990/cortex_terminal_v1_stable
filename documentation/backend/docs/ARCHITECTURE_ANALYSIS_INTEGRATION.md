# Cortex AI - Architecture Analysis Part 4: Integration Blueprint
**Date**: 2026-05-11  
**Part**: 4 of 4 - Pattern Detection Service Integration Blueprint

---

## 15. Pattern Detection Service Integration

### 15.1 Service Architecture

**File**: `backend/app/services/pattern_detection_service.py`

**Blueprint**: Following existing CandleService pattern

```python
"""
Pattern Detection Service — TA-Lib Integration
===============================================
Production-grade candlestick pattern detection with caching and async support.

Architecture:
  1. Fetch OHLCV data via CandleService (DB-first)
  2. Detect patterns using TA-Lib (async wrapper)
  3. Cache results in Redis (5-min TTL)
  4. Return structured pattern data

Benefits:
  - 61 candlestick patterns (vs 3 in POC)
  - Sub-millisecond detection (0.16ms for 10 patterns)
  - Redis caching for performance
  - Async/await for non-blocking execution
"""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import numpy as np
import talib
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.candle_service import CandleService

logger = logging.getLogger(__name__)


class PatternDetectionService:
    """
    Service for detecting candlestick patterns using TA-Lib.
    
    Features:
    - 61 candlestick patterns
    - Async/await support
    - Redis caching (5-min TTL)
    - Confidence scoring
    - Multi-timeframe support
    """
    
    # All 61 TA-Lib candlestick patterns
    PATTERNS = [
        "CDL2CROWS", "CDL3BLACKCROWS", "CDL3INSIDE", "CDL3LINESTRIKE",
        "CDL3OUTSIDE", "CDL3STARSINSOUTH", "CDL3WHITESOLDIERS",
        "CDLABANDONEDBABY", "CDLADVANCEBLOCK", "CDLBELTHOLD",
        "CDLBREAKAWAY", "CDLCLOSINGMARUBOZU", "CDLCONCEALBABYSWALL",
        "CDLCOUNTERATTACK", "CDLDARKCLOUDCOVER", "CDLDOJI",
        "CDLDOJISTAR", "CDLDRAGONFLYDOJI", "CDLENGULFING",
        "CDLEVENINGDOJISTAR", "CDLEVENINGSTAR", "CDLGAPSIDESIDEWHITE",
        "CDLGRAVESTONEDOJI", "CDLHAMMER", "CDLHANGINGMAN",
        "CDLHARAMI", "CDLHARAMICROSS", "CDLHIGHWAVE",
        "CDLHIKKAKE", "CDLHIKKAKEMOD", "CDLHOMINGPIGEON",
        "CDLIDENTICAL3CROWS", "CDLINNECK", "CDLINVERTEDHAMMER",
        "CDLKICKING", "CDLKICKINGBYLENGTH", "CDLLADDERBOTTOM",
        "CDLLONGLEGGEDDOJI", "CDLLONGLINE", "CDLMARUBOZU",
        "CDLMATCHINGLOW", "CDLMATHOLD", "CDLMORNINGDOJISTAR",
        "CDLMORNINGSTAR", "CDLONNECK", "CDLPIERCING",
        "CDLRICKSHAWMAN", "CDLRISEFALL3METHODS", "CDLSEPARATINGLINES",
        "CDLSHOOTINGSTAR", "CDLSHORTLINE", "CDLSPINNINGTOP",
        "CDLSTALLEDPATTERN", "CDLSTICKSANDWICH", "CDLTAKURI",
        "CDLTASUKIGAP", "CDLTHRUSTING", "CDLTRISTAR",
        "CDLUNIQUE3RIVER", "CDLUPSIDEGAP2CROWS", "CDLXSIDEGAP3METHODS",
    ]
    
    def __init__(self, db: AsyncSession, redis: Redis | None = None):
        self.db = db
        self.redis = redis
        self.candle_service = CandleService()
    
    async def detect_patterns(
        self,
        instrument_key: str,
        timeframe: str = "1D",
        lookback_days: int = 365,
    ) -> dict[str, Any]:
        """
        Detect all candlestick patterns for an instrument.
        
        Args:
            instrument_key: NSE instrument key
            timeframe: Candle timeframe (1D, 1H, 5m, etc.)
            lookback_days: Number of days to analyze
        
        Returns:
            dict: {
                "patterns": [...],
                "total_detected": 15,
                "timeframe": "1D",
                "analyzed_candles": 235,
            }
        """
        # Check cache
        if self.redis:
            cached = await self._get_cached(instrument_key, timeframe)
            if cached:
                logger.info(f"Cache hit: patterns:{instrument_key}:{timeframe}")
                return cached
        
        # Fetch OHLCV data
        ohlcv = await self._fetch_ohlcv(instrument_key, timeframe, lookback_days)
        
        if not ohlcv or len(ohlcv["close"]) < 30:
            return {
                "patterns": [],
                "total_detected": 0,
                "timeframe": timeframe,
                "analyzed_candles": 0,
                "error": "insufficient_data",
            }
        
        # Detect patterns (offload to thread pool)
        patterns = await asyncio.to_thread(
            self._detect_patterns_sync,
            ohlcv["open"],
            ohlcv["high"],
            ohlcv["low"],
            ohlcv["close"],
            ohlcv["timestamps"],
        )
        
        result = {
            "patterns": patterns,
            "total_detected": len(patterns),
            "timeframe": timeframe,
            "analyzed_candles": len(ohlcv["close"]),
        }
        
        # Cache result (5-min TTL)
        if self.redis:
            await self._set_cached(instrument_key, timeframe, result, ttl=300)
        
        return result
    
    def _detect_patterns_sync(
        self,
        open_prices: np.ndarray,
        high_prices: np.ndarray,
        low_prices: np.ndarray,
        close_prices: np.ndarray,
        timestamps: list[str],
    ) -> list[dict]:
        """
        Synchronous pattern detection (runs in thread pool).
        """
        detected = []
        
        for pattern_name in self.PATTERNS:
            pattern_func = getattr(talib, pattern_name)
            result = pattern_func(open_prices, high_prices, low_prices, close_prices)
            
            # Find non-zero values (pattern detected)
            indices = np.where(result != 0)[0]
            
            for idx in indices:
                detected.append({
                    "name": pattern_name.replace("CDL", ""),
                    "timestamp": timestamps[idx],
                    "confidence": abs(int(result[idx])),
                    "direction": "bullish" if result[idx] > 0 else "bearish",
                })
        
        # Sort by timestamp (most recent first)
        detected.sort(key=lambda x: x["timestamp"], reverse=True)
        
        return detected
    
    async def _fetch_ohlcv(
        self,
        instrument_key: str,
        timeframe: str,
        lookback_days: int,
    ) -> dict[str, np.ndarray] | None:
        """Fetch OHLCV data from CandleService."""
        # Implementation uses existing CandleService
        pass
    
    async def _get_cached(
        self,
        instrument_key: str,
        timeframe: str,
    ) -> dict | None:
        """Get cached patterns."""
        cache_key = f"pattern:{instrument_key}:{timeframe}"
        cached = await self.redis.get(cache_key)
        if cached:
            return json.loads(cached)
        return None
    
    async def _set_cached(
        self,
        instrument_key: str,
        timeframe: str,
        data: dict,
        ttl: int = 300,
    ):
        """Cache patterns with TTL."""
        cache_key = f"pattern:{instrument_key}:{timeframe}"
        await self.redis.setex(cache_key, ttl, json.dumps(data))
```

**Key Design Decisions**:
- ✅ **Follows CandleService pattern**: Same structure and conventions
- ✅ **Async wrapper**: `asyncio.to_thread()` for TA-Lib calls
- ✅ **Redis caching**: 5-min TTL for performance
- ✅ **Error handling**: Graceful degradation on insufficient data
- ✅ **Logging**: Structured logs with context

---

### 15.2 API Endpoint

**File**: `backend/app/api/v1/pattern_analysis.py`

**Blueprint**: Following ml_predictions.py pattern

```python
"""
Pattern Analysis API Endpoints
===============================
Candlestick pattern detection powered by TA-Lib.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.core.redis import get_redis
from app.services.pattern_detection_service import PatternDetectionService
from app.schemas.pattern_analysis import (
    PatternAnalysisRequest,
    PatternAnalysisResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Pattern Analysis"])


@router.post(
    "/pattern-analysis",
    response_model=PatternAnalysisResponse,
    status_code=status.HTTP_200_OK,
    summary="Detect candlestick patterns for an NSE instrument",
)
@limiter.limit("100/minute")
async def analyze_patterns(
    request: Request,
    body: PatternAnalysisRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> PatternAnalysisResponse:
    """
    Detect all candlestick patterns using TA-Lib (61 patterns).
    
    Returns patterns with timestamps, confidence scores, and direction.
    """
    user_id = current_user.get("user_id", "unknown")
    instrument_key = body.instrument_key
    timeframe = body.timeframe
    
    try:
        redis = await get_redis()
        service = PatternDetectionService(db=db, redis=redis)
        
        result = await service.detect_patterns(
            instrument_key=instrument_key,
            timeframe=timeframe,
            lookback_days=body.lookback_days,
        )
        
        logger.info(
            "Pattern analysis: user=%s instrument=%s patterns=%d",
            user_id, instrument_key, result["total_detected"],
        )
        
        return PatternAnalysisResponse(**result)
    
    except Exception as exc:
        logger.error(
            "Pattern analysis failed: user=%s instrument=%s error=%s",
            user_id, instrument_key, exc, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "pattern_analysis_failed", "message": str(exc)},
        )
```

**Key Design Decisions**:
- ✅ **Same structure as ml_predictions**: Consistent API design
- ✅ **Rate limiting**: 100/minute like other endpoints
- ✅ **Authentication**: Required via get_current_user
- ✅ **Error handling**: Comprehensive try-except with logging
- ✅ **User context**: User ID in all logs

---

### 15.3 Request/Response Schemas

**File**: `backend/app/schemas/pattern_analysis.py`

```python
"""
Pydantic schemas for pattern analysis.
"""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class PatternAnalysisRequest(BaseModel):
    """Request schema for pattern analysis."""
    instrument_key: str = Field(..., min_length=1, max_length=100)
    timeframe: str = Field("1D", pattern="^(1m|5m|15m|1h|4h|1D|1W)$")
    lookback_days: int = Field(365, ge=30, le=3650)


class PatternDetection(BaseModel):
    """Individual pattern detection."""
    name: str
    timestamp: str
    confidence: int = Field(..., ge=0, le=200)
    direction: str = Field(..., pattern="^(bullish|bearish)$")


class PatternAnalysisResponse(BaseModel):
    """Response schema for pattern analysis."""
    patterns: list[PatternDetection]
    total_detected: int
    timeframe: str
    analyzed_candles: int
    error: Optional[str] = None
```

**Key Design Decisions**:
- ✅ **Validation**: Field constraints enforced
- ✅ **Type safety**: Full type annotations
- ✅ **Nested models**: PatternDetection for individual patterns
- ✅ **Optional error**: Graceful degradation support

---

## 16. Integration Checklist

### Phase 1: Service Implementation
- [ ] Create `backend/app/services/pattern_detection_service.py`
- [ ] Implement `PatternDetectionService` class
- [ ] Add all 61 TA-Lib patterns
- [ ] Implement async wrapper with `asyncio.to_thread()`
- [ ] Add Redis caching (5-min TTL)
- [ ] Integrate with CandleService for OHLCV data
- [ ] Add comprehensive error handling
- [ ] Add structured logging

### Phase 2: API Endpoint
- [ ] Create `backend/app/api/v1/pattern_analysis.py`
- [ ] Implement `/pattern-analysis` endpoint
- [ ] Add rate limiting (100/minute)
- [ ] Add authentication (get_current_user)
- [ ] Add error handling with user context
- [ ] Create request/response schemas
- [ ] Add OpenAPI documentation

### Phase 3: Testing
- [ ] Create `backend/tests/unit/test_pattern_detection_service.py`
- [ ] Test pattern detection with mock data
- [ ] Test caching behavior
- [ ] Test error handling
- [ ] Create `backend/tests/api/test_pattern_analysis.py`
- [ ] Test endpoint with async_client
- [ ] Test authentication
- [ ] Test rate limiting

### Phase 4: Integration
- [ ] Register router in `backend/app/main.py`
- [ ] Add to API documentation
- [ ] Update requirements.txt (TA-Lib==0.6.8)
- [ ] Run full test suite
- [ ] Performance testing (load test)

---

## 17. Code Quality Standards

### 17.1 Mandatory Requirements

**Every file must have**:
- ✅ Module docstring explaining purpose
- ✅ Type hints on all functions/methods
- ✅ Docstrings with Args/Returns sections
- ✅ Structured logging with context
- ✅ Comprehensive error handling
- ✅ No hardcoded values (use settings)

**Example**:
```python
"""
Module description here.
"""
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def my_function(
    param1: str,
    param2: int,
) -> dict[str, Any]:
    """
    Function description.
    
    Args:
        param1: Description of param1
        param2: Description of param2
    
    Returns:
        Dictionary with results
    
    Raises:
        ValueError: If param2 is negative
    """
    try:
        # Implementation
        logger.info("Operation: param1=%s param2=%d", param1, param2)
        return {"result": "success"}
    except Exception as exc:
        logger.error("Operation failed: %s", exc, exc_info=True)
        raise
```

### 17.2 Performance Requirements

- ✅ **Latency**: <100ms p95 for pattern detection
- ✅ **Throughput**: >100 requests/second
- ✅ **Cache hit rate**: >80% after warmup
- ✅ **Memory**: <100MB per request
- ✅ **CPU**: Non-blocking (async/await)

### 17.3 Security Requirements

- ✅ **Authentication**: JWT required on all endpoints
- ✅ **Rate limiting**: Applied to prevent abuse
- ✅ **Input validation**: Pydantic schemas
- ✅ **SQL injection**: Parameterized queries only
- ✅ **Error messages**: No internal details leaked

---

## 18. Summary

### Architecture Patterns Identified

1. **Database Layer**:
   - Dual-engine architecture (API + worker)
   - Async session management with auto-rollback
   - Connection pooling with health checks

2. **Service Layer**:
   - Stateless classes with dependency injection
   - DB-first fetching strategy
   - Comprehensive error handling
   - Async/await for I/O operations

3. **API Layer**:
   - FastAPI routers with tags
   - Rate limiting via decorator
   - JWT authentication
   - Pydantic request/response schemas
   - Structured error responses

4. **Caching Layer**:
   - Redis with connection pooling
   - Namespaced cache keys
   - TTL-based expiry
   - Pub/Sub for real-time events

5. **Testing Infrastructure**:
   - Pytest with async support
   - Real database with transaction rollback
   - Dependency overrides for isolation
   - Comprehensive fixtures

### Integration Strategy

**Follow existing patterns exactly**:
- ✅ Use same file structure
- ✅ Use same naming conventions
- ✅ Use same error handling
- ✅ Use same logging format
- ✅ Use same testing approach

**No new patterns without justification**:
- ❌ Don't introduce new libraries
- ❌ Don't create new patterns
- ❌ Don't deviate from conventions

**Quality gates before deployment**:
- ✅ All tests passing
- ✅ Code review completed
- ✅ Performance benchmarks met
- ✅ Documentation complete

---

**Task 3 Complete**: Comprehensive architecture analysis delivered

**Next**: Task 4 - Design production-grade pattern detection service architecture
