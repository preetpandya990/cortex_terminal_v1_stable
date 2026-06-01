# Cortex AI - Architecture Analysis for ML Services Integration
**Date**: 2026-05-11  
**Purpose**: Deep codebase analysis for pattern detection and sentiment analysis services  
**Quality Standard**: Billion-dollar app - world-class, production-ready, industry standards

---

## Executive Summary

This document provides a comprehensive analysis of the Cortex AI codebase architecture to ensure new ML services (pattern detection and sentiment analysis) integrate seamlessly with existing patterns and maintain world-class quality standards.

**Key Findings**:
- ✅ **Mature Architecture**: Production-grade async FastAPI with TimescaleDB
- ✅ **Consistent Patterns**: Well-defined service, API, and database layers
- ✅ **Security First**: JWT auth, rate limiting, comprehensive error handling
- ✅ **Performance Optimized**: Connection pooling, Redis caching, async/await
- ✅ **Testing Infrastructure**: Comprehensive test suite with fixtures

**Integration Strategy**: Follow existing patterns exactly - no new patterns without justification.

---

## 1. Database Layer Architecture

### 1.1 Connection Management

**File**: `backend/app/core/database.py`

**Pattern**: Dual-engine architecture for API and worker processes

```python
# API Engine (main process)
engine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_size=20,              # High concurrency support
    max_overflow=10,           # Burst capacity
    pool_timeout=30,           # Connection wait timeout
    pool_recycle=1800,         # Recycle connections every 30 min
    pool_pre_ping=True,        # Verify connections before use
    echo=settings.DEBUG,       # SQL logging in debug mode
    connect_args={
        "server_settings": {
            "timezone": "UTC",
            "statement_timeout": "30000",  # 30s query timeout
        }
    },
)

# Worker Engine (background process)
worker_engine = create_async_engine(
    str(settings.DATABASE_URL),
    pool_size=10,              # Lower pool for workers
    max_overflow=5,
    # ... same configuration
)
```

**Key Insights**:
- ✅ **Separate engines** for API and workers prevent pool exhaustion
- ✅ **Pool pre-ping** ensures connection health before queries
- ✅ **Statement timeout** prevents runaway queries
- ✅ **UTC timezone** enforced at database level

### 1.2 Session Management

**Pattern**: Async context manager with automatic rollback

```python
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield AsyncSession scoped to a single request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()  # Automatic rollback on error
            raise
        finally:
            await session.close()     # Always close session
```

**Usage in Services**:
```python
async def my_service_method(self, db: AsyncSession):
    # Session is already open and managed
    result = await db.execute(select(Model).where(...))
    # No need to commit/close - handled by get_db()
```

**Key Insights**:
- ✅ **Automatic cleanup**: Rollback on error, close on exit
- ✅ **No manual commit**: Services don't call commit() directly
- ✅ **Exception safety**: Rollback ensures database consistency

### 1.3 Database Health Check

**Pattern**: Simple connectivity verification

```python
async def check_db_connection() -> bool:
    """Verify database connectivity."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False
```

**Key Insights**:
- ✅ **Lightweight check**: Simple SELECT 1 query
- ✅ **Used in health endpoints**: `/health` endpoint validation
- ✅ **Logging on failure**: Errors logged for monitoring

---

## 2. Configuration Management

### 2.1 Settings Architecture

**File**: `backend/app/core/config.py`

