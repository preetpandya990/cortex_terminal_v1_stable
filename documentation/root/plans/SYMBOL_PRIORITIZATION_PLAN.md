# Symbol Prioritization & Company Name Display - Implementation Plan

**Date**: 2026-05-11  
**Status**: Planning Phase  
**Priority**: High  

---

## PROBLEM STATEMENT

### Current Issues

1. **No Symbol Prioritization**: Events are processed for any NSE symbol the LLM extracts from news, regardless of:
   - Whether symbol exists in `instrument_master`
   - Data availability (OHLCV candles)
   - Liquidity/trading volume
   - Market relevance

2. **Poor UX**: Trading symbols (e.g., "HDFCBANK") are cryptic to users. Need full company names (e.g., "HDFC Bank Ltd.").

### Impact

- Wasted computation on illiquid/irrelevant symbols
- Failed signal generation attempts (no OHLCV data)
- Confusing user experience in trading signals table
- No systematic approach to symbol universe management

---

## STRATEGIC OBJECTIVES

1. **Data Quality**: Only process symbols with sufficient historical data and liquidity
2. **Performance**: Reduce wasted computation on illiquid/irrelevant symbols
3. **User Experience**: Display human-readable company names alongside symbols
4. **Scalability**: Support dynamic universe expansion (watchlists, user requests)
5. **Maintainability**: Clean, testable, production-grade implementation

---

## ARCHITECTURE DECISIONS

### A. Symbol Universe Tiers (Industry Standard Approach)

**Tier 1: Scheduled Universe** (Nifty 50 + Next 50 = 100 symbols)
- Already configured in `SIGNAL_SCHEDULED_UNIVERSE`
- Generated every 15 minutes during market hours
- Guaranteed data availability and liquidity

**Tier 2: Extended Universe** (Nifty 500 constituents)
- On-demand generation only
- Validated against minimum criteria before processing
- Cached for 15 minutes

**Tier 3: Watchlist/User-Requested**
- User-specific symbols from watchlists
- Validated but not rejected if criteria fail (user intent)
- Lower priority in event processing

**Rejected: Symbols outside these tiers**
- No OHLCV data
- Below minimum liquidity threshold
- Not in `instrument_master`

### B. Liquidity Filtering Criteria (NSE Best Practices)

Based on research and NSE derivative selection criteria:

1. **Minimum Average Daily Volume (ADV)**: 
   - Tier 1: No filter (pre-vetted)
   - Tier 2: ADV > 100,000 shares (last 30 days)
   - Tier 3: ADV > 50,000 shares (relaxed for user intent)

2. **Minimum Data Availability**:
   - At least 60 days of daily OHLCV data
   - At least 52 candles for technical indicators

3. **Market Cap** (optional, Phase 2):
   - Can add market cap filtering later
   - Requires additional data source or periodic sync

### C. Database Schema Changes

**New Table: `symbol_metadata`**

```sql
CREATE TABLE symbol_metadata (
    id BIGSERIAL PRIMARY KEY,
    instrument_key VARCHAR(200) UNIQUE NOT NULL REFERENCES instrument_master(instrument_key),
    trading_symbol VARCHAR(50) NOT NULL,
    company_name VARCHAR(200) NOT NULL,  -- from instrument_master.name
    
    -- Liquidity metrics (computed daily)
    avg_volume_30d BIGINT,
    avg_volume_90d BIGINT,
    last_traded_date DATE,
    candle_count_1d INT,
    
    -- Tier classification
    tier INT NOT NULL DEFAULT 3,  -- 1=scheduled, 2=extended, 3=watchlist
    is_eligible BOOLEAN NOT NULL DEFAULT true,
    
    -- Metadata
    sector VARCHAR(100),
    market_cap NUMERIC(20, 2),  -- future
    
    -- Timestamps
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX idx_symbol_metadata_trading_symbol ON symbol_metadata(trading_symbol);
CREATE INDEX idx_symbol_metadata_tier_eligible ON symbol_metadata(tier, is_eligible);
CREATE INDEX idx_symbol_metadata_updated ON symbol_metadata(updated_at);
CREATE INDEX idx_symbol_metadata_eligible ON symbol_metadata(is_eligible) WHERE is_eligible = true;
```

