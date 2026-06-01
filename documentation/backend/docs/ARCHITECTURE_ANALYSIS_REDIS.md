# Cortex AI - Architecture Analysis Part 3: Redis & Testing
**Date**: 2026-05-11  
**Part**: 3 of 4 - Redis Caching, Pub/Sub, Testing Infrastructure

---

## 11. Redis Architecture

### 11.1 Redis Client Pattern

**File**: `backend/app/core/redis.py`

**Pattern**: Singleton with connection pooling

```python
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool

# Singleton state
_pool: ConnectionPool | None = None
_client: Redis | None = None


async def init_redis():
    """Initialize Redis connection pool."""
    global _pool, _client
    
    _pool = ConnectionPool.from_url(
        str(settings.REDIS_URL),
        max_connections=settings.REDIS_MAX_CONNECTIONS,
        decode_responses=True,  # Auto-decode bytes to strings
    )
    _client = Redis(connection_pool=_pool)


async def get_redis() -> Redis:
    """Get Redis client instance."""
    if _client is None:
        await init_redis()
    return _client


async def close_redis():
    """Close Redis connection pool."""
    global _pool, _client
    if _client:
        await _client.close()
    if _pool:
        await _pool.disconnect()
```

**Key Insights**:
- ✅ **Connection pooling**: Reuses connections efficiently
- ✅ **Singleton pattern**: One pool per application
- ✅ **Async support**: Full async/await compatibility
- ✅ **Graceful shutdown**: Cleanup on application exit

### 11.2 Caching Pattern

**Pattern**: Get-or-fetch with TTL

```python
async def get_prediction_cached(
    redis: Redis,
    symbol: str,
    timeframe: str,
) -> dict | None:
    """Get cached prediction or return None."""
    cache_key = f"prediction:{symbol}:{timeframe}"
    
    # Try cache first
    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)
    
    return None


async def set_prediction_cache(
    redis: Redis,
    symbol: str,
    timeframe: str,
    prediction: dict,
    ttl: int = 300,  # 5 minutes
):
    """Cache prediction with TTL."""
    cache_key = f"prediction:{symbol}:{timeframe}"
    await redis.setex(
        cache_key,
        ttl,
        json.dumps(prediction),
    )
```

**Key Insights**:
- ✅ **Namespaced keys**: Prefix with domain (prediction:, pattern:, etc.)
- ✅ **TTL-based expiry**: Automatic cache invalidation
- ✅ **JSON serialization**: Structured data storage
- ✅ **Null handling**: Return None if not cached

### 11.3 Cache Key Patterns

**Standard Naming Convention**:
```python
# Predictions
f"prediction:{symbol}:{timeframe}"           # ML predictions
f"pattern:{symbol}:{timeframe}"              # Pattern detection
f"sentiment:{symbol}"                        # Sentiment analysis

# Features
f"features:{symbol}:{timeframe}"             # Feature vectors

# User data
f"user:{user_id}:preferences"                # User preferences
f"user:{user_id}:watchlist"                  # User watchlist

# System state
f"model:active"                              # Active model metadata
f"regime:{symbol}"                           # Market regime
```

**Key Insights**:
- ✅ **Hierarchical**: Colon-separated namespaces
- ✅ **Predictable**: Easy to construct and debug
- ✅ **Scannable**: Can use SCAN with patterns

### 11.4 Pub/Sub Pattern

**File**: `backend/app/core/redis.py`

**Pattern**: Channel-based messaging

