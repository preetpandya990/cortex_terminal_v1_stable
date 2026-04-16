# Known Issues and Remediation Plan

**Document Version**: 1.0  
**Last Updated**: 2026-04-15  
**Status**: Post-Merge Cleanup Roadmap

## Overview

This document catalogs all known issues discovered during the Cortex AI unified merge validation and provides detailed remediation plans. Issues are prioritized by severity and impact on production operations.

---

## Priority 1: Critical (Fix Immediately)

### None Identified ✅

All critical issues were resolved during merge validation.

---

## Priority 2: High (Fix Within 1 Week)

### 2.1 Feature Loader Parameter Mismatch

**Issue**: `compute_features_for_symbol()` called with unexpected `lookback_days` parameter

**Location**: `backend/app/ml/inference/feature_loader.py:205`

**Error**:
```python
TypeError: compute_features_for_symbol() got an unexpected keyword argument 'lookback_days'
```

**Impact**: 
- Non-critical: Gracefully handled with fallback
- Signals still generated successfully
- Appears in logs frequently

**Root Cause**:
```python
# feature_loader.py line 205
features_df = await compute_features_for_symbol(
    symbol=symbol,
    timeframe=timeframe,
    lookback_days=self.lookback_days,  # ❌ Parameter doesn't exist
)
```

**Fix**:
```python
# Option 1: Remove the parameter if not needed
features_df = await compute_features_for_symbol(
    symbol=symbol,
    timeframe=timeframe,
)

# Option 2: Add parameter to compute_features_for_symbol signature
# In backend/app/ml/features/feature_pipeline.py
async def compute_features_for_symbol(
    symbol: str,
    timeframe: str = "1d",
    lookback_days: int = 90,  # Add this parameter
) -> pd.DataFrame:
    # Implementation
```

**Recommended Solution**: Option 2 - Add the parameter to maintain flexibility

**Files to Modify**:
1. `backend/app/ml/features/feature_pipeline.py` - Add `lookback_days` parameter
2. `backend/app/ml/inference/feature_loader.py` - Verify call site

**Testing**:
```bash
# Test feature loading
pytest tests/unit/test_feature_store.py -v
pytest tests/integration/test_prediction_pipeline.py -v

# Verify no errors in worker logs
tail -f /tmp/worker.log | grep "lookback_days"
```

**Estimated Effort**: 1 hour

---

### 2.2 API Integration Tests Failing

**Issue**: 5 API contract tests failing due to missing infrastructure

**Location**: `backend/tests/api/test_contracts.py`

**Failing Tests**:
1. `test_live_price_requires_auth` - 502 (Upstox API unavailable)
2. `test_scanner_requires_auth` - 500 (Database/scanner service)
3. `test_stream_start_requires_auth` - 200 (WebSocket service running)
4. `test_instrument_search_requires_min_2_chars` - 500 (Database query)
5. `test_instrument_search_returns_list` - Assertion (Mock not applied)

**Root Cause**:
- Tests require live Upstox API connection
- Database needs populated instrument data
- Mocks not properly applied in test context

**Fix**:

**Step 1**: Add proper database fixtures
```python
# backend/tests/conftest.py

@pytest.fixture
async def db_with_instruments():
    """Provide database session with test instrument data."""
    from app.core.database import AsyncSessionLocal
    from app.models.upstox_data import UpstoxInstrument
    
    async with AsyncSessionLocal() as session:
        # Insert test instruments
        instruments = [
            UpstoxInstrument(
                instrument_key="NSE_EQ|INE002A01018",
                exchange="NSE",
                trading_symbol="RELIANCE",
                name="Reliance Industries Ltd",
                instrument_type="EQ",
            ),
            # Add more test instruments
        ]
        session.add_all(instruments)
        await session.commit()
        
        yield session
        
        # Cleanup
        await session.rollback()
```

**Step 2**: Mock Upstox client properly
```python
# backend/tests/api/test_contracts.py

@pytest.fixture
def mock_upstox():
    """Mock Upstox client for API tests."""
    with patch("app.services.upstox_client.UpstoxClient") as mock:
        mock_instance = AsyncMock()
        mock_instance.get_live_price.return_value = {
            "data": {
                "ltp": 2500.50,
                "volume": 1000000,
            }
        }
        mock.return_value = mock_instance
        yield mock_instance

class TestAuthEnforcement:
    async def test_live_price_requires_auth(self, lifespan_client, mock_upstox):
        # Now properly mocked
        response = await lifespan_client.get("/api/v1/market-data/live/NSE_EQ|INE002A01018")
        assert response.status_code == 401
```