**Why separate table?**
- `instrument_master` is synced from Upstox (immutable source of truth)
- `symbol_metadata` is computed internally (mutable, business logic)
- Clean separation of concerns

**Update to `ai_trading_signals` table:**

```sql
ALTER TABLE ai_trading_signals 
ADD COLUMN company_name VARCHAR(200);

-- Backfill (run after metadata table is populated)
UPDATE ai_trading_signals s
SET company_name = m.company_name
FROM symbol_metadata m
WHERE m.trading_symbol = s.symbol;

-- Make non-nullable after backfill
ALTER TABLE ai_trading_signals 
ALTER COLUMN company_name SET NOT NULL;
```

### D. Event Processing Changes

**Current Flow:**
```
RSS → NLP → EventClassifier (LLM) → affected_symbols (any NSE symbol)
```

**New Flow:**
```
RSS → NLP → EventClassifier (LLM) → affected_symbols 
    → Symbol Validator → Filtered symbols (only eligible)
    → AIEventClassification.affected_symbols
```

**Symbol Validator Logic:**
1. Check if symbol exists in `symbol_metadata`
2. Check `is_eligible = true`
3. Check tier (1, 2, or 3)
4. Log rejected symbols for monitoring
5. If all symbols rejected → still create event but with empty `affected_symbols`

### E. Frontend Changes

**Type Update (`frontend/src/types/signals.ts`):**

```typescript
export interface TradingSignal {
  signal_id: string;
  symbol: string;
  company_name: string;  // NEW
  signal_type: SignalType;
  confidence: number;
  calibrated_confidence: number;
  target_price?: number;
  stop_loss?: number;
  time_horizon: TimeHorizon;
  reasoning: string;
  contributing_factors: ContributingFactors;
  regime_type: RegimeType;
  generated_at: string;
  expires_at: string;
}
```

**UI Update (`frontend/src/components/ai/SignalsPanel.tsx`):**

```tsx
<TableCell>
  <div className="flex flex-col">
    <span className="font-medium">{signal.symbol}</span>
    <span className="text-xs text-muted-foreground">{signal.company_name}</span>
  </div>
</TableCell>
```

---

## IMPLEMENTATION PHASES

### Phase 1: Database & Core Infrastructure (Foundation)

**Tasks:**

1. **Create `symbol_metadata` table**
   - Migration script with proper indexes
   - Backfill from `instrument_master` + `upstox_ohlcv`

2. **Compute liquidity metrics**
   - Daily background job (similar to drift monitoring)
   - Compute ADV 30d/90d, candle counts
   - Update `is_eligible` flag

3. **Tier assignment**
   - Tier 1: Match against `SIGNAL_SCHEDULED_UNIVERSE`
   - Tier 2: Nifty 500 constituents (need list or API)
   - Tier 3: Everything else

4. **ORM Model**
   - `backend/app/models/symbol_metadata.py`
   - Proper relationships with `InstrumentMaster`

**Files to Create/Modify:**
- `backend/alembic/versions/XXXX_add_symbol_metadata.py`
- `backend/app/models/symbol_metadata.py`
- `backend/scripts/backfill_symbol_metadata.py`
- `backend/scripts/compute_symbol_metrics.py`

**Estimated Effort**: 2-3 days

---

### Phase 2: Symbol Validation Service (Business Logic)

**Tasks:**

1. **Create `SymbolValidator` service**
   - `backend/app/services/symbol_validator.py`
   - Methods:
     - `validate_symbol(symbol: str) → bool`
     - `validate_symbols(symbols: list[str]) → list[str]`
     - `get_symbol_metadata(symbol: str) → SymbolMetadata | None`
     - `get_eligible_symbols(tier: int | None) → list[str]`

2. **Integrate into EventClassifier**
   - Post-process `affected_symbols` from LLM
   - Filter through validator
   - Log rejections with reason

3. **Integrate into SignalAssembler**
   - Validate symbol before `assemble_signal()`
   - Raise `ValueError` with clear message if ineligible
   - API endpoint catches and returns 400 with reason

