# **Full-Scale Implementation Plan: AI Analysis Cards Revamp**
## **Cortex Merge AI-ML — Production-Grade Financial Intelligence Dashboard**

**Created**: 2026-05-11  
**Status**: Planning Phase  
**Owner**: Development Team

---

## **I. EXECUTIVE SUMMARY**

### **Objective**
Transform the AnalysisCardsSection into a world-class financial intelligence dashboard that synthesizes:
1. **ML Technical Pattern Analysis** (chart patterns + historical accuracy)
2. **AI Sentiment Intelligence** (RSS news + NLP sentiment scoring)
3. **Human-Readable Synthesis** (actionable BUY/SELL/HOLD recommendations)

### **Success Criteria**
- **Latency**: p95 < 300ms for all 3 cards (parallel fetch)
- **Accuracy**: ML pattern detection >70%, sentiment classification >85%
- **Reliability**: 99.9% uptime, graceful degradation on failures
- **Security**: JWT auth, rate limiting, audit logging
- **UX**: Real-time updates, skeleton loading, error recovery

---

## **II. ARCHITECTURE OVERVIEW**

### **A. System Components**

```
┌─────────────────────────────────────────────────────────────┐
│                    FRONTEND (React)                         │
├─────────────────────────────────────────────────────────────┤
│  AnalysisCardsSection.tsx                                   │
│  ├─ useQuery: /ml/pattern-analysis                          │
│  ├─ useQuery: /ai/sentiment                                 │
│  └─ useMemo: synthesis (client-side)                        │
└─────────────────────────────────────────────────────────────┘
                            ↓ ↓ ↓
┌─────────────────────────────────────────────────────────────┐
│                  BACKEND API (FastAPI)                      │
├─────────────────────────────────────────────────────────────┤
│  /api/v1/ml/pattern-analysis                                │
│  │  ├─ Pattern Detection Engine (CNN-based)                 │
│  │  ├─ Historical Accuracy Lookup (PostgreSQL)              │
│  │  └─ Redis Cache (5min TTL)                               │
│  │                                                           │
│  /api/v1/ai/sentiment                                       │
│  │  ├─ RSS News Aggregator (existing)                       │
│  │  ├─ FinBERT Sentiment Classifier                         │
│  │  ├─ NLP Impact Scorer                                    │
│  │  └─ Redis Cache (2min TTL)                               │
└─────────────────────────────────────────────────────────────┘
                            ↓ ↓ ↓
┌─────────────────────────────────────────────────────────────┐
│                   DATA LAYER                                │
├─────────────────────────────────────────────────────────────┤
│  PostgreSQL:                                                │
│  ├─ ml_prediction_outcomes (unified ML tracking)            │
│  │    ├─ Pure predictions (pattern detection, sentiment)    │
│  │    ├─ Execution data (if traded via paper/live)          │
│  │    └─ Outcome measurement (all predictions)              │
│  ├─ paper_trade_outcomes (existing, for execution P&L)      │
│  ├─ ai_raw_events (RSS news)                                │
│  ├─ ai_nlp_results (sentiment scores)                       │
│  └─ ohlcv_data (candle data for pattern detection)          │
│                                                              │
│  Redis:                                                     │
│  ├─ pattern:{instrument_key}:{timeframe} (5min)             │
│  └─ sentiment:{instrument_key} (2min)                       │
└─────────────────────────────────────────────────────────────┘
```

---

## **III. UNIFIED ML PREDICTION TRACKING SYSTEM**

### **A. Architecture Decision: Single Source of Truth**

The `ml_prediction_outcomes` table serves as a **unified tracking system** that combines:
1. **Pure ML predictions** (pattern detection, sentiment analysis, ensemble outputs)
2. **Execution data** (if prediction was acted upon via paper/live trading)
3. **Outcome measurement** (price movement validation for ALL predictions)

### **B. Key Benefits**

✅ **Complete ML Governance**: Track accuracy of ALL predictions, not just traded ones  
✅ **Slippage Analysis**: Compare predicted entry vs actual execution price  
✅ **Confidence Calibration**: Validate if HIGH confidence predictions actually perform better  
✅ **Pattern Performance**: Measure which patterns have highest success rates  
✅ **Traded vs Non-Traded**: Compare execution performance vs pure ML accuracy  
✅ **Single Query**: Get historical stats without joining multiple tables

### **C. Data Flow**