**Step 3**: Fix mock application in market data tests
```python
async def test_instrument_search_returns_list(self, lifespan_client, db_with_instruments):
    # Use real database with test data instead of mocking
    response = await lifespan_client.get(
        "/api/v1/market-data/instruments/search", 
        params={"q": "RE"}
    )
    assert response.status_code == 200
    assert isinstance(response.json(), list)
```

**Files to Modify**:
1. `backend/tests/conftest.py` - Add database fixtures
2. `backend/tests/api/test_contracts.py` - Add Upstox mocks, use fixtures

**Testing**:
```bash
pytest tests/api/test_contracts.py -v
```

**Estimated Effort**: 4 hours

---

## Priority 3: Medium (Fix Within 2 Weeks)

### 3.1 AsyncMock Coroutine Warnings in LLM Tests

**Issue**: 11 tests skipped due to AsyncMock coroutine not being awaited

**Location**: 
- `backend/tests/ai/intelligence/test_event_classifier.py`
- `backend/tests/ai/intelligence/test_fake_news_detector.py`

**Error**:
```python
pytest.PytestUnraisableExceptionWarning: Exception ignored in: <coroutine object AsyncMockMixin._execute_mock_call>
RuntimeWarning: coroutine 'AsyncMockMixin._execute_mock_call' was never awaited
```

**Root Cause**:
```python
# Incorrect: Setting return_value on AsyncMock
classifier.ollama_client.generate_json.return_value = {
    "event_type": "earnings",
    # ...
}

# The mock returns a coroutine that's never awaited
```

**Fix**:
```python
# Correct: Use AsyncMock properly
classifier.ollama_client.generate_json = AsyncMock(return_value={
    "event_type": "earnings",
    "impact_score": 75.0,
    "confidence": 0.85,
})

# Or use side_effect for more control
async def mock_generate():
    return {
        "event_type": "earnings",
        "impact_score": 75.0,
        "confidence": 0.85,
    }

classifier.ollama_client.generate_json = AsyncMock(side_effect=mock_generate)
```

**Files to Modify**:
1. `backend/tests/ai/intelligence/test_event_classifier.py` - Fix all AsyncMock usages
2. `backend/tests/ai/intelligence/test_fake_news_detector.py` - Fix all AsyncMock usages
3. `backend/tests/ai/governance/test_unified_model_registry.py` - Fix encryption test

**Testing**:
```bash
# Remove skip decorators and run
pytest tests/ai/intelligence/test_event_classifier.py -v
pytest tests/ai/intelligence/test_fake_news_detector.py -v
pytest tests/ai/governance/test_unified_model_registry.py::test_register_model_encrypts_artifact -v
```

**Estimated Effort**: 3 hours

---

### 3.2 Migration Test Infrastructure

**Issue**: 2 migration tests skipped due to missing test database setup

**Location**: `backend/tests/alembic/test_migration_0008.py`

**Root Cause**:
- Tests require isolated test database
- Migration file path hardcoded incorrectly
- No test database fixture

**Fix**:

**Step 1**: Create test database fixture
```python
# backend/tests/conftest.py

@pytest.fixture(scope="session")
async def test_db_engine():
    """Create test database engine."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.core.config import settings
    
    # Use test database
    test_db_url = settings.DATABASE_URL.replace("/cortex_db", "/cortex_test_db")
    engine = create_async_engine(test_db_url, echo=False)
    
    # Create database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Cleanup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
```

**Step 2**: Fix migration file path
```python
# backend/tests/alembic/test_migration_0008.py

def test_migration_0008_structure():
    """Test migration 0008 has correct structure."""
    import importlib.util
    from pathlib import Path
    
    # Use Path for cross-platform compatibility
    migration_path = Path(__file__).parent.parent.parent / "alembic" / "versions" / "0008_add_encryption_fields_to_ai_ml_models.py"
    
    spec = importlib.util.spec_from_file_location("migration_0008", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    
    assert hasattr(migration, "revision")
    assert hasattr(migration, "upgrade")
    assert hasattr(migration, "downgrade")
```

**Step 3**: Implement actual migration test
```python
async def test_migration_0008_adds_fields(test_db_engine):
    """Test that migration 0008 adds encryption fields."""
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect
    
    # Run migration
    alembic_cfg = Config("alembic.ini")
    command.upgrade(alembic_cfg, "0008")
    
    # Verify fields added
    async with test_db_engine.connect() as conn:
        inspector = inspect(conn)
        columns = [col["name"] for col in inspector.get_columns("ai_ml_models")]
        
        assert "artifact_encrypted" in columns
        assert "artifact_sha256" in columns
        assert "timeframe" in columns
    
    # Rollback
    command.downgrade(alembic_cfg, "-1")
```

**Files to Modify**:
1. `backend/tests/conftest.py` - Add test database fixture
2. `backend/tests/alembic/test_migration_0008.py` - Fix path, implement tests

