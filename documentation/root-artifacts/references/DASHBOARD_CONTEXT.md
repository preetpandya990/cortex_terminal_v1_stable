# Dashboard Context - Cortex AI

## Overview
The Cortex AI Dashboard (`/cai-dashboard`) is a real-time trading intelligence dashboard that aggregates data from multiple AI/ML microservices. It provides traders with signals, market regime analysis, event intelligence, and ML model monitoring.

## Architecture

### Frontend Structure
```
frontend/src/
├── app/cai-dashboard/page.tsx          # Main dashboard page
├── components/
│   ├── ai/
│   │   ├── CAIRealtimeDemo.tsx         # Main dashboard component
│   │   ├── SignalsPanel.tsx            # Trading signals display
│   │   ├── RegimePanel.tsx             # Market regime analysis
│   │   ├── EventsPanel.tsx             # High-impact events
│   │   ├── MLModelsPanel.tsx           # ML model monitoring
│   │   ├── SignalDetailModal.tsx       # Signal details popup
│   │   └── InstrumentRegimeModal.tsx   # Instrument regime details
│   └── dashboard/
│       ├── DashboardMarketPanel.tsx    # Live price & market status
│       └── LivePriceVolumeChart.tsx    # Real-time chart
├── hooks/
│   ├── useSignals.ts                   # Signals data fetching
│   ├── useSignalsRealtime.ts           # Signals WebSocket
│   ├── useRegime.ts                    # Regime data fetching
│   ├── useEvents.ts                    # Events data fetching
│   ├── useEventsRealtime.ts            # Events WebSocket
│   ├── useModels.ts                    # Models data fetching
│   └── useModelsRealtime.ts            # Models WebSocket
└── types/
    ├── signals.ts                      # Signal type definitions
    ├── events.ts                       # Event type definitions
    ├── models.ts                       # Model type definitions
    └── regime.ts                       # Regime type definitions
```

### Backend API Structure
```
backend/app/
├── api/v1/
│   ├── cai.py                          # Dashboard stats endpoint
│   ├── fusion.py                       # Trading signals API
│   ├── intelligence.py                 # Events API
│   ├── governance.py                   # ML models API
│   ├── strategy.py                     # Market regime API
│   ├── ml_drift.py                     # Drift detection API
│   └── safety.py                       # Kill switch API
└── ai/fusion/models.py                 # AI data models
```

## Dashboard Components

### 1. Trading Signals Panel (`SignalsPanel.tsx`)
**Purpose**: Display real-time trading signals with filtering and pagination

**Features**:
- Signal type filtering (BUY/SELL/HOLD)
- Symbol search
- Time horizon filtering (intraday/swing/positional)
- Confidence threshold filtering
- Pagination support
- Real-time updates via WebSocket
- Signal detail modal

**Data Source**: 
- API: `GET /api/v1/fusion/signals`
- WebSocket: `/api/v1/fusion/ws/signals`
- Hook: `useSignals()`, `useSignalsRealtime()`

**Key Fields**:
- `signal_type`: buy/sell/hold
- `calibrated_confidence`: 0-1 confidence score
- `target_price`, `stop_loss`: price levels
- `time_horizon`: intraday/swing/positional
- `reasoning`: AI-generated explanation
- `contributing_factors`: events, ML predictions, technical indicators

### 2. Market Regime Panel (`RegimePanel.tsx`)
**Purpose**: Display market regime classification for indices and stocks

**Features**:
- Macro overview (Nifty 50 aggregate regime)
- Market breadth visualization
- 10 Nifty indices grid (IT, Bank, Pharma, etc.)
- Constituent stocks list with regime classification
- Stock search functionality
- Instrument detail modal with indicators

**Data Source**:
- API: `GET /api/v1/strategy/market/overview`
- API: `GET /api/v1/strategy/index/{key}/constituents`
- Hook: `useMarketOverview()`, `useIndexConstituents()`

**Regime Types**:
- `bull_trending`: Strong uptrend
- `bear_trending`: Strong downtrend
- `sideways_range`: Range-bound
- `high_volatility`: Volatile conditions
- `low_liquidity`: Low volume