```
┌─────────────────────────────────────────────────────────────┐
│  1. ML PREDICTION GENERATED                                 │
│     ├─ Pattern detected: BULLISH_ENGULFING                  │
│     ├─ Confidence: 0.82 (HIGH)                              │
│     ├─ Direction: BUY                                       │
│     └─ INSERT INTO ml_prediction_outcomes                   │
│         (was_traded=FALSE, outcome_status=PENDING)          │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  2. USER TRADES (OPTIONAL)                                  │
│     ├─ Paper/Live position opened                           │
│     └─ UPDATE ml_prediction_outcomes                        │
│         SET was_traded=TRUE,                                │
│             actual_entry_price=2455.00,                     │
│             portfolio_id=..., position_id=...               │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  3. OUTCOME MEASUREMENT (ALL PREDICTIONS)                   │
│     ├─ Background job runs daily                            │
│     ├─ Fetch OHLCV for measurement_window_days (5 days)     │
│     ├─ Calculate: max_price, min_price, TP/SL hits          │
│     └─ UPDATE ml_prediction_outcomes                        │
│         SET outcome_status='SUCCESS',                       │
│             ml_direction_correct=TRUE,                      │
│             hit_predicted_tp1=TRUE, ...                     │
└─────────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  4. POSITION CLOSED (IF TRADED)                             │
│     ├─ Paper/Live position closed                           │
│     └─ UPDATE ml_prediction_outcomes                        │
│         SET actual_exit_price=2620.00,                      │
│             gross_pnl=1700.00, net_pnl=1654.50,             │
│             exit_reason='TP2', closed_at=...                │
└─────────────────────────────────────────────────────────────┘
```

### **D. Query Examples**

#### **1. Overall Pattern Accuracy**
```sql
SELECT 
    pattern_name,
    pattern_timeframe,
    confidence_level,
    COUNT(*) as total_predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as accuracy,
    AVG(final_move_pct) as avg_move_pct,
    AVG(CASE WHEN hit_predicted_tp1 THEN 1.0 ELSE 0.0 END) as tp1_hit_rate
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
  AND pattern_name IS NOT NULL
GROUP BY pattern_name, pattern_timeframe, confidence_level
ORDER BY accuracy DESC;
```

#### **2. Traded vs Non-Traded Performance**
```sql
SELECT 
    was_traded,
    COUNT(*) as predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as ml_accuracy,
    AVG(final_move_pct) as avg_move,
    AVG(net_pnl) FILTER (WHERE was_traded) as avg_pnl_if_traded,
    AVG(entry_slippage_pct) FILTER (WHERE was_traded) as avg_slippage
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
GROUP BY was_traded;
```

#### **3. Confidence Calibration**
```sql
-- Are HIGH confidence predictions actually better?
SELECT 
    confidence_level,
    COUNT(*) as predictions,
    AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as accuracy,
    AVG(max_favorable_move_pct) as avg_best_move,
    AVG(max_adverse_move_pct) as avg_worst_move
FROM ml_prediction_outcomes
WHERE outcome_status IN ('SUCCESS', 'FAILURE')
GROUP BY confidence_level
ORDER BY 
    CASE confidence_level 
        WHEN 'HIGH' THEN 1 
        WHEN 'MEDIUM' THEN 2 
        WHEN 'LOW' THEN 3 
    END;
```

### **E. Integration with Existing Paper Trading**

The unified table **complements** (not replaces) the existing `paper_trade_outcomes` table:

| Table | Purpose | Scope |
|-------|---------|-------|
| `ml_prediction_outcomes` | **ML governance** - Track all predictions + outcomes | ALL predictions (traded + non-traded) |
| `paper_trade_outcomes` | **Trading audit** - Track execution P&L + fills | ONLY traded positions |

**Foreign Key Links**:
- `ml_prediction_outcomes.position_id` → `paper_positions.id`
- `ml_prediction_outcomes.portfolio_id` → `portfolios.id`
- `ml_prediction_outcomes.suggestion_id` → `trade_suggestions.suggestion_id`

When a prediction is traded:
1. Row exists in `ml_prediction_outcomes` (with `was_traded=TRUE`)
2. Row exists in `paper_trade_outcomes` (with execution details)
3. Both linked via `position_id`

---

## **IV. DETAILED COMPONENT BREAKDOWN**

### **A. Card 1: ML Pattern Analysis**

#### **1. Backend: Pattern Detection Engine**

**File**: `backend/app/ml/pattern_detection/engine.py` (NEW)