**Files to Create/Modify:**
- `backend/app/services/symbol_validator.py`
- `backend/app/ai/intelligence/event_classifier.py`
- `backend/app/ai/fusion/signal_assembler.py`
- `backend/app/api/v1/fusion.py`

**Estimated Effort**: 2-3 days

---

### Phase 3: API & Serialization (Data Flow)

**Tasks:**

1. **Update `AITradingSignal` model**
   - Add `company_name` column (denormalized for performance)
   - Populated from `symbol_metadata` during signal creation

2. **Update serializers**
   - `backend/app/ai/fusion/serializers.py`
   - Include `company_name` in JSON response

3. **Update API responses**
   - `/signals` endpoint includes company name
   - `/signals/generate/{symbol}` includes company name

**Files to Create/Modify:**
- `backend/alembic/versions/XXXX_add_company_name_to_signals.py`
- `backend/app/ai/fusion/models.py`
- `backend/app/ai/fusion/serializers.py`
- `backend/app/ai/fusion/signal_assembler.py`

**Estimated Effort**: 1-2 days

---

### Phase 4: Frontend Integration (User Experience)

**Tasks:**

1. **Update TypeScript types**
   - Add `company_name` to `TradingSignal` interface

2. **Update SignalsPanel component**
   - Two-line display: symbol + company name
   - Responsive design (stack on mobile)

3. **Update filters**
   - Search by symbol OR company name
   - Autocomplete suggestions with both

4. **Update SignalDetailModal**
   - Prominent company name display
   - Symbol as secondary info

**Files to Create/Modify:**
- `frontend/src/types/signals.ts`
- `frontend/src/components/ai/SignalsPanel.tsx`
- `frontend/src/components/ai/SignalDetailModal.tsx`

**Estimated Effort**: 1-2 days

---

### Phase 5: Monitoring & Maintenance (Operations)

**Tasks:**

1. **Daily metrics computation job**
   - Scheduled task (similar to signal scheduler)
   - Runs at market close (15:45 IST)
   - Updates `symbol_metadata` for all Tier 1+2

2. **Alerting**
   - Monitor rejected symbol rate
   - Alert if >20% of events have no eligible symbols
   - Track tier distribution

3. **Admin API endpoints**
   - `GET /admin/symbols/metadata` - view all metadata
   - `POST /admin/symbols/recompute` - force recompute
   - `GET /admin/symbols/rejected` - view rejection log

**Files to Create/Modify:**
- `backend/app/services/symbol_metrics_scheduler.py`
- `backend/app/api/v1/admin/symbols.py`
- `backend/app/monitoring/symbol_metrics.py`

**Estimated Effort**: 1 day

---

## DATA SOURCES FOR NIFTY 500

### Option 1: NSE Official API (Recommended for Production)
- Endpoint: `https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20500`
- Free, official, updated daily
- Returns JSON with all constituents

### Option 2: Static Configuration (Recommended for Initial Implementation)
- Maintain list in `backend/app/core/config.py`
- Update quarterly (index rebalancing)
- Less dynamic but more reliable

### Option 3: Third-party data provider
- Screener.in, Moneycontrol APIs
- Requires API key, may have rate limits

**Recommendation**: Start with Option 2 (static config), migrate to Option 1 in Phase 2.

---

## PERFORMANCE CONSIDERATIONS

### 1. Caching Strategy

- Cache `symbol_metadata` in Redis (TTL: 1 hour)
- Cache validation results (TTL: 15 minutes)
- Invalidate on daily recompute

**Redis Keys:**
```
symbol:metadata:{trading_symbol}  → SymbolMetadata JSON
symbol:validation:{trading_symbol} → boolean
symbol:tier:{tier}:eligible → list of symbols
```

### 2. Database Optimization

**Indexes:**
- Composite index on `(tier, is_eligible, trading_symbol)`
- Partial index on `is_eligible = true`
- Index on `updated_at` for incremental updates