**Pattern**: Pydantic Settings with environment variables

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",          # Ignore unknown env vars
        env_prefix="",
    )
    
    # Application
    APP_NAME: str = "Cortex AI Trading Platform"
    ENVIRONMENT: Literal["development", "staging", "production"] = "production"
    DEBUG: bool = False
    
    # Database
    DATABASE_URL: PostgresDsn = Field(..., description="Async PostgreSQL URL")
    DB_POOL_SIZE: int = Field(20, ge=1, le=100)
    
    # Redis
    REDIS_URL: RedisDsn = Field(..., description="Redis URL")
    REDIS_MAX_CONNECTIONS: int = Field(50, ge=10, le=500)
    
    # Security
    SECRET_KEY: str = Field(..., min_length=32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(30, ge=5, le=1440)
```

**Key Insights**:
- ✅ **Type safety**: Pydantic validates all settings at startup
- ✅ **Fail fast**: Missing required settings cause startup failure
- ✅ **Validation**: Field constraints (ge, le, min_length) enforced
- ✅ **Documentation**: Field descriptions for clarity

### 2.2 Settings Access Pattern

**Pattern**: Singleton with lru_cache

```python
@lru_cache()
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()

# Usage
settings = get_settings()
```

**Key Insights**:
- ✅ **Singleton**: Settings loaded once and cached
- ✅ **Thread-safe**: lru_cache handles concurrency
- ✅ **No global state**: Function-based access pattern

---

## 3. Service Layer Patterns

### 3.1 Service Architecture

**Example**: `backend/app/services/candle_service.py`

**Pattern**: Static methods with dependency injection

```python
class CandleService:
    """
    Service for managing OHLCV candle data with DB-first strategy.
    """
    
    @staticmethod
    def _timeframe_to_db_format(unit: str, interval: int) -> str:
        """Convert API timeframe format to database format."""
        # Implementation
    
    async def get_historical_candles(
        self,
        db: AsyncSession,
        instrument_key: str,
        unit: str,
        interval: int,
        from_date: str,
        to_date: str,
    ) -> tuple[list[list], bool, list[dict[str, str]]]:
        """Get historical candles with DB-first strategy."""
        # Implementation
```

**Key Insights**:
- ✅ **Stateless**: Services don't maintain state between calls
- ✅ **Dependency injection**: Database session passed as parameter
- ✅ **Type hints**: Full type annotations for IDE support
- ✅ **Docstrings**: Comprehensive documentation

### 3.2 DB-First Fetching Strategy

**Pattern**: Check database first, fallback to API

```python
async def get_historical_candles(self, db: AsyncSession, ...):
    # 1. Query database for existing candles
    result = await db.execute(text("SELECT ... FROM upstox_ohlcv ..."))
    rows = result.fetchall()
    
    if not rows:
        # 2. No data in DB - signal API fetch needed
        return [], False, []
    
    # 3. Convert rows to API format
    candles = [self._format_candle_for_api(row) for row in rows]
    
    # 4. Detect gaps in data
    gaps = self._detect_gaps(candles, from_date, to_date, unit)
    
    if gaps:
        # 5. Partial data - return with gap information
        return candles, True, gaps
    else:
        # 6. Complete data - return from DB
        return candles, True, []
```

**Key Insights**:
- ✅ **Performance**: Database query faster than API call
- ✅ **Rate limit protection**: Reduces external API calls
- ✅ **Gap detection**: Identifies missing data ranges
- ✅ **Transparent**: Returns metadata about data source

### 3.3 Error Handling Pattern

**Pattern**: Try-except with logging and graceful degradation

```python
async def get_historical_candles(self, db: AsyncSession, ...):
    try:
        result = await db.execute(...)
        # Process result
    except Exception as e:
        logger.exception(
            f"[CandleService] Error querying DB for {instrument_key}: {e}"
        )
        return [], False, []  # Graceful degradation
```

**Key Insights**:
- ✅ **Comprehensive logging**: Exception details captured
- ✅ **Graceful degradation**: Returns empty data instead of crashing
- ✅ **Context in logs**: Includes relevant parameters
- ✅ **Exception propagation**: Only catches at service boundary

---

## 4. Summary - Database & Services

### Patterns to Follow

1. **Database Access**:
   - Use `AsyncSession` from `get_db()` dependency
   - Never call `commit()` or `close()` manually
   - Use `text()` for raw SQL queries
   - Always include error handling with rollback

2. **Service Design**:
   - Stateless classes with dependency injection
   - DB-first strategy for data fetching
   - Comprehensive error handling with logging
   - Type hints and docstrings required

3. **Configuration**:
   - All settings in `Settings` class
   - Use `get_settings()` for access
   - Validate at startup with Pydantic

### Integration Checklist

- [ ] Service class follows stateless pattern
- [ ] Database session injected via `get_db()`
- [ ] Error handling with try-except and logging
- [ ] Type hints on all methods
- [ ] Docstrings with parameter descriptions
- [ ] DB-first fetching strategy implemented
- [ ] Graceful degradation on errors

---

**Next**: Part 2 - API Layer, Authentication, and Rate Limiting

See: `ARCHITECTURE_ANALYSIS_API.md`