**Technology Stack**:
- **Model**: CNN-based pattern classifier (YOLOv8 or EfficientNet fine-tuned on candlestick patterns)
- **Patterns**: 15 major patterns (Head & Shoulders, Bull/Bear Flag, Triangle, Wedge, Double Top/Bottom, etc.)
- **Input**: Last 100 candles (OHLCV) → normalized image representation
- **Output**: Pattern name, confidence, timeframe, historical accuracy

**Key Features**:
- **Real-time detection**: Analyze last 100 candles on-demand
- **Historical accuracy**: Query past detections → measure success rate (% of predictions that moved in predicted direction within N days)
- **Multi-timeframe**: Support 1H, 4H, 1D, 1W
- **Confidence threshold**: Only return patterns with >60% confidence

**Database Schema** (NEW):

**Unified ML Prediction Tracking**: `ml_prediction_outcomes`

This table combines pure ML predictions with execution data from paper/live trading, providing a single source of truth for all ML predictions and their outcomes.

```sql
CREATE TABLE ml_prediction_outcomes (
    -- ═══════════════════════════════════════════════════════════════════════
    -- PRIMARY KEY & INSTRUMENT
    -- ═══════════════════════════════════════════════════════════════════════
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    symbol VARCHAR(50) NOT NULL,
    instrument_key VARCHAR(100) NOT NULL,
    predicted_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    -- ═══════════════════════════════════════════════════════════════════════
    -- ML PREDICTION DATA
    -- ═══════════════════════════════════════════════════════════════════════
    model_version VARCHAR(50) NOT NULL,
    prediction_type VARCHAR(50) NOT NULL,  -- 'PATTERN', 'SENTIMENT', 'ENSEMBLE', 'FUSION'
    
    -- Pattern Detection (if applicable)
    pattern_name VARCHAR(50),              -- 'BULLISH_ENGULFING', 'HEAD_SHOULDERS', etc.
    pattern_timeframe VARCHAR(10),         -- '1D', '4H', '1H'
    pattern_confidence DECIMAL(5,4),       -- 0.0000 to 1.0000
    
    -- ML Signal
    signal_direction VARCHAR(4) NOT NULL,  -- 'BUY', 'SELL'
    confidence_score DECIMAL(5,4) NOT NULL,
    confidence_level VARCHAR(10),          -- 'HIGH', 'MEDIUM', 'LOW'
    
    -- Price Targets (predicted)
    predicted_entry_price DECIMAL(12,4) NOT NULL,
    predicted_stop_loss DECIMAL(12,4),
    predicted_tp1 DECIMAL(12,4),
    predicted_tp2 DECIMAL(12,4),
    predicted_tp3 DECIMAL(12,4),
    
    -- Additional ML Metadata
    model_probabilities JSONB,             -- {"up": 0.65, "down": 0.20, "hold": 0.15}
    feature_importance JSONB,              -- Top contributing features
    sentiment_score DECIMAL(6,2),          -- -100 to +100 (if sentiment-based)
    consensus_score DECIMAL(5,2),          -- Multi-model agreement
    
    -- Market Context at Prediction Time
    market_regime VARCHAR(50),             -- 'bullish', 'bearish', 'neutral', 'volatile'
    volatility DECIMAL(6,4),
    entry_market_price DECIMAL(12,4),      -- Actual market price when predicted
    
    -- ═══════════════════════════════════════════════════════════════════════
    -- EXECUTION DATA (NULL if prediction was not traded)
    -- ═══════════════════════════════════════════════════════════════════════
    was_traded BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Link to Paper/Live Trading
    portfolio_id UUID REFERENCES portfolios(id) ON DELETE SET NULL,
    position_id UUID REFERENCES paper_positions(id) ON DELETE SET NULL,
    suggestion_id UUID REFERENCES trade_suggestions(suggestion_id) ON DELETE SET NULL,
    
    -- Actual Execution Prices (if traded)
    actual_entry_price DECIMAL(12,4),
    actual_exit_price DECIMAL(12,4),
    actual_quantity INTEGER,
    
    -- Entry Slippage
    entry_slippage_bps DECIMAL(6,2),       -- Basis points from predicted entry
    entry_slippage_pct DECIMAL(6,4),       -- % deviation
    
    -- Execution Costs (if traded)
    total_charges DECIMAL(10,4),
    brokerage DECIMAL(10,4),
    stt DECIMAL(10,4),
    exchange_charges DECIMAL(10,4),
    gst DECIMAL(10,4),
    
    -- P&L (if traded and closed)
    gross_pnl DECIMAL(14,4),
    net_pnl DECIMAL(14,4),
    pnl_pct DECIMAL(8,4),
    
    -- Hold Duration (if traded)
    opened_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    hold_duration_seconds INTEGER,
    
    -- Exit Details (if traded)
    exit_reason VARCHAR(10),               -- 'TP1', 'TP2', 'TP3', 'SL', 'MANUAL', 'EXPIRED'
    
    -- ═══════════════════════════════════════════════════════════════════════
    -- OUTCOME MEASUREMENT (computed for ALL predictions, traded or not)
    -- ═══════════════════════════════════════════════════════════════════════
    outcome_status VARCHAR(20) NOT NULL DEFAULT 'PENDING',  
    -- 'PENDING', 'MEASURING', 'SUCCESS', 'FAILURE', 'EXPIRED'
    
    outcome_measured_at TIMESTAMP WITH TIME ZONE,
    measurement_window_days INTEGER DEFAULT 5,  -- How many days to track
    
    -- Price Movement Validation (measured from entry_market_price)
    max_price_reached DECIMAL(12,4),       -- Highest price in measurement window
    min_price_reached DECIMAL(12,4),       -- Lowest price in measurement window
    price_at_window_end DECIMAL(12,4),     -- Price at end of measurement window
    
    -- ML Feedback (computed)
    ml_direction_correct BOOLEAN,          -- Did price move in predicted direction?
    hit_predicted_tp1 BOOLEAN DEFAULT FALSE,
    hit_predicted_tp2 BOOLEAN DEFAULT FALSE,
    hit_predicted_tp3 BOOLEAN DEFAULT FALSE,
    hit_predicted_sl BOOLEAN DEFAULT FALSE,
    
    -- Movement Metrics
    max_favorable_move_pct DECIMAL(8,4),   -- Best move in predicted direction
    max_adverse_move_pct DECIMAL(8,4),     -- Worst move against prediction
    final_move_pct DECIMAL(8,4),           -- Move at window end
    days_to_tp1 INTEGER,                   -- How long to reach TP1 (if hit)
    days_to_sl INTEGER,                    -- How long to hit SL (if hit)
    
    -- Risk/Reward Realized
    actual_risk_reward_ratio DECIMAL(6,2), -- Actual R:R achieved
    predicted_risk_reward_ratio DECIMAL(6,2), -- Predicted R:R
    
    -- ═══════════════════════════════════════════════════════════════════════
    -- METADATA
    -- ═══════════════════════════════════════════════════════════════════════
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    notes TEXT,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT ck_signal_direction CHECK (signal_direction IN ('BUY', 'SELL')),
    CONSTRAINT ck_outcome_status CHECK (outcome_status IN ('PENDING', 'MEASURING', 'SUCCESS', 'FAILURE', 'EXPIRED')),
    CONSTRAINT ck_confidence_level CHECK (confidence_level IN ('HIGH', 'MEDIUM', 'LOW') OR confidence_level IS NULL)
);

-- Indexes for fast queries
CREATE INDEX idx_ml_pred_outcomes_symbol_predicted ON ml_prediction_outcomes(symbol, predicted_at DESC);
CREATE INDEX idx_ml_pred_outcomes_status ON ml_prediction_outcomes(outcome_status, predicted_at DESC);
CREATE INDEX idx_ml_pred_outcomes_pattern ON ml_prediction_outcomes(pattern_name, pattern_timeframe) WHERE pattern_name IS NOT NULL;
CREATE INDEX idx_ml_pred_outcomes_confidence ON ml_prediction_outcomes(confidence_level, ml_direction_correct);
CREATE INDEX idx_ml_pred_outcomes_traded ON ml_prediction_outcomes(was_traded, outcome_status);
CREATE INDEX idx_ml_pred_outcomes_user ON ml_prediction_outcomes(user_id, predicted_at DESC);
```