**Query Patterns:**
```sql
-- Fast validation (uses partial index)
SELECT EXISTS(
    SELECT 1 FROM symbol_metadata 
    WHERE trading_symbol = ? AND is_eligible = true
);

-- Batch validation (single query)
SELECT trading_symbol FROM symbol_metadata
WHERE trading_symbol = ANY(?) AND is_eligible = true;

-- Tier lookup (uses composite index)
SELECT trading_symbol, company_name 
FROM symbol_metadata
WHERE tier = ? AND is_eligible = true;
```

### 3. Batch Operations

- Validate symbols in batch (single query)
- Bulk insert for metadata backfill
- Parallel computation for metrics (asyncio.gather)

### 4. Denormalization Trade-offs

**Denormalized Fields:**
- `company_name` in `ai_trading_signals` (avoid join on every query)
- `sector` in `symbol_metadata` (computed from sector_map)

**Justification:**
- Read-heavy workload (signals displayed frequently)
- Company names rarely change
- Acceptable staleness (updated daily)

---

## TESTING STRATEGY

### 1. Unit Tests

**Test Coverage:**
- `SymbolValidator` logic (all validation rules)
- Tier assignment algorithm
- Liquidity computation (ADV, candle counts)
- Edge cases (missing data, zero volume, etc.)

**Files:**
- `backend/tests/services/test_symbol_validator.py`
- `backend/tests/services/test_symbol_metrics.py`

### 2. Integration Tests

**Test Scenarios:**
- Event processing with validation (eligible symbols pass)
- Event processing with ineligible symbols (filtered out)
- Signal generation with ineligible symbol (fails gracefully)
- API responses include company name
- Metadata computation job runs successfully

**Files:**
- `backend/tests/integration/test_symbol_validation_flow.py`
- `backend/tests/integration/test_signal_generation_with_validation.py`

### 3. E2E Tests

**Test Scenarios:**
- Frontend displays company names correctly
- Filtering works with company names
- Rejected symbols don't generate signals
- User can search by symbol or company name

**Files:**
- `backend/tests/e2e/test_signal_display.py`

### 4. Performance Tests

**Benchmarks:**
- Validation overhead < 5ms per symbol
- Metadata query < 10ms
- No regression in signal generation latency
- Batch validation of 100 symbols < 50ms

**Files:**
- `backend/tests/performance/test_symbol_validation_perf.py`

---

## MIGRATION STRATEGY

### Zero-Downtime Deployment

**Step 1: Add nullable column**
```sql
ALTER TABLE ai_trading_signals 
ADD COLUMN company_name VARCHAR(200);
```

**Step 2: Backfill in background**
```python
# Run as background job, batched
UPDATE ai_trading_signals s
SET company_name = m.company_name
FROM symbol_metadata m
WHERE m.trading_symbol = s.symbol
AND s.company_name IS NULL
LIMIT 10000;
```

**Step 3: Make non-nullable after backfill**
```sql
ALTER TABLE ai_trading_signals 
ALTER COLUMN company_name SET NOT NULL;
```

### Backward Compatibility

- Frontend gracefully handles missing `company_name` (displays symbol only)
- API version remains v1 (additive change, not breaking)
- Old clients continue to work (ignore new field)

### Rollback Plan

- Keep old serializer logic (feature flag)
- Feature flag for validation (can disable via env var)
- Database changes are additive (can be ignored)

**Feature Flags:**
```python
# backend/app/core/config.py
ENABLE_SYMBOL_VALIDATION: bool = Field(True, description="Enable symbol validation")
ENABLE_COMPANY_NAME_DISPLAY: bool = Field(True, description="Include company names in API")
```

---

## OPEN QUESTIONS (REQUIRES DECISION)

### 1. Nifty 500 Source
**Question**: Should we use NSE API (dynamic) or static config (reliable)?

**Options:**
- **A**: NSE API - Dynamic, always up-to-date, but requires handling API failures
- **B**: Static config - Reliable, but needs quarterly updates

**Recommendation**: Start with B (static), migrate to A in Phase 2

**Decision**: [ PENDING ]

---

### 2. Rejection Behavior
**Question**: When LLM extracts ineligible symbols from news, what should we do?

**Options:**
- **A**: Skip event entirely (strict)
- **B**: Create event with empty `affected_symbols` (lenient)
- **C**: Create event but don't generate signals (middle ground)