**Key Indicators**:
- ADX (trend strength)
- RSI (momentum)
- Bollinger Bands
- VWAP

### 3. Events Panel (`EventsPanel.tsx`)
**Purpose**: Display high-impact market events with credibility scoring

**Features**:
- Event type filtering (earnings, M&A, regulatory, etc.)
- Impact score filtering
- Symbol filtering
- Fake news detection (4-layer system)
- Credibility scoring
- Sentiment analysis
- Pagination

**Data Source**:
- API: `GET /api/v1/intelligence/events`
- WebSocket: `/api/v1/intelligence/ws/events`
- Hook: `useEvents()`, `useEventsRealtime()`

**Event Types**:
- Earnings announcement
- Merger & acquisition
- Regulatory change
- Management change
- Product launch
- Legal issue
- Market rumor
- Analyst rating
- Dividend/buyback/split

**Fake News Detection**:
- Layer 1: Source credibility check
- Layer 2: Cross-reference verification
- Layer 3: Sentiment consistency
- Layer 4: LLM-based analysis

### 4. ML Models Panel (`MLModelsPanel.tsx`)
**Purpose**: Monitor ML model health, drift, and deployment state

**Features**:
- Model state management (shadow/paper/live/retired)
- Drift detection alerts
- Accuracy metrics display
- Admin controls for state transitions
- Last prediction timestamp
- Model version tracking

**Data Source**:
- API: `GET /api/v1/governance/models`
- API: `GET /api/v1/ml/drift/reports`
- Hook: `useModels()`, `useDriftReports()`

**Model States**:
- `shadow`: Testing in background
- `paper`: Paper trading mode
- `live`: Production serving
- `retired`: Deprecated

**Drift Metrics**:
- Drift score (0-1)
- KS test results
- Feature drift detection
- Prediction distribution shift

### 5. Dashboard Market Panel (`DashboardMarketPanel.tsx`)
**Purpose**: Display live price and market status

**Features**:
- Real-time price updates (5s polling with exponential backoff)
- Market hours detection (IST timezone)
- Live price chart (1-minute candles)
- Volume visualization
- AbortController for request cancellation
- Exponential backoff on API failures (5s → 60s cap)

**Data Source**:
- API: `GET /api/v1/market-data/live/{instrument_key}`
- API: `GET /api/v1/upstox/candles/intraday`

## API Endpoints

### Dashboard Stats
```
GET /api/v1/cai/dashboard/stats
Response: {
  total_signals: number,
  total_events: number,
  kill_switch_active: boolean
}
```

### Trading Signals
```
GET /api/v1/fusion/signals
Query Params:
  - symbol: string
  - signal_type: buy|sell|hold
  - min_confidence: number (0-1)
  - time_horizon: intraday|swing|positional
  - page: number
  - limit: number

Response: {
  signals: TradingSignal[],
  total: number,
  page: number,
  limit: number
}
```

### Market Regime
```
GET /api/v1/strategy/market/overview
Response: {
  nifty50: IndexRegime,
  breadth: MarketBreadth,
  all_indices: IndexRegime[]
}

GET /api/v1/strategy/index/{index_key}/constituents
Response: {
  index_key: string,
  constituents: InstrumentRegime[]
}
```

### Events
```
GET /api/v1/intelligence/events
Query Params:
  - symbol: string
  - event_type: string
  - min_impact: number (0-100)
  - page: number
  - limit: number

Response: {
  events: ProcessedEvent[],
  total: number,
  page: number,
  limit: number
}
```

### ML Models
```
GET /api/v1/governance/models
Query Params:
  - state: shadow|paper|live|retired
  - model_type: string
  - page: number
  - limit: number

Response: {
  models: MLModel[],
  total: number
}

GET /api/v1/ml/drift/reports
Response: {
  reports: DriftReport[],
  total: number
}

PATCH /api/v1/governance/models/{model_id}/state
Body: {
  new_state: ModelState,
  reason: string
}
```

## WebSocket Integration