**Key Features**:
- ✅ **Tracks ALL predictions** (traded and non-traded)
- ✅ **Unified analytics** (compare execution vs pure ML accuracy)
- ✅ **Complete audit trail** (every prediction + outcome)
- ✅ **Pattern-specific metrics** (which patterns work best)
- ✅ **Confidence calibration** (are HIGH confidence predictions actually better?)
- ✅ **Slippage analysis** (predicted vs actual entry)
- ✅ **Integrates with existing paper trading system** (via foreign keys)

**API Endpoint**:
```python
# backend/app/api/v1/ml_patterns.py (NEW)

@router.get("/pattern-analysis")
@limiter.limit("60/minute")
async def get_pattern_analysis(
    instrument_key: str,
    timeframe: str = "4H",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> PatternAnalysisResponse:
    """
    Returns current pattern detection + historical accuracy from ml_prediction_outcomes.
    
    Response:
    {
        "pattern_name": "bull_flag",
        "timeframe": "4H",
        "confidence": 0.82,
        "direction": "BUY",
        "entry_price": 2450.00,
        "stop_loss": 2380.00,
        "target_price": 2620.00,
        "historical_accuracy": 0.73,
        "historical_stats": {
            "avg_move_pct": 8.2,
            "avg_time_days": 4.5,
            "sample_size": 127,
            "tp1_hit_rate": 0.68,
            "tp2_hit_rate": 0.42,
            "sl_hit_rate": 0.27
        },
        "detected_at": "2026-05-11T19:00:00Z"
    }
    
    Historical stats computed from:
    SELECT 
        COUNT(*) as sample_size,
        AVG(CASE WHEN ml_direction_correct THEN 1.0 ELSE 0.0 END) as accuracy,
        AVG(final_move_pct) as avg_move_pct,
        AVG(EXTRACT(EPOCH FROM (outcome_measured_at - predicted_at)) / 86400.0) as avg_time_days,
        AVG(CASE WHEN hit_predicted_tp1 THEN 1.0 ELSE 0.0 END) as tp1_hit_rate,
        AVG(CASE WHEN hit_predicted_tp2 THEN 1.0 ELSE 0.0 END) as tp2_hit_rate,
        AVG(CASE WHEN hit_predicted_sl THEN 1.0 ELSE 0.0 END) as sl_hit_rate
    FROM ml_prediction_outcomes
    WHERE pattern_name = :pattern_name
      AND pattern_timeframe = :timeframe
      AND outcome_status IN ('SUCCESS', 'FAILURE')
      AND confidence_level = 'HIGH';
    """
    ```