**Testing**:
```bash
pytest tests/alembic/test_migration_0008.py -v
```

**Estimated Effort**: 2 hours

---

### 3.3 Reuters RSS Feed Unreachable

**Issue**: Worker logs show Reuters RSS feed connection failures

**Location**: `backend/app/ai/ingestion/rss_fetcher.py`

**Error**:
```
WARNING: Failed to fetch RSS from https://www.reuters.com/finance/markets: Connection timeout
```

**Impact**:
- Low: 3 other RSS feeds working (Economic Times, Moneycontrol, Business Standard)
- Reduces news coverage diversity

**Root Cause**:
- Reuters may block automated requests
- URL may have changed
- Rate limiting or geo-blocking

**Fix**:

**Option 1**: Update Reuters URL
```python
# backend/app/ai/ingestion/rss_fetcher.py

RSS_FEEDS = [
    {
        "name": "Reuters Markets",
        "url": "https://www.reuters.com/business/finance/rss",  # Updated URL
        "category": "market_news",
    },
    # ... other feeds
]
```

**Option 2**: Add user agent and headers
```python
async def fetch_rss_feed(self, feed_url: str) -> List[Dict]:
    """Fetch RSS feed with proper headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml",
    }
    
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        response = await client.get(feed_url)
        # ... parse RSS
```

**Option 3**: Replace with alternative source
```python
RSS_FEEDS = [
    {
        "name": "Bloomberg Markets",
        "url": "https://www.bloomberg.com/feed/markets/rss",
        "category": "market_news",
    },
    # Or Financial Times, WSJ, etc.
]
```

**Recommended Solution**: Try Option 1, then Option 2, fallback to Option 3

**Files to Modify**:
1. `backend/app/ai/ingestion/rss_fetcher.py` - Update URL and headers

**Testing**:
```bash
# Test RSS fetching
python -c "
import asyncio
from app.ai.ingestion.rss_fetcher import RSSFetcher

async def test():
    fetcher = RSSFetcher()
    events = await fetcher.fetch_all_feeds()
    print(f'Fetched {len(events)} events')

asyncio.run(test())
"
```

**Estimated Effort**: 1 hour

---

## Priority 4: Low (Fix Within 1 Month)

### 4.1 Test Coverage Below Target

**Issue**: Overall coverage 26.42% < 80% target

**Impact**: 
- Low: Critical modules have 90%+ coverage
- Affects confidence in untested code paths

**Analysis**:
```
High Coverage (>90%): Schemas, Models, Core Services
Medium Coverage (40-60%): API Routes, AI Fusion
Low Coverage (<20%): ML Training, Feature Engineering
```

**Root Cause**:
- Large codebase with many legacy modules
- ML training pipelines are offline batch processes
- Feature engineering tested via integration tests
- Some modules are examples/demos

**Fix Strategy**:

**Phase 1: Quick Wins (Target: 35%)**
1. Add tests for API routes (currently 40-60%)
2. Add tests for AI fusion components (currently 46-63%)
3. Add tests for core services (currently 45-96%)

**Phase 2: ML Pipelines (Target: 50%)**
1. Add unit tests for feature engineering
2. Add unit tests for training pipelines
3. Add integration tests for end-to-end ML workflows

**Phase 3: Comprehensive (Target: 80%)**
1. Add property-based tests
2. Add chaos engineering tests
3. Add performance regression tests

**Implementation Plan**:

```python
# Example: Add tests for signal_assembler.py (currently 62.50%)

# tests/ai/fusion/test_signal_assembler_comprehensive.py

class TestSignalAssemblerComprehensive:
    """Comprehensive tests for SignalAssembler."""
    
    async def test_event_decay_calculation(self):
        """Test exponential decay calculation."""
        assembler = SignalAssembler()
        
        # Test at half-life
        decay = assembler.calculate_event_decay(age_hours=24.0, half_life_hours=24.0)
        assert abs(decay - 0.5) < 0.01
        
        # Test at 2x half-life
        decay = assembler.calculate_event_decay(age_hours=48.0, half_life_hours=24.0)
        assert abs(decay - 0.25) < 0.01
    
    async def test_signal_fusion_weights(self):
        """Test signal fusion weight validation."""
        # Should raise if weights don't sum to 1.0
        with pytest.raises(ValueError):
            SignalAssembler(
                event_weight=0.5,
                ml_weight=0.3,
                technical_weight=0.1,  # Sum = 0.9
            )
    
    async def test_gather_technical_signals(self):
        """Test technical signal gathering."""
        assembler = SignalAssembler()
        db_mock = AsyncMock()
        
        result = await assembler.gather_technical_signals(
            db=db_mock,
            symbol="NSE_EQ|INE002A01018",
        )
        
        assert "score" in result
        assert "confidence" in result
        assert "indicators" in result
```