### Connection Management
- Hook: `useCAIWebSocket()`
- Auto-reconnect with exponential backoff
- Heartbeat/ping-pong for connection health
- Connection status indicator

### Real-time Channels
1. **Signals**: `/api/v1/fusion/ws/signals`
2. **Events**: `/api/v1/intelligence/ws/events`
3. **Models**: `/api/v1/governance/ws/models`
4. **Drift**: `/api/v1/ml/drift/ws`

### Message Format
```typescript
{
  type: "signal" | "event" | "model" | "drift",
  action: "create" | "update" | "delete",
  data: T
}
```

## Data Flow

### Initial Load
1. User navigates to `/cai-dashboard`
2. `CAIRealtimeDemo` component mounts
3. All hooks fetch initial data via REST API
4. WebSocket connections established
5. Components render with data

### Real-time Updates
1. Backend publishes event to Redis pub/sub
2. WebSocket server broadcasts to connected clients
3. React Query cache updated via `setQueryData`
4. Components re-render with new data

### Polling Fallback
- Signals: 30s polling interval
- Regime: 15min polling interval
- Events: 30s polling interval
- Models: No polling (WebSocket only)

## Authentication

All dashboard endpoints require JWT authentication:
```typescript
const { isAuthenticated } = useAuth();
// Queries are disabled when not authenticated
enabled: isAuthenticated
```

Headers:
```
Authorization: Bearer <jwt_token>
```

## Error Handling

### API Errors
- Network errors: Retry with exponential backoff
- 401 Unauthorized: Redirect to login
- 403 Forbidden: Show permission error
- 500 Server Error: Show error message + retry button

### WebSocket Errors
- Connection lost: Auto-reconnect (max 5 attempts)
- Message parse error: Log and skip
- Authentication error: Close connection + redirect

## Performance Optimizations

1. **React Query Caching**
   - Stale time: 30s for signals/events, 15min for regime
   - Cache time: 5min
   - Background refetch on window focus

2. **Request Deduplication**
   - React Query deduplicates identical requests
   - AbortController cancels in-flight requests

3. **Pagination**
   - Default limit: 10 items
   - Server-side pagination for large datasets

4. **WebSocket Throttling**
   - Max 1 message per second per channel
   - Batch updates for rapid changes

## Database Schema

### Key Tables
- `ai_trading_signals`: Trading signals
- `ai_event_classification`: Processed events
- `ai_ml_models`: Model registry
- `ml_drift_metrics`: Drift reports
- `ai_regime_detection`: Market regime data
- `upstox_ohlcv`: Price data

## God Nodes (from Graph Report)
1. `MLModelMetadata` - 221 edges (model registry core)
2. `AIEventClassification` - 207 edges (event processing)
3. `TradeSuggestion` - 164 edges (signal generation)
4. `EventCorrelation` - 156 edges (event correlation)
5. `UpstoxOHLCV` - 132 edges (market data)

## Key Communities
- **Community 1**: Dashboard & API endpoints
- **Community 3**: ML drift detection
- **Community 4**: WebSocket services
- **Community 12**: Trade suggestions & events

## Next Steps for Development

1. **Add New Panel**: Create component → Add hook → Register in `CAIRealtimeDemo`
2. **Add Filter**: Update type → Add to UI → Pass to API
3. **Add WebSocket Channel**: Backend pub/sub → Frontend hook → Component subscription
4. **Add Metric**: Backend calculation → API endpoint → Frontend display

## Testing

### Frontend Tests
```bash
cd frontend
npm test -- components/ai/
```

### Backend Tests
```bash
cd backend
pytest tests/api/v1/test_cai.py
pytest tests/api/v1/test_fusion.py
```

### E2E Tests
```bash
cd frontend
npm run test:e2e
```

## Monitoring

### Metrics
- Request latency (P50, P95, P99)
- Error rate by endpoint
- WebSocket connection count
- Cache hit rate

### Logs
- Structured JSON logging
- Request ID tracing
- Error stack traces

### Alerts
- High error rate (>5%)
- Slow response time (>2s P95)
- WebSocket disconnections (>10/min)
- Drift detection (score >0.7)