#### **2. Frontend: ML Predictions Card**

**Updates to**: `frontend/src/components/AnalysisCardsSection.tsx`

```tsx
const mlPatternQuery = useQuery({
  queryKey: ["ml-pattern", instrumentKey, "4H"],
  queryFn: async () => {
    const response = await api.get(`/ml/pattern-analysis`, {
      params: { instrument_key: instrumentKey, timeframe: "4H" },
    });
    return response.data as PatternAnalysisResponse;
  },
  enabled: canQuery,
  staleTime: 300_000, // 5 minutes
});
```

**UI Design**:
- **Pattern badge**: Visual icon for pattern type
- **Confidence meter**: Circular progress (0-100%)
- **Historical accuracy**: "73% success rate (127 samples)"
- **Price targets**: Entry, SL, TP with visual price ladder
- **Timeframe selector**: Toggle between 1H/4H/1D

---

### **B. Card 3: AI Sentiment Analysis**

#### **1. Backend: Sentiment Intelligence Engine**

**File**: `backend/app/ai/intelligence/sentiment_engine.py` (ENHANCE EXISTING)

**Technology Stack**:
- **News Source**: Existing RSS fetcher (`rss_fetcher.py`)
- **NLP Model**: FinBERT (ProsusAI/finbert) for financial sentiment
- **Entity Extraction**: spaCy for company/ticker matching
- **Impact Scoring**: Weighted sentiment aggregation

**Key Features**:
- **Real-time news**: Fetch last 24h/7d news for instrument
- **Sentiment classification**: Positive/Negative/Neutral with confidence
- **Impact scoring**: -100 to +100 (weighted by source credibility + recency)
- **Event clustering**: Group related news (e.g., earnings, regulatory)

**Database Schema** (ENHANCE EXISTING):
```sql
-- Add to ai_nlp_results table
ALTER TABLE ai_nlp_results ADD COLUMN instrument_key VARCHAR(50);
ALTER TABLE ai_nlp_results ADD COLUMN impact_score DECIMAL(5,2);  -- -100 to +100
ALTER TABLE ai_nlp_results ADD COLUMN event_category VARCHAR(50);  -- 'earnings', 'regulatory', etc.

CREATE INDEX idx_nlp_instrument ON ai_nlp_results(instrument_key, created_at DESC);
```