**Files to Create**:
- `tests/ai/fusion/test_signal_assembler_comprehensive.py`
- `tests/ml/features/test_feature_engineering.py`
- `tests/ml/training/test_training_pipelines.py`
- `tests/integration/test_end_to_end_ml.py`

**Estimated Effort**: 40 hours (spread over 1 month)

---

### 4.2 Pydantic V2 Migration Warnings

**Issue**: Some models still using deprecated Pydantic v1 patterns

**Location**: Various schema files

**Warning**:
```python
DeprecationWarning: `class Config` is deprecated, use `model_config` instead
```

**Fix**:

**Pattern 1**: Replace `class Config` with `model_config`
```python
# Before (Pydantic v1)
class MySchema(BaseModel):
    field: str
    
    class Config:
        from_attributes = True
        protected_namespaces = ()

# After (Pydantic v2)
class MySchema(BaseModel):
    model_config = ConfigDict(
        from_attributes=True,
        protected_namespaces=()
    )
    
    field: str
```

**Pattern 2**: Update field validators
```python
# Before (Pydantic v1)
from pydantic import validator

class MySchema(BaseModel):
    @validator('field')
    def validate_field(cls, v):
        return v

# After (Pydantic v2)
from pydantic import field_validator

class MySchema(BaseModel):
    @field_validator('field')
    @classmethod
    def validate_field(cls, v):
        return v
```

**Files to Audit**:
```bash
# Find all schemas with old Config pattern
grep -r "class Config:" backend/app/schemas/
grep -r "class Config:" backend/app/models/
```

**Estimated Effort**: 3 hours

---

### 4.3 SQLAlchemy Connection Pool Warnings

**Issue**: Minor connection cleanup warnings on shutdown

**Warning**:
```
WARNING: Connection pool cleanup incomplete
```

**Impact**: Very low - cosmetic warning, no functional impact

**Fix**:
```python
# backend/app/core/database.py

async def close_db():
    """Properly close database connections."""
    global engine
    if engine:
        await engine.dispose()
        logger.info("Database connections closed")
```

**Files to Modify**:
1. `backend/app/core/database.py` - Add proper cleanup
2. `backend/app/main.py` - Call cleanup in lifespan

**Estimated Effort**: 30 minutes

---

### 4.4 WebSocket Task Cleanup Warning

**Issue**: WebSocket task destroyed while pending

**Warning**:
```
ERROR: Task was destroyed but it is pending!
task: <Task pending name='Task-91' coro=<BaseWebSocketService.connect()>>
```

**Impact**: Low - occurs during test teardown, not in production

**Root Cause**: WebSocket connection not properly closed in tests

**Fix**:
```python
# backend/app/services/base_websocket.py

async def disconnect(self):
    """Disconnect WebSocket with proper cleanup."""
    if self._ws:
        try:
            await asyncio.wait_for(self._ws.close(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("WebSocket close timeout, forcing disconnect")
        finally:
            self._ws = None
            self._connected = False

# backend/tests/conftest.py

@pytest.fixture
async def websocket_service():
    """Provide WebSocket service with proper cleanup."""
    service = BaseWebSocketService()
    yield service
    await service.disconnect()  # Ensure cleanup
```

**Files to Modify**:
1. `backend/app/services/base_websocket.py` - Add timeout to disconnect
2. `backend/tests/conftest.py` - Add cleanup fixture

**Estimated Effort**: 1 hour

---

## Summary

### Issue Count by Priority
- **Priority 1 (Critical)**: 0 issues ✅
- **Priority 2 (High)**: 2 issues (5 hours effort)
- **Priority 3 (Medium)**: 3 issues (9 hours effort)
- **Priority 4 (Low)**: 4 issues (44.5 hours effort)

**Total**: 9 issues, 58.5 hours estimated effort

### Recommended Fix Order
1. **Week 1**: P2.1 Feature Loader (1h), P2.2 API Tests (4h)
2. **Week 2**: P3.1 AsyncMock (3h), P3.2 Migration Tests (2h), P3.3 RSS Feed (1h)
3. **Month 1**: P4.1 Coverage (40h), P4.2 Pydantic (3h), P4.3 Pool (0.5h), P4.4 WebSocket (1h)

### Success Metrics
- All P2 issues resolved within 1 week
- All P3 issues resolved within 2 weeks
- Coverage increased to 35% within 2 weeks
- Coverage increased to 50% within 1 month
- All tests passing (no skipped/failed) within 1 month

---

**Document Owner**: Engineering Team  
**Review Frequency**: Weekly  
**Next Review**: 2026-04-22
