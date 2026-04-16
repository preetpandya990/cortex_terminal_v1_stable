# Post-Merge Cleanup Checklist

Quick reference for addressing known issues after Cortex AI unified merge.

## Week 1 (Priority 2 - High)

### [ ] Fix Feature Loader Parameter (1 hour)
- **File**: `backend/app/ml/features/feature_pipeline.py`
- **Action**: Add `lookback_days` parameter to `compute_features_for_symbol()`
- **Test**: `pytest tests/unit/test_feature_store.py -v`

### [ ] Fix API Integration Tests (4 hours)
- **Files**: `backend/tests/conftest.py`, `backend/tests/api/test_contracts.py`
- **Actions**:
  - Add database fixtures with test instrument data
  - Add Upstox client mocks
  - Fix mock application in tests
- **Test**: `pytest tests/api/test_contracts.py -v`

## Week 2 (Priority 3 - Medium)

### [ ] Fix AsyncMock Coroutine Warnings (3 hours)
- **Files**: 
  - `backend/tests/ai/intelligence/test_event_classifier.py`
  - `backend/tests/ai/intelligence/test_fake_news_detector.py`
  - `backend/tests/ai/governance/test_unified_model_registry.py`
- **Action**: Replace `return_value` with proper `AsyncMock()` initialization
- **Test**: Remove skip decorators and run tests

### [ ] Fix Migration Tests (2 hours)
- **Files**: `backend/tests/conftest.py`, `backend/tests/alembic/test_migration_0008.py`
- **Actions**:
  - Add test database fixture
  - Fix migration file path
  - Implement actual migration tests
- **Test**: `pytest tests/alembic/ -v`

### [ ] Fix Reuters RSS Feed (1 hour)
- **File**: `backend/app/ai/ingestion/rss_fetcher.py`
- **Actions**:
  - Try updated Reuters URL
  - Add proper headers
  - Consider alternative source if needed
- **Test**: Run RSS fetcher manually

## Month 1 (Priority 4 - Low)

### [ ] Increase Test Coverage to 35% (Week 1-2, 20 hours)
- Add tests for API routes
- Add tests for AI fusion components
- Add tests for core services

### [ ] Increase Test Coverage to 50% (Week 3-4, 20 hours)
- Add unit tests for feature engineering
- Add unit tests for training pipelines
- Add integration tests for ML workflows

### [ ] Fix Pydantic V2 Warnings (3 hours)
- Replace `class Config` with `model_config`
- Update field validators
- Audit all schema files

### [ ] Fix SQLAlchemy Pool Warnings (30 minutes)
- Add proper connection cleanup
- Update lifespan management

### [ ] Fix WebSocket Task Warnings (1 hour)
- Add timeout to disconnect
- Add cleanup fixtures

## Verification Commands

```bash
# Run all tests
cd backend
pytest tests/ -v --cov=app --cov-report=term

# Check coverage
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html

# Check worker logs
tail -f /tmp/worker.log | grep -E "(ERROR|WARNING)"

# Verify no AsyncMock warnings
pytest tests/ai/intelligence/ -v 2>&1 | grep -i "unraisable"

# Check API tests
pytest tests/api/test_contracts.py -v
```

## Success Criteria

- [ ] All tests passing (no failures)
- [ ] No tests skipped
- [ ] Coverage ≥ 35% (Week 2)
- [ ] Coverage ≥ 50% (Month 1)
- [ ] No ERROR logs in worker
- [ ] No AsyncMock warnings
- [ ] All API integration tests passing

## Progress Tracking

| Week | Tasks | Status | Coverage |
|------|-------|--------|----------|
| Week 1 | P2 issues | ⏳ Pending | 26.42% |
| Week 2 | P3 issues | ⏳ Pending | Target: 35% |
| Week 3-4 | P4 coverage | ⏳ Pending | Target: 50% |

---

**Last Updated**: 2026-04-15  
**Next Review**: 2026-04-22