**API Endpoint**:
```python
# backend/app/api/v1/ai_sentiment.py (NEW)

@router.get("/sentiment")
@limiter.limit("60/minute")
async def get_sentiment_analysis(
    instrument_key: str,
    lookback_hours: int = 24,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> SentimentAnalysisResponse:
    """
    Returns:
    {
        "total_events": 12,
        "positive_count": 8,
        "negative_count": 2,
        "neutral_count": 2,
        "impact_score": 68,  // -100 to +100
        "sentiment_label": "bullish",
        "top_event": {
            "title": "Company beats Q4 earnings by 15%",
            "sentiment": "positive",
            "confidence": 0.92,
            "published_at": "2026-05-11T10:30:00Z"
        },
        "prediction": "Positive indicator for upcoming movement",
        "last_updated": "2026-05-11T19:00:00Z"
    }
    ```

#### **2. Frontend: AI Sentiment Card**

```tsx
const sentimentQuery = useQuery({
  queryKey: ["ai-sentiment", instrumentKey],
  queryFn: async () => {
    const response = await api.get(`/ai/sentiment`, {
      params: { instrument_key: instrumentKey, lookback_hours: 24 },
    });
    return response.data as SentimentAnalysisResponse;
  },
  enabled: canQuery,
  staleTime: 120_000, // 2 minutes
});
```

**UI Design**:
- **Event count**: "12 events in last 24h"
- **Sentiment breakdown**: Horizontal bar (green/red/gray segments)
- **Impact score**: Large number with color coding (-100 to +100)
- **Top event**: Headline preview with timestamp
- **Prediction badge**: "Bullish Indicator" / "Bearish Indicator"

---

### **C. Card 2: Prediction Summary (Synthesis)**

#### **1. Frontend: Client-Side Synthesis**

**Logic**: Combine ML + AI data into human-readable narrative

```tsx
const synthesis = useMemo(() => {
  if (!mlPatternQuery.data || !sentimentQuery.data) return null;

  const ml = mlPatternQuery.data;
  const ai = sentimentQuery.data;

  // Determine overall recommendation
  const mlBullish = ml.direction === "BUY";
  const aiBullish = ai.impact_score > 20;
  
  let recommendation: "BUY" | "SELL" | "HOLD";
  let confidence: number;
  
  if (mlBullish && aiBullish) {
    recommendation = "BUY";
    confidence = (ml.confidence + (ai.impact_score + 100) / 200) / 2;
  } else if (!mlBullish && !aiBullish) {
    recommendation = "SELL";
    confidence = (ml.confidence + (100 - ai.impact_score) / 200) / 2;
  } else {
    recommendation = "HOLD";
    confidence = 0.5;
  }

  return {
    ml_insight: `ML detected ${ml.pattern_name} on ${ml.timeframe}, historically leads to ${ml.historical_stats.avg_move_pct}% move in ${ml.historical_stats.avg_time_days} days`,
    ai_insight: `AI found ${ai.total_events} news events: ${ai.positive_count} positive, ${ai.negative_count} negative. NLP impact: ${ai.impact_score > 0 ? '+' : ''}${ai.impact_score}`,
    recommendation,
    confidence,
    action: `Consider ${recommendation === "BUY" ? "buying" : recommendation === "SELL" ? "selling" : "holding"} ${recommendation !== "HOLD" ? `above ₹${ml.entry_price} with SL at ₹${ml.stop_loss}` : ""}`,
  };
}, [mlPatternQuery.data, sentimentQuery.data]);
```

**UI Design**:
- **ML Insight**: Icon + text summary
- **AI Insight**: Icon + text summary
- **Conclusion**: Large BUY/SELL/HOLD badge with confidence meter
- **Action**: Actionable text with price levels
- **Disclaimer**: "AI-generated analysis. Not financial advice."

---

## **IV. IMPLEMENTATION PHASES**

### **Phase 1: Backend Foundation** (Week 1)
1. **Pattern Detection Engine**
   - [ ] Research & select pattern detection approach (TA-Lib + PatternPy)
   - [ ] Implement `PatternDetectionEngine` class with TA-Lib integration
   - [ ] Create `ml_prediction_outcomes` unified tracking table
   - [ ] Build pattern detection service (60+ candlestick patterns)
   - [ ] Implement background outcome measurement job
   - [ ] Build `/ml/pattern-analysis` endpoint
   - [ ] Add Redis caching layer (5min TTL)
   - [ ] Write unit tests (>80% coverage)