```python
class RedisChannels:
    """Redis pub/sub channel constants."""
    
    # Trading Signals
    SIGNALS_SYMBOL = "cai:signals:{symbol}"
    SIGNALS_ALL = "cai:signals:all"
    
    # Market Regime
    REGIME_SYMBOL = "cai:regime:{symbol}"
    REGIME_ALL = "cai:regime:all"
    
    # Trade Suggestions
    SUGGESTIONS_NEW = "cai:suggestions:new"
    SUGGESTIONS_EXPIRED = "cai:suggestions:expired"
    
    # Pattern Detection (NEW)
    PATTERNS_DETECTED = "cai:patterns:detected"
    PATTERNS_SYMBOL = "cai:patterns:{symbol}"


# Publishing
async def publish_pattern_detection(
    redis: Redis,
    symbol: str,
    patterns: list[dict],
):
    """Publish pattern detection event."""
    message = {
        "symbol": symbol,
        "patterns": patterns,
        "timestamp": datetime.utcnow().isoformat(),
    }
    
    await redis.publish(
        RedisChannels.PATTERNS_DETECTED,
        json.dumps(message),
    )


# Subscribing
async def subscribe_to_patterns(redis: Redis):
    """Subscribe to pattern detection events."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(RedisChannels.PATTERNS_DETECTED)
    
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            # Process pattern detection event
```

**Key Insights**:
- ✅ **Fire-and-forget**: No persistence, at-most-once delivery
- ✅ **Real-time**: Instant notification to subscribers
- ✅ **Decoupled**: Publishers don't know about subscribers
- ✅ **Channel naming**: Consistent with cache keys

---

## 12. Testing Infrastructure

### 12.1 Test Configuration

**File**: `backend/tests/conftest.py`

**Pattern**: Pytest fixtures with async support

```python
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

@pytest.fixture
async def db_session() -> AsyncGenerator:
    """
    Provide async database session with transaction rollback.
    
    Production-grade approach:
    - Uses actual PostgreSQL (not SQLite)
    - Each test runs in a transaction that rolls back
    - Uses NullPool to avoid event loop conflicts
    """
    from app.core.config import get_settings
    from app.core.database import Base
    
    settings = get_settings()
    
    # Create test engine with NullPool
    test_engine = create_async_engine(
        str(settings.DATABASE_URL),
        poolclass=NullPool,  # Prevents "different loop" errors
        echo=False,
    )
    
    async with test_engine.connect() as conn:
        trans = await conn.begin()
        
        # Create session bound to transaction
        session = AsyncSession(
            bind=conn,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint"
        )
        
        # Ensure tables exist
        await conn.run_sync(Base.metadata.create_all)
        
        yield session
        
        # Rollback transaction (cleanup)
        await session.close()
        await trans.rollback()
    
    await test_engine.dispose()
```

**Key Insights**:
- ✅ **Real database**: Uses actual PostgreSQL, not SQLite
- ✅ **Transaction isolation**: Each test rolls back
- ✅ **NullPool**: Prevents async event loop conflicts
- ✅ **Automatic cleanup**: Rollback ensures no test pollution

### 12.2 HTTP Client Fixture

**Pattern**: Test client with dependency overrides

```python
@pytest.fixture
async def async_client(db_session: AsyncSession) -> AsyncGenerator:
    """Provide HTTP test client with auth bypassed."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.api.deps import get_current_user_id, get_db

    # Override auth to return test user
    def override_auth():
        return "test-user-id"
    
    # Override database to use test session
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_current_user_id] = override_auth
    app.dependency_overrides[get_db] = override_get_db

    # Create client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Clean up overrides
    app.dependency_overrides.clear()
```

**Key Insights**:
- ✅ **Dependency override**: Bypasses auth for testing
- ✅ **Test database**: Uses test session
- ✅ **Cleanup**: Clears overrides after test
- ✅ **Async support**: Full async/await in tests

### 12.3 Test Structure

**Pattern**: Arrange-Act-Assert with async

```python
@pytest.mark.asyncio
async def test_predict_success(async_client, db_session):
    """Test successful prediction."""
    # Arrange
    symbol = "NSE_EQ|INE002A01018"
    request_data = {
        "symbol": symbol,
        "timeframe": "1D",
    }
    
    # Act
    response = await async_client.post(
        "/api/v1/ml/predict",
        json=request_data,
    )
    
    # Assert
    assert response.status_code == 200
    data = response.json()
    assert data["symbol"] == symbol
    assert data["available"] is True
    assert "direction" in data
    assert "confidence" in data
```

**Key Insights**:
- ✅ **Async tests**: `@pytest.mark.asyncio` decorator
- ✅ **Clear structure**: Arrange-Act-Assert pattern
- ✅ **Comprehensive assertions**: Validate all response fields
- ✅ **Fixtures**: Reuse db_session and async_client