**Recommendation**: Option C (preserve event data, prevent bad signals)

**Decision**: [ PENDING ]

---

### 3. User-Requested Symbols
**Question**: If user manually requests signal for ineligible symbol via API, should we:

**Options:**
- **A**: Hard reject with 400 error (fail fast)
- **B**: Generate anyway with warning (permissive)

**Recommendation**: Option A (clear feedback, prevent confusion)

**Decision**: [ PENDING ]

---

### 4. Tier 2 Universe Size
**Question**: Nifty 500 is large. Should we:

**Options:**
- **A**: Support all 500 (resource intensive)
- **B**: Limit to Nifty 200 (balanced)
- **C**: Dynamic based on watchlist popularity

**Recommendation**: Option B initially, expand to A in Phase 2

**Decision**: [ PENDING ]

---

### 5. Company Name Source
**Question**: `instrument_master.name` has full names like "HDFC Bank Ltd.". Should we:

**Options:**
- **A**: Use as-is (official names)
- **B**: Clean/shorten (remove "Ltd.", "Limited")

**Recommendation**: Option A (professional, official)

**Decision**: [ PENDING ]

---

## ESTIMATED EFFORT

| Phase | Tasks | Estimated Time |
|-------|-------|----------------|
| Phase 1 | Database & Infrastructure | 2-3 days |
| Phase 2 | Validation Service | 2-3 days |
| Phase 3 | API & Serialization | 1-2 days |
| Phase 4 | Frontend Integration | 1-2 days |
| Phase 5 | Monitoring & Maintenance | 1 day |
| **Total** | | **7-11 days** |

**Assumptions:**
- Single developer, full-time
- No major blockers or scope changes
- Testing included in each phase

---

## SUCCESS METRICS

### Data Quality
- ✅ 95%+ of signals have eligible symbols
- ✅ <5% event rejection rate due to no eligible symbols
- ✅ Zero signals generated for symbols without OHLCV data

### Performance
- ✅ No regression in signal generation latency (< 500ms p95)
- ✅ Validation overhead < 5ms per symbol
- ✅ Metadata query < 10ms p95

### User Experience
- ✅ User testing confirms company names improve clarity
- ✅ Search by company name works correctly
- ✅ Mobile display is readable (responsive design)

### Reliability
- ✅ Zero production incidents during rollout
- ✅ Graceful degradation if validation service fails
- ✅ Monitoring alerts work correctly

---

## RISKS & MITIGATION

### Risk 1: NSE API Reliability
**Impact**: High  
**Probability**: Medium  
**Mitigation**: Start with static config, add API as enhancement

### Risk 2: Performance Degradation
**Impact**: High  
**Probability**: Low  
**Mitigation**: Comprehensive caching, database indexes, performance tests

### Risk 3: Data Staleness
**Impact**: Medium  
**Probability**: Medium  
**Mitigation**: Daily recompute job, monitoring alerts for stale data

### Risk 4: Migration Complexity
**Impact**: Medium  
**Probability**: Low  
**Mitigation**: Phased rollout, feature flags, rollback plan

---

## NEXT STEPS

1. **Review & Approve Plan**: Stakeholder sign-off on approach
2. **Answer Open Questions**: Make decisions on 5 pending questions
3. **Create Jira/GitHub Issues**: Break down into trackable tasks
4. **Begin Phase 1**: Database schema and infrastructure
5. **Daily Standups**: Track progress, unblock issues

---

## REFERENCES

- NSE Derivative Selection Criteria: https://www.nseindia.com/products-services/equity-derivatives-selection-criteria
- Nifty 500 Index: https://www.nseindia.com/products-services/indices-nifty500-index
- Industry Best Practices: Liquidity filtering in trading platforms
- Internal: `backend/app/core/config.py` (SIGNAL_SCHEDULED_UNIVERSE)
- Internal: `backend/app/services/sector_map.py` (existing sector classification)

---

**Document Version**: 1.0  
**Last Updated**: 2026-05-11  
**Author**: Kiro AI Agent  
**Status**: Awaiting Approval