2. **Sentiment Intelligence Engine**
   - [ ] Integrate FinBERT model (ProsusAI/finbert) with ONNX optimization
   - [ ] Enhance `NLPEngine` with instrument matching
   - [ ] Implement impact scoring algorithm
   - [ ] Update database schema (ai_nlp_results enhancements)
   - [ ] Build `/ai/sentiment` endpoint
   - [ ] Add Redis caching layer (2min TTL)
   - [ ] Write unit tests (>80% coverage)

3. **Historical Accuracy System**
   - [ ] Implement background job to measure prediction outcomes
   - [ ] Backtest on 10 years of historical OHLCV data (2016-2026)
   - [ ] Populate `ml_prediction_outcomes` with historical pattern detections
   - [ ] Build aggregation queries for pattern-specific accuracy
   - [ ] Create admin dashboard for ML governance metrics

### **Phase 2: Frontend Integration** (Week 2)
1. **Card Components**
   - [ ] Refactor `AnalysisCardsSection.tsx`
   - [ ] Create `MLPatternCard.tsx`
   - [ ] Create `AISentimentCard.tsx`
   - [ ] Create `PredictionSummaryCard.tsx`
   - [ ] Implement synthesis logic
   - [ ] Add skeleton loading states
   - [ ] Add error boundaries
   - [ ] Implement retry logic

2. **UI/UX Polish**
   - [ ] Design pattern icons (15 patterns)
   - [ ] Create confidence meters
   - [ ] Build sentiment breakdown chart
   - [ ] Add animations (fade-in, pulse)
   - [ ] Responsive design (mobile/tablet)
   - [ ] Accessibility (ARIA labels, keyboard nav)

### **Phase 3: Production Hardening** (Week 3)
1. **Performance**
   - [ ] Parallel query execution (Promise.all)
   - [ ] Redis cache warming (background job)
   - [ ] Database query optimization (indexes)
   - [ ] CDN for static assets
   - [ ] Lazy loading for non-critical data

2. **Reliability**
   - [ ] Circuit breaker for external APIs
   - [ ] Graceful degradation (show partial data)
   - [ ] Rate limiting (60 req/min per user)
   - [ ] Audit logging (all predictions)
   - [ ] Monitoring (Prometheus + Grafana)

3. **Security**
   - [ ] JWT authentication
   - [ ] Input validation (Pydantic)
   - [ ] SQL injection prevention (parameterized queries)
   - [ ] XSS prevention (sanitize outputs)
   - [ ] CORS configuration

### **Phase 4: Testing & Deployment** (Week 4)
1. **Testing**
   - [ ] Unit tests (backend: >80%, frontend: >70%)
   - [ ] Integration tests (API endpoints)
   - [ ] E2E tests (Playwright)
   - [ ] Load testing (Locust: 1000 req/s)
   - [ ] Security testing (OWASP Top 10)

2. **Deployment**
   - [ ] Staging deployment
   - [ ] Smoke tests
   - [ ] Production deployment (blue-green)
   - [ ] Monitoring setup
   - [ ] Documentation (API docs, user guide)

---

## **V. TECHNICAL SPECIFICATIONS**

### **A. API Contracts**

#### **1. GET /api/v1/ml/pattern-analysis**
```typescript
interface PatternAnalysisResponse {
  pattern_name: string;
  timeframe: string;
  confidence: number;
  direction: "BUY" | "SELL";
  entry_price: number;
  stop_loss: number;
  target_price: number;
  historical_accuracy: number;
  historical_stats: {
    avg_move_pct: number;
    avg_time_days: number;
    sample_size: number;
  };
  detected_at: string;
}
```

#### **2. GET /api/v1/ai/sentiment**
```typescript
interface SentimentAnalysisResponse {
  total_events: number;
  positive_count: number;
  negative_count: number;
  neutral_count: number;
  impact_score: number;  // -100 to +100
  sentiment_label: "bullish" | "bearish" | "neutral";
  top_event: {
    title: string;
    sentiment: string;
    confidence: number;
    published_at: string;
  };
  prediction: string;
  last_updated: string;
}
```

### **B. Performance Targets**

| Metric | Target | Measurement |
|--------|--------|-------------|
| API Latency (p95) | < 300ms | Prometheus |
| Cache Hit Rate | > 80% | Redis metrics |
| ML Accuracy | > 70% | Backtesting |
| Sentiment Accuracy | > 85% | Manual validation |
| Uptime | 99.9% | Uptime Robot |
| Error Rate | < 0.1% | Sentry |