### 12.4 Mock Pattern

**Pattern**: AsyncMock for async dependencies

```python
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_predict_with_mock():
    """Test prediction with mocked predictor."""
    # Mock predictor
    mock_predictor = AsyncMock()
    mock_predictor.predict.return_value = {
        "direction_label": "BUY",
        "confidence": 0.85,
        "entry_price": 2850.0,
        # ...
    }
    
    # Override dependency
    app.dependency_overrides[get_ml_predictor] = lambda: mock_predictor
    
    # Test endpoint
    response = await async_client.post("/api/v1/ml/predict", ...)
    
    # Verify mock was called
    mock_predictor.predict.assert_called_once()
```

**Key Insights**:
- ✅ **AsyncMock**: For async methods
- ✅ **Return values**: Configure mock responses
- ✅ **Verification**: Assert mock was called correctly

---

## 13. Performance Patterns

### 13.1 Async/Await Best Practices

**Pattern**: Offload CPU-bound work to thread pool

```python
import asyncio

async def detect_patterns_async(
    open_prices: np.ndarray,
    high_prices: np.ndarray,
    low_prices: np.ndarray,
    close_prices: np.ndarray,
) -> dict:
    """Async wrapper for CPU-bound TA-Lib calls."""
    def _detect():
        # CPU-bound TA-Lib calls
        return {
            'DOJI': talib.CDLDOJI(open_prices, high_prices, low_prices, close_prices),
            'HAMMER': talib.CDLHAMMER(open_prices, high_prices, low_prices, close_prices),
            # ... more patterns
        }
    
    # Run in thread pool (non-blocking)
    return await asyncio.to_thread(_detect)
```

**Key Insights**:
- ✅ **Non-blocking**: Doesn't block event loop
- ✅ **Thread pool**: Automatic thread management
- ✅ **Simple API**: `asyncio.to_thread()` (Python 3.9+)

### 13.2 Parallel Execution

**Pattern**: Gather multiple async operations

```python
async def get_multiple_predictions(
    symbols: list[str],
    predictor,
    db: AsyncSession,
):
    """Get predictions for multiple symbols in parallel."""
    tasks = [
        predictor.predict(symbol=symbol, db=db)
        for symbol in symbols
    ]
    
    # Execute in parallel
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Handle results
    predictions = []
    for symbol, result in zip(symbols, results):
        if isinstance(result, Exception):
            logger.error(f"Prediction failed for {symbol}: {result}")
            continue
        predictions.append(result)
    
    return predictions
```

**Key Insights**:
- ✅ **Parallel execution**: All tasks run concurrently
- ✅ **Error handling**: `return_exceptions=True` prevents one failure from stopping all
- ✅ **Performance**: N requests in ~same time as 1

---

## 14. Summary - Redis & Testing

### Patterns to Follow

1. **Redis Caching**:
   - Use namespaced keys (domain:entity:id)
   - Set appropriate TTLs (5min for predictions)
   - JSON serialize complex data
   - Handle cache misses gracefully

2. **Pub/Sub**:
   - Use channel constants (RedisChannels class)
   - JSON serialize messages
   - Fire-and-forget pattern
   - No persistence guarantees

3. **Testing**:
   - Use pytest with async support
   - Real database with transaction rollback
   - Override dependencies for isolation
   - AsyncMock for async methods
   - Arrange-Act-Assert structure

4. **Performance**:
   - Use `asyncio.to_thread()` for CPU-bound work
   - Use `asyncio.gather()` for parallel I/O
   - Avoid blocking the event loop

### Integration Checklist

- [ ] Redis caching implemented with TTL
- [ ] Cache keys follow naming convention
- [ ] Pub/Sub channels defined in RedisChannels
- [ ] Unit tests with db_session fixture
- [ ] Integration tests with async_client
- [ ] Mocks for external dependencies
- [ ] CPU-bound work offloaded to threads

---

**Next**: Part 4 - Integration Blueprint and Code Examples

See: `ARCHITECTURE_ANALYSIS_INTEGRATION.md`
