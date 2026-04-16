# Phase 4: AI Services Async Rewrite - Implementation Guide

**Status:** ⏳ IN PROGRESS  
**Started:** 2026-04-10 20:25 IST

## Overview

Phase 4 converts 20+ AI microservice modules from synchronous to async patterns. This document provides the conversion template and tracks progress.

## Conversion Template

### Pattern: Sync → Async Conversion

**Before (Sync):**
```python
from sqlalchemy.orm import Session

class MyService:
    def process(self, db: Session, data: dict) -> Result:
        result = db.execute(select(Model).where(...))
        item = result.scalar_one()
        db.add(new_item)
        db.commit()
        db.refresh(new_item)
        return new_item
```

**After (Async):**
```python
from sqlalchemy.ext.asyncio import AsyncSession

class MyService:
    async def process(self, db: AsyncSession, data: dict) -> Result:
        result = await db.execute(select(Model).where(...))
        item = result.scalar_one()
        db.add(new_item)
        await db.commit()
        await db.refresh(new_item)
        return new_item
```

### Key Changes:
1. `Session` → `AsyncSession`
2. `def` → `async def`
3. `db.execute()` → `await db.execute()`
4. `db.commit()` → `await db.commit()`
5. `db.refresh()` → `await db.refresh()`
6. Remove `db.close()` (handled by context manager)

## Progress

### 4.1 Signal Assembler ✅ COMPLETE

**File:** `backend/app/ai/fusion/signal_assembler.py`

**Changes:**
- Converted to `AsyncSession`
- All database operations use `await`
- Redis pub/sub integration with `PubSubClient`
- Methods: `assemble_signal()`, `gather_event_signals()`, `get_latest_regime()`, `get_active_strategy()`

**Lines:** 235

### 4.2 Signal Pipeline ⏳ PENDING

**File:** `backend/app/ai/fusion/signal_pipeline.py` (to create)

**Source:** `cortex_v4_w_aimsvc-master/backend/app/services/signal_pipeline.py`

**Required Changes:**
- Convert all database operations to async
- Update signal publishing to use async Redis
- Convert batch processing loops to async
- Test end-to-end signal generation

### 4.3 NLP & Intelligence Services ⏳ PENDING

**Files to Convert:**
- `backend/app/ai/intelligence/nlp_engine.py`
- `backend/app/ai/intelligence/event_classifier.py`
- `backend/app/ai/intelligence/fake_news_detector.py`
- `backend/app/ai/intelligence/sentiment_analyzer.py`

**Source:** `cortex_v4_w_aimsvc-master/backend/app/services/`

**Required Changes:**
- Convert `process_event()` to async
- Convert `classify()` methods to async
- Convert `detect()` methods to async
- Update all Ollama/LLM calls to async
- Test each service independently

### 4.4 Safety & Governance Services ⏳ PENDING

**Files to Convert:**
- `backend/app/ai/safety/kill_switch_manager.py`
- `backend/app/ai/safety/safety_trigger_engine.py`
- `backend/app/ai/governance/model_registry.py`

**Source:** `cortex_v4_w_aimsvc-master/backend/app/services/`

**Required Changes:**
- Convert `activate()`, `deactivate()`, `is_active()` to async
- Convert `check_safety_conditions()` to async
- Convert model governance functions to async
- Test kill switch activation flow

### 4.5 Strategy Services ⏳ PENDING

**Files to Convert:**
- `backend/app/ai/strategy/regime_detector.py`
- `backend/app/ai/strategy/strategy_orchestrator.py`

**Source:** `cortex_v4_w_aimsvc-master/backend/app/services/`

**Required Changes:**
- Convert `detect_regime()` to async
- Convert strategy selection logic to async
- Update all database queries to async
- Test regime detection with live data

### 4.6 Remaining AI Services ⏳ PENDING

**Files to Convert:**
- `backend/app/ai/ingestion/rss_fetcher.py`
- `backend/app/ai/ingestion/event_parser.py`
- All remaining services in `ai/` directory

**Source:** `cortex_v4_w_aimsvc-master/backend/app/services/`

**Required Changes:**
- Convert RSS fetching to async (use `httpx.AsyncClient`)
- Convert event parsing to async
- Remove all `db.close()` calls
- Verify no sync `Session` imports remain

### 4.7 Validation ⏳ PENDING

**Tasks:**
- Run all AI service unit tests (100% pass rate)
- Run integration tests for signal generation pipeline
- Verify no synchronous `Session` imports in `ai/` directory
- Grep check: `grep -r "from sqlalchemy.orm import Session" backend/app/ai/`

## Files Created

### Completed:
1. ✅ `backend/app/ai/fusion/signal_assembler.py` (235 lines)
2. ✅ `backend/app/ai/fusion/models.py` (ORM models, 200 lines)
3. ✅ `backend/app/ai/fusion/__init__.py`
4. ✅ `backend/app/ai/intelligence/__init__.py`
5. ✅ `backend/app/ai/safety/__init__.py`

### Pending:
- `backend/app/ai/fusion/signal_pipeline.py`
- `backend/app/ai/intelligence/nlp_engine.py`
- `backend/app/ai/intelligence/event_classifier.py`
- `backend/app/ai/intelligence/fake_news_detector.py`
- `backend/app/ai/safety/kill_switch_manager.py`
- `backend/app/ai/safety/safety_trigger_engine.py`
- `backend/app/ai/strategy/regime_detector.py`
- `backend/app/ai/ingestion/rss_fetcher.py`
- 15+ additional services

## Estimated Effort

- **Services to convert:** 20+
- **Lines per service:** 200-500
- **Total estimated lines:** 5,000-10,000
- **Estimated time:** 8-12 hours of focused work

## Recommendation

Given the scope, Phase 4 should be completed incrementally:

1. **Critical path first:** signal_pipeline, nlp_engine, regime_detector (tasks 4.2, 4.3, 4.5)
2. **Safety layer:** kill_switch, safety_trigger (task 4.4)
3. **Remaining services:** RSS, event parser, etc. (task 4.6)
4. **Validation:** Full test suite (task 4.7)

## Next Steps

1. Convert `signal_pipeline.py` (task 4.2)
2. Convert NLP services (task 4.3)
3. Continue with remaining subtasks

---

**Phase 4 Status:** ⏳ PARTIALLY COMPLETE (1/7 subtasks)  
**Ready for:** Task 4.2 - Signal Pipeline Conversion