### **C. Security Requirements**

1. **Authentication**: JWT with 1h expiry
2. **Rate Limiting**: 60 req/min per user
3. **Input Validation**: Pydantic schemas
4. **Audit Logging**: All predictions logged
5. **Data Encryption**: TLS 1.3, encrypted DB fields
6. **GDPR Compliance**: User data anonymization

---

## **VI. RISKS & MITIGATION**

| Risk | Impact | Probability | Mitigation |
|------|--------|-------------|------------|
| ML model accuracy < 70% | High | Medium | Extensive training data, ensemble methods |
| FinBERT latency > 500ms | Medium | Low | Model quantization, GPU inference |
| RSS feeds down | Medium | Medium | Multiple sources, fallback to cached data |
| Database overload | High | Low | Connection pooling, read replicas |
| API rate limits (Upstox) | High | Medium | Aggressive caching, batch requests |

---

## **VII. SUCCESS METRICS**

### **Business Metrics**
- **User Engagement**: 40% increase in DetailPane time-on-page
- **Conversion**: 25% increase in paper trade entries from analysis cards
- **Retention**: 15% increase in weekly active users

### **Technical Metrics**
- **Latency**: p95 < 300ms
- **Availability**: 99.9% uptime
- **Accuracy**: ML >70%, Sentiment >85%
- **Cache Hit Rate**: >80%

---

## **VIII. OPEN QUESTIONS**

1. **Pattern Detection Approach**: ✅ **RESOLVED** - Use TA-Lib (60+ candlestick patterns) + PatternPy (chart patterns)
2. **Historical Accuracy Tracking**: ✅ **RESOLVED** - Unified `ml_prediction_outcomes` table tracks all predictions (traded + non-traded)
3. **Training Data**: ✅ **RESOLVED** - Backtest on 10 years of historical OHLCV data (2016-2026)
4. **Sentiment Sources**: Add Twitter/Reddit or stick to RSS?
5. **Real-time Updates**: WebSocket push or polling?
6. **Synthesis Logic**: Client-side or server-side?
7. **Timeframe Selection**: User-configurable or auto-detect?
8. **Measurement Window**: ✅ **RESOLVED** - 5 days default (configurable per prediction)
9. **Caching Strategy**: Redis TTL (2min for sentiment, 5min for patterns)
10. **FinBERT Optimization**: ✅ **RESOLVED** - ONNX quantization for 50-80ms latency

---

## **IX. NEXT STEPS**

**Immediate Actions**:
1. **Confirm architecture** with stakeholders
2. **Answer open questions** (above)
3. **Provision resources** (GPU for ML training, FinBERT model)
4. **Set up project tracking** (Jira/Linear)
5. **Begin Phase 1** (Backend Foundation)

**Your Input Needed**:
- Approve architecture & tech stack
- Answer open questions
- Confirm timeline (4 weeks realistic?)
- Budget for GPU compute (training + inference)

---

## **X. REFERENCES**

### **Research Papers**
- [Enhancing market trend prediction using CNNs on Japanese candlestick patterns](https://pmc.ncbi.nlm.nih.gov/articles/PMC11935771/)
- [A Serverless Architecture for Real-Time Stock Analysis using LLMs](https://arxiv.org/html/2507.09583)
- [Financial Sentiment Analysis for Algorithmic Trading](https://arxiv.org/html/2403.12285v1)
- [Adaptive Financial Sentiment Analysis via LLMs and RAG](https://arxiv.org/html/2512.20082v1)

### **Technology Stack**
- **Pattern Detection**: YOLOv8 / EfficientNet
- **Sentiment Analysis**: FinBERT (ProsusAI/finbert)
- **Entity Extraction**: spaCy
- **Backend**: FastAPI, PostgreSQL, Redis
- **Frontend**: React, TanStack Query, TypeScript

---

**Document Version**: 2.0  
**Last Updated**: 2026-05-11 20:10 IST  
**Status**: Updated with Unified ML Prediction Tracking System  
**Key Changes**:
- ✅ Replaced `ml_pattern_detections` with unified `ml_prediction_outcomes` table
- ✅ Added comprehensive tracking for traded + non-traded predictions
- ✅ Integrated with existing `paper_trade_outcomes` system
- ✅ Resolved pattern detection approach (TA-Lib + PatternPy)
- ✅ Resolved historical accuracy tracking (10 years backtest on OHLCV data)
