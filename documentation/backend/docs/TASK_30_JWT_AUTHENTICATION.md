# Task 30: JWT Authentication for ML Endpoints

## Implementation Summary

This document describes the implementation of JWT authentication for ML prediction endpoints.

### Requirements Implemented

- **18.1**: JWT token validation on every ML API request
- **18.5**: Authentication failure logging to audit logs
- **8.3**: Per-user prediction tracking for rate limiting

## Changes Made

### 1. Database Schema Updates

#### Migration: `0003_add_user_tracking.py`

Added user tracking to ML system:

- **ml_predictions table**: Added `user_id` column (indexed)
- **ml_audit_logs table**: New table for authentication and security events
  - Tracks authentication failures
  - Tracks successful prediction requests
  - Stores IP address, user agent, request data
  - Links to prediction records

### 2. Model Updates

#### `backend/app/models/ml_data.py`

**MLPrediction Model**:
- Added `user_id` field to associate predictions with users
- Indexed for efficient per-user queries

**MLAuditLog Model** (New):
- `event_type`: Type of event ('auth_failure', 'prediction_request', etc.)
- `user_id`: User who triggered the event
- `endpoint`: API endpoint accessed
- `ip_address`: Client IP address
- `user_agent`: Client user agent string
- `request_data`: JSON request parameters
- `response_status`: HTTP response status code
- `error_message`: Error details for failures
- `prediction_id`: Link to related prediction

### 3. Authentication Utilities

#### `backend/app/api/v1/ml_auth_utils.py` (New)

Helper functions for authentication and audit logging:

**`log_auth_failure()`**:
- Logs authentication failures to audit logs
- Captures IP address, user agent, endpoint
- Records error message and 401 status
- Requirement: 18.5

**`log_auth_success()`**:
- Logs successful prediction requests
- Associates with prediction ID
- Tracks user activity
- Requirement: 18.5, 8.3

**`get_user_prediction_count()`**:
- Counts predictions by user in time window
- Used for per-user rate limiting
- Requirement: 8.3

**`check_user_rate_limit()`**:
- Checks if user exceeded rate limit
- Returns (is_allowed, current_count)
- Configurable limit and time window
- Requirement: 8.3

### 4. ML Endpoints Updates

#### `backend/app/api/v1/ml_predictions.py`

**Router-Level Authentication**:
```python
router = APIRouter(
    prefix="/ml",
    tags=["ML Predictions"],
    dependencies=[Depends(get_current_user_id)]  # All endpoints require auth
)
```

**Endpoint Changes**:

All ML endpoints now:
1. Use `user_id: str = Depends(get_current_user_id)` parameter
2. Validate JWT token on every request (automatic via dependency)
3. Return HTTP 401 for invalid/expired tokens (automatic)
4. Associate predictions with `user_id` in database
5. Support audit logging (infrastructure in place)

**Updated Endpoints**:
- `POST /ml/predict` - Single prediction
- `POST /ml/predict/batch` - Batch predictions
- `GET /ml/predictions/{symbol}` - Get cached prediction
- `POST /ml/predict/ensemble` - Ensemble prediction

## Authentication Flow

### 1. Request with Valid Token

```
Client Request
  ↓
  JWT Token in Authorization Header
  ↓
  get_current_user_id() validates token
  ↓
  Returns user_id
  ↓
  Endpoint executes with user_id
  ↓
  Prediction saved with user_id
  ↓
  Audit log created (optional)
  ↓
  Response returned
```

### 2. Request with Invalid Token

```
Client Request
  ↓
  Invalid/Expired JWT Token
  ↓
  get_current_user_id() raises InvalidTokenError
  ↓
  FastAPI returns HTTP 401
  ↓
  log_auth_failure() called (if configured)
  ↓
  Audit log created
```

## Usage Examples

### Making Authenticated Requests

```python
import requests

# Get access token from auth endpoint
response = requests.post(
    "http://localhost:8000/api/v1/auth/login",
    json={"username": "user@example.com", "password": "password"}
)
access_token = response.json()["access_token"]

# Make prediction request with token
headers = {"Authorization": f"Bearer {access_token}"}
response = requests.post(
    "http://localhost:8000/api/v1/ml/predict",
    headers=headers,
    json={
        "symbol": "AAPL",
        "timeframe": "1d"
    }
)

prediction = response.json()
```

### Checking User Prediction Count

