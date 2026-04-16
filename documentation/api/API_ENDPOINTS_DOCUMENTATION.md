# API Endpoints Documentation - Cortex AI Trading Platform

**Generated:** 2026-04-15  
**Project:** Cortex_Merge_AI-ML  
**Architecture:** FastAPI Backend + Next.js Frontend (BFF Pattern)

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Backend API Endpoints (FastAPI)](#backend-api-endpoints-fastapi)
3. [Frontend API Routes (Next.js BFF)](#frontend-api-routes-nextjs-bff)
4. [WebSocket Endpoints](#websocket-endpoints)
5. [Authentication & Authorization](#authentication--authorization)
6. [Request/Response Patterns](#requestresponse-patterns)

---

## Architecture Overview

### Backend (FastAPI)
- **Base URL:** `http://localhost:8000` (dev) / `https://api.cortex.in` (prod)
- **API Prefix:** `/api/v1`
- **Documentation:** `/docs` (Swagger), `/redoc` (ReDoc)
- **Health Check:** `/health`, `/metrics` (Prometheus)

### Frontend (Next.js)
- **Base URL:** `http://localhost:3000` (dev)
- **BFF Proxy:** `/api/v1/*` → forwards to FastAPI backend
- **Auth Routes:** `/api/auth/*` (login, logout, refresh, me)
- **Health Check:** `/api/health`

### Communication Pattern
```
Frontend → Next.js API Routes (BFF) → FastAPI Backend
         ↓
    WebSocket (direct to backend for real-time streams)
```

---

## Backend API Endpoints (FastAPI)

### 1. Authentication (`/api/v1/auth`)

| Method | Endpoint | Description | Auth Required | Rate Limit |
|--------|----------|-------------|---------------|------------|
| POST | `/register` | Register new user | No | Default |
| POST | `/login` | Login with username/password | No | Default |
| POST | `/logout` | Logout and invalidate refresh token | Yes | Default |
| POST | `/refresh` | Refresh access token using refresh token cookie | No | Default |
| GET | `/me` | Get current user profile | Yes | Default |
| POST | `/create-admin` | Create admin user (restricted) | Yes (Admin) | Default |

**Request/Response Models:**
```typescript
// Register
Request: { username: string, email: string, password: string, full_name?: string }
Response: { id: number, username: string, email: string, role: string, ... }

// Login
Request: { username: string, password: string }
Response: { access_token: string, refresh_token: string, token_type: "bearer", expires_in: number }

// Me
Response: { id: number, username: string, email: string, role: string, is_active: boolean, ... }
```

---

### 2. Market Data (`/api/v1/market-data`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/live/{instrument_key}` | Get live price for instrument | Yes |
| GET | `/instruments/search` | Search instruments by query | Yes |
| GET | `/ohlcv/{instrument_key}` | Get OHLCV data for instrument | Yes |

**Query Parameters:**
- `/instruments/search`: `?q=<query>&limit=<number>`
- `/ohlcv/{instrument_key}`: `?from=<date>&to=<date>&interval=<1m|5m|1d>`

---

### 3. Scanner (`/api/v1/scanner`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/run` | Run market scan (legacy) | Yes |
| POST | `/run` | Run market scan with parameters | Yes |
| GET | `/latest` | Get latest scan results | Yes |
| GET | `/context` | Get scanner context (symbols, config) | Yes |

**Request/Response:**
```typescript
// POST /run
Request: { scan_type?: "momentum" | "breakout" | "reversal", symbols?: string[] }
Response: { 
  scan_id: string, 
  status: "running" | "completed" | "failed",
  results: StockAnalysis[],
  timestamp: string 
}

// GET /latest
Response: { 
  scan_id: string,
  results: { strong_buy: [], buy: [], neutral: [], sell: [], strong_sell: [] },
  metadata: { total_scanned: number, timestamp: string }
}
```

---

### 4. Upstox Integration (`/api/v1/upstox`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/stream/start` | Start real-time tick stream | Yes |
| POST | `/stream/stop` | Stop tick stream | Yes |
| GET | `/stream/status` | Get stream status | Yes |
| POST | `/ingest` | Ingest candles for symbols | Yes |
| GET | `/candles/intraday` | Get intraday candles | Yes |
| GET | `/candles/historical` | Get historical candles | Yes |
| WS | `/ws/ticks` | WebSocket for real-time ticks | Yes |

**Query Parameters:**
- `/candles/intraday`: `?instrument_key=<key>&from=<datetime>&to=<datetime>&interval=<1m|5m>`
- `/candles/historical`: `?instrument_key=<key>&from=<date>&to=<date>&interval=<1d|1w>`

---

### 5. HawkEye Scanner (`/api/v1/hawk-eye`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/scan` | Run HawkEye multi-timeframe scan | Yes |
| GET | `/analyze` | Analyze specific instrument | Yes |
| GET | `/fundamentals` | Get fundamental data | Yes |

**Response:**
```typescript
{
  results: Array<{
    symbol: string,
    signals: {
      "1m": { signal: "BUY" | "SELL" | "NEUTRAL", strength: number },
      "5m": { signal: string, strength: number },
      "15m": { signal: string, strength: number },
      // ... more timeframes
    }
  }>
}
```

---

### 6. ML Predictions (`/api/v1/ml`)

| Method | Endpoint | Description | Auth Required | Rate Limit |
|--------|----------|-------------|---------------|------------|
| POST | `/predict` | Generate ML prediction for symbol | Yes | 1000/min |
| GET | `/models` | List available ML models | Yes | Default |

**Request/Response:**
```typescript
// POST /predict
Request: { 
  symbol: string, 
  model_id?: number, 
  features?: Record<string, number>,
  prediction_type?: "price" | "direction" | "volatility"
}
Response: { 
  id: number,
  prediction: number,
  prediction_proba: { bullish: number, bearish: number },
  symbol: string,
  model_version: string,
  timestamp: string
}
```

---

### 7. ML Drift Detection (`/api/v1/ml/drift`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/metrics` | Get drift metrics | Yes |
| POST | `/detect` | Trigger drift detection | Yes |
| GET | `/summary` | Get drift summary | Yes |

**Query Parameters:**
- `/metrics`: `?model_id=<id>&days=<1-90>&drift_only=<bool>`

---

### 8. AI Intelligence (`/api/v1/intelligence`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/classify` | Classify event using NLP | Yes |
| POST | `/fake-news/detect` | Detect fake news | Yes |

**Request/Response:**
```typescript
// POST /classify
Request: { content: string, entities?: Record<string, any> }
Response: { 
  event_type: "earnings" | "merger" | "regulatory" | ...,
  confidence: number,
  entities: string[],
  sentiment: "positive" | "negative" | "neutral"
}

// POST /fake-news/detect
Request: { classification_id: number, content: string, source: string }
Response: { 
  is_fake: boolean, 
  confidence: number, 
  reasoning: string,
  credibility_score: number 
}
```

---

### 9. AI Governance (`/api/v1/governance`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/models` | List ML models with states | Yes |
| POST | `/models/{model_name}/promote` | Promote model state | Yes (Admin) |
| POST | `/drift/check/{model_id}` | Trigger drift check | Yes |
| GET | `/drift/reports` | Get drift reports | Yes |

**Model States:** `candidate` → `staging` → `production` → `deprecated`

**Request/Response:**
```typescript
// POST /models/{model_name}/promote
Request: { target_state: "staging" | "production" | "deprecated" }
Response: { model_id: number, old_state: string, new_state: string, timestamp: string }

// GET /drift/reports
Response: Array<{
  id: number,
  model_name: string,
  drift_detected: boolean,
  drift_score: number,
  action_taken: "demoted" | "alerted" | "none",
  timestamp: string
}>
```

---

### 10. AI Strategy (`/api/v1/strategy`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/regime/latest` | Get latest market regime | Yes |
| GET | `/regime/history` | Get regime history | Yes |

**Response:**
```typescript
// GET /regime/latest
{
  regime_type: "trending_bullish" | "trending_bearish" | "ranging" | "volatile",
  confidence: number,
  indicators: {
    adx: number,
    atr: number,
    rsi: number,
    bollinger_width: number
  },
  timestamp: string
}
```

---

### 11. AI Fusion (`/api/v1/fusion`)

| Method | Endpoint | Description | Auth Required | Rate Limit |
|--------|----------|-------------|---------------|------------|
| POST | `/signals/generate/{symbol}` | Generate trading signal | Yes | 10000/min |
| GET | `/signals` | Get all signals | Yes | Default |
| GET | `/signals/{symbol}` | Get signals for symbol | Yes | Default |

**Response:**
```typescript
{
  signal_id: number,
  symbol: string,
  action: "BUY" | "SELL" | "HOLD",
  confidence: number,
  time_horizon: "short" | "medium" | "long",
  contributing_factors: {
    technical: { rsi: number, macd: string, ... },
    ml_prediction: { direction: string, confidence: number },
    events: Array<{ type: string, impact: number }>
  },
  timestamp: string
}
```

---

### 12. AI Safety (`/api/v1/safety`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/kill-switch/activate` | Activate kill switch | Yes (Admin) |
| POST | `/kill-switch/deactivate` | Deactivate kill switch | Yes (Admin) |
| GET | `/kill-switch/status` | Get kill switch status | Yes |
| GET | `/kill-switch/monitor` | Get monitoring data | Yes |

**Request/Response:**
```typescript
// POST /kill-switch/activate
Request: { 
  switch_type: "global" | "strategy" | "symbol",
  reason: string,
  duration_minutes?: number,
  affected_entities?: string[]
}
Response: { 
  kill_switch_id: number, 
  status: "active", 
  activated_at: string,
  expires_at?: string 
}

// GET /kill-switch/status
Response: {
  active_switches: Array<{
    id: number,
    type: string,
    reason: string,
    activated_at: string,
    expires_at?: string
  }>
}
```

---

### 13. AI Ingestion (`/api/v1/ingestion`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/events/raw` | Get raw events | Yes |
| GET | `/events/processed` | Get processed events | Yes |

---

### 14. Cortex AI Dashboard (`/api/v1/cai`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/dashboard/stats` | Get dashboard statistics | Yes |
| GET | `/dashboard/recent-activity` | Get recent activity | Yes |

**Response:**
```typescript
// GET /dashboard/stats
{
  total_signals: number,
  total_events: number,
  kill_switch_active: boolean,
  active_models: number,
  drift_alerts: number
}
```

---

### 15. Health & Monitoring (`/api/v1`)

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/ml/health` | ML system health check | No |
| GET | `/ml/readiness` | ML readiness probe | No |
| GET | `/ml/liveness` | ML liveness probe | No |
| GET | `/health` | Basic health check | No |
| GET | `/metrics` | Prometheus metrics | No |

**Response:**
```typescript
// GET /ml/health
{
  status: "healthy" | "degraded" | "unhealthy",
  components: {
    database: { status: string, latency_ms: number },
    redis: { status: string, latency_ms: number },
    models: { status: string, loaded_models: number }
  },
  timestamp: string
}
```

---

## Frontend API Routes (Next.js BFF)

### 1. Universal Proxy (`/api/v1/[...path]`)

**Purpose:** Forwards all `/api/v1/*` requests to FastAPI backend with authentication

**Supported Methods:** GET, POST, PUT, PATCH, DELETE, OPTIONS

**Features:**
- Automatic token injection from Authorization header
- Request/response logging
- Error handling with proper status codes
- JSON body forwarding for POST/PUT/PATCH

**Usage:**
```typescript
// Frontend code
const response = await fetch('/api/v1/market-data/live/NSE_EQ|INE002A01018', {
  headers: { 'Authorization': `Bearer ${accessToken}` }
});
```

---

### 2. Authentication Routes

#### POST `/api/auth/refresh`
- **Purpose:** Refresh access token using refresh token cookie
- **Request:** No body (uses httpOnly cookie)
- **Response:** `{ access_token: string, token_type: string, expires_in: number }`
- **Features:** 
  - Forwards refresh token cookie to backend
  - Returns new access token
  - Updates refresh token cookie if rotated

#### POST `/api/auth/dev-login`
- **Purpose:** Development-only login endpoint
- **Request:** `{ username: string, password: string }`
- **Response:** Same as `/api/v1/auth/login`
- **Note:** Only available in development mode

#### POST `/api/auth/logout`
- **Purpose:** Logout and clear tokens
- **Request:** No body
- **Response:** `{ success: boolean }`
- **Features:** Clears refresh token cookie

#### GET `/api/auth/me`
- **Purpose:** Get current user profile
- **Request:** Requires Authorization header
- **Response:** User profile object

---

### 3. Health Check (`/api/health`)

**Purpose:** Check health of both Next.js frontend and FastAPI backend

**Response:**
```typescript
{
  status: "healthy" | "degraded" | "error",
  frontend: "healthy" | "degraded",
  backend: "healthy" | "unhealthy" | "unreachable",
  backendVersion?: string,
  responseTime: number,
  timestamp: string
}
```

**Status Codes:**
- `200`: All systems healthy
- `503`: Backend unavailable (frontend still responding)
- `500`: Internal error

**Features:**
- 3-second timeout for backend check
- No caching (always fresh)
- Used by load balancers and monitoring services

---

## WebSocket Endpoints

### 1. Upstox Ticks (`/api/v1/upstox/ws/ticks`)

**Purpose:** Real-time market data ticks from Upstox

**Connection:**
```typescript
const ws = new WebSocket('ws://localhost:8000/api/v1/upstox/ws/ticks?symbols=NSE_EQ|INE002A01018');
```

**Message Format:**
```typescript
{
  instrument_key: string,
  ltp: number,
  volume: number,
  timestamp: string,
  bid: number,
  ask: number,
  change_percent: number
}
```

---

### 2. CAI Stream (`/api/v1/cai/stream`) [Cortex v4]

**Purpose:** Real-time Cortex AI updates (signals, regime changes, events, drift alerts)

**Connection:**
```typescript
const ws = new WebSocket('ws://localhost:8000/api/v1/cai/stream');
```

**Message Types:**
```typescript
// Signal Update
{ type: "signal", data: { symbol: string, action: string, confidence: number, ... } }

// Regime Change
{ type: "regime", data: { regime_type: string, confidence: number, ... } }

// High Impact Event
{ type: "event", data: { event_type: string, content: string, impact: number, ... } }

// Drift Alert
{ type: "drift", data: { model_name: string, drift_score: number, action_taken: string, ... } }
```

**Features:**
- Redis pub/sub backed
- Automatic reconnection
- Connection pooling (shared Redis listener)
- Graceful shutdown when no clients connected

---

## Authentication & Authorization

### Token-Based Authentication (JWT)

**Flow:**
1. User logs in → receives `access_token` (short-lived, 15 min) and `refresh_token` (long-lived, 7 days)
2. Access token stored in memory, refresh token in httpOnly cookie
3. Access token sent in `Authorization: Bearer <token>` header
4. On 401 error, frontend automatically refreshes token using `/api/auth/refresh`
5. New access token used for subsequent requests

**Token Rotation:**
- Refresh tokens are rotated on each refresh (one-time use)
- Old refresh token invalidated after use
- Token family tracking prevents reuse attacks

### Role-Based Access Control (RBAC)

**Roles:**
- `viewer`: Read-only access to data
- `trader`: Can generate signals, run scans, make predictions
- `admin`: Full access including model promotion, kill switch activation

**Permission Checks:**
- Enforced at endpoint level using `@require_role()` decorator
- Enforced at permission level using `@require_permission()` decorator

**Example:**
```python
@router.post("/signals/generate/{symbol}")
async def generate_signal(
    current_user = Depends(require_role("trader"))
):
    ...
```

---

## Request/Response Patterns

### Standard Response Format

**Success:**
```json
{
  "data": { ... },
  "timestamp": "2026-04-15T20:45:30Z"
}
```

**Error:**
```json
{
  "error": "Error message",
  "detail": "Detailed error description",
  "code": "ERROR_CODE",
  "timestamp": "2026-04-15T20:45:30Z"
}
```

### Pagination

**Query Parameters:**
- `limit`: Number of items per page (default: 20, max: 100)
- `offset`: Number of items to skip (default: 0)
- `sort_by`: Field to sort by
- `order`: `asc` or `desc`

**Response:**
```json
{
  "items": [...],
  "total": 150,
  "limit": 20,
  "offset": 0,
  "has_more": true
}
```

### Rate Limiting

**Headers:**
- `X-RateLimit-Limit`: Total requests allowed per window
- `X-RateLimit-Remaining`: Requests remaining in current window
- `X-RateLimit-Reset`: Unix timestamp when window resets

**Response on Limit Exceeded (429):**
```json
{
  "error": "Rate limit exceeded",
  "retry_after": 60
}
```

### Request Tracking

**Headers:**
- `X-Request-ID`: Unique request identifier (auto-generated or client-provided)
- `X-Correlation-ID`: Correlation ID for distributed tracing

### CORS

**Allowed Origins:**
- Development: `http://localhost:3000`, `http://localhost:8000`
- Production: `https://cortex.in`, `https://*.cortex.in`

**Allowed Methods:** GET, POST, PUT, PATCH, DELETE, OPTIONS

**Allowed Headers:** Authorization, Content-Type, X-Request-ID

---

## API Client Usage Examples

### Frontend (TypeScript)

```typescript
import { api, setAccessToken } from '@/lib/api-client';

// Set token after login
setAccessToken(accessToken);

// Make authenticated request
const response = await api.get('/market-data/live/NSE_EQ|INE002A01018');

// Handle 401 automatically (token refresh happens in interceptor)
const signals = await api.get('/fusion/signals', {
  params: { symbol: 'RELIANCE', limit: 10 }
});

// POST request
const prediction = await api.post('/ml/predict', {
  symbol: 'RELIANCE',
  prediction_type: 'price'
});
```

### Backend (Python)

```python
from app.core.auth import get_current_user, require_role
from fastapi import Depends

@router.get("/protected")
async def protected_endpoint(
    current_user = Depends(get_current_user)
):
    # current_user contains: id, username, email, role
    return {"user_id": current_user.id}

@router.post("/admin-only")
async def admin_endpoint(
    current_user = Depends(require_role("admin"))
):
    # Only accessible by admin role
    return {"message": "Admin access granted"}
```

---

## Environment Variables

### Backend (FastAPI)

```bash
# Database
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/cortex

# Redis
REDIS_URL=redis://localhost:6379/0

# JWT
SECRET_KEY=your-secret-key-here
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# CORS
CORS_ORIGINS=http://localhost:3000,http://localhost:8000

# Upstox
UPSTOX_API_KEY=your-api-key
UPSTOX_API_SECRET=your-api-secret
UPSTOX_ACCESS_TOKEN=your-access-token

# ML
ML_ENCRYPTION_KEY=your-encryption-key
MODEL_REGISTRY_PATH=/app/models
```

### Frontend (Next.js)

```bash
# Backend URL
BACKEND_URL=http://localhost:8000
NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1

# WebSocket URL (auto-derived from API_URL)
NEXT_PUBLIC_WS_URL=ws://localhost:8000/api/v1
```

---

## Summary

**Total Backend Endpoints:** 50+  
**Total Frontend Routes:** 6  
**WebSocket Endpoints:** 2  
**Authentication:** JWT with token rotation  
**Authorization:** RBAC (viewer, trader, admin)  
**Rate Limiting:** Per-endpoint configuration  
**Real-time:** WebSocket + Redis pub/sub  
**Monitoring:** Health checks, Prometheus metrics, request tracking

**Key Features:**
- BFF pattern for secure token management
- Automatic token refresh on 401
- Real-time updates via WebSocket
- Comprehensive error handling
- Request/response logging
- Rate limiting and CORS protection
- Role-based access control
- Distributed tracing support

---

**Last Updated:** 2026-04-15  
**Maintained By:** Cortex AI Team