```python
from app.api.v1.ml_auth_utils import get_user_prediction_count

# Get predictions in last hour
count = await get_user_prediction_count(
    db=db,
    user_id="user_123",
    time_window_minutes=60
)

print(f"User made {count} predictions in the last hour")
```

### Checking Rate Limit

```python
from app.api.v1.ml_auth_utils import check_user_rate_limit

# Check if user can make more predictions
is_allowed, count = await check_user_rate_limit(
    db=db,
    user_id="user_123",
    limit=100,
    time_window_minutes=60
)

if not is_allowed:
    raise HTTPException(
        status_code=429,
        detail=f"Rate limit exceeded: {count}/100 predictions in last hour"
    )
```

## Security Features

### 1. JWT Token Validation

- **Short-lived access tokens**: 15-30 minutes (configurable)
- **Token verification**: Signature, expiration, type checked
- **Revocation support**: Tokens can be revoked via Redis
- **No token leakage**: Generic error messages on failure

### 2. Audit Logging

- **Authentication failures**: All failed auth attempts logged
- **Successful requests**: Prediction requests tracked
- **IP tracking**: Client IP addresses recorded
- **User agent tracking**: Browser/client information captured
- **Request data**: Query parameters and metadata stored

### 3. Rate Limiting

- **Per-user tracking**: Predictions counted per user
- **Time windows**: Configurable time periods (default: 60 minutes)
- **Flexible limits**: Different limits for different endpoints
- **Database-backed**: Persistent across server restarts

## Testing

### Unit Tests

Located in `backend/tests/unit/test_ml_auth.py`:

- JWT token validation tests
- Authentication failure tests
- Audit logging tests
- User prediction tracking tests
- Rate limit checking tests
- Endpoint authentication tests

### Running Tests

```bash
# Run all ML auth tests
pytest backend/tests/unit/test_ml_auth.py -v

# Run specific test
pytest backend/tests/unit/test_ml_auth.py::TestJWTAuthentication::test_valid_token_returns_user_id -v
```

## Database Migration

### Running the Migration

```bash
# Apply migration
alembic upgrade head

# Verify tables created
psql -d cortex_db -c "\d ml_predictions"
psql -d cortex_db -c "\d ml_audit_logs"
```

### Rollback

```bash
# Rollback to previous version
alembic downgrade -1
```

## Configuration

### Environment Variables

```bash
# JWT Settings (in .env)
SECRET_KEY=your-secret-key-here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7

# Rate Limiting
ML_RATE_LIMIT_PER_HOUR=100
ML_RATE_LIMIT_WINDOW_MINUTES=60
```

## Error Responses

### 401 Unauthorized

```json
{
  "detail": "No authentication token provided"
}
```

```json
{
  "detail": "Token has expired"
}
```

```json
{
  "detail": "Invalid token"
}
```

### 429 Too Many Requests

```json
{
  "detail": "Rate limit exceeded: 150/100 predictions in last hour"
}
```

## Monitoring

### Audit Log Queries

```sql
-- Failed authentication attempts in last hour
SELECT * FROM ml_audit_logs
WHERE event_type = 'auth_failure'
  AND timestamp > NOW() - INTERVAL '1 hour'
ORDER BY timestamp DESC;

-- Top users by prediction count
SELECT user_id, COUNT(*) as prediction_count
FROM ml_predictions
WHERE timestamp > NOW() - INTERVAL '24 hours'
GROUP BY user_id
ORDER BY prediction_count DESC
LIMIT 10;

-- Authentication failures by IP
SELECT ip_address, COUNT(*) as failure_count
FROM ml_audit_logs
WHERE event_type = 'auth_failure'
  AND timestamp > NOW() - INTERVAL '1 hour'
GROUP BY ip_address
ORDER BY failure_count DESC;
```

## Future Enhancements

1. **API Key Support**: Alternative authentication method
2. **OAuth2 Integration**: Third-party authentication
3. **Role-Based Access Control**: Different permissions for different users
4. **Advanced Rate Limiting**: Different limits based on user tier
5. **Real-time Monitoring**: Dashboard for authentication metrics
6. **Automated Alerts**: Notify on suspicious authentication patterns

## References

- JWT Specification: https://jwt.io/
- FastAPI Security: https://fastapi.tiangolo.com/tutorial/security/
- SQLAlchemy Async: https://docs.sqlalchemy.org/en/14/orm/extensions/asyncio.html
