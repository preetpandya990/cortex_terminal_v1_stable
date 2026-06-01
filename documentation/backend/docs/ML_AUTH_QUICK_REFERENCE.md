# ML API Authentication - Quick Reference

## For API Consumers

### Making Authenticated Requests

```bash
# 1. Get access token
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "user@example.com", "password": "password"}'

# Response: {"access_token": "eyJ...", "token_type": "bearer"}

# 2. Use token in ML API requests
curl -X POST http://localhost:8000/api/v1/ml/predict \
  -H "Authorization: Bearer eyJ..." \
  -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "timeframe": "1d"}'
```

### Python Client Example

```python
import requests

class MLClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.access_token = self._login(username, password)
    
    def _login(self, username, password):
        response = requests.post(
            f"{self.base_url}/api/v1/auth/login",
            json={"username": username, "password": password}
        )
        response.raise_for_status()
        return response.json()["access_token"]
    
    def predict(self, symbol, timeframe="1d"):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        response = requests.post(
            f"{self.base_url}/api/v1/ml/predict",
            headers=headers,
            json={"symbol": symbol, "timeframe": timeframe}
        )
        response.raise_for_status()
        return response.json()

# Usage
client = MLClient("http://localhost:8000", "user@example.com", "password")
prediction = client.predict("AAPL")
print(prediction)
```

## For Backend Developers

### Adding Authentication to New Endpoints

```python
from fastapi import APIRouter, Depends
from app.core.security import get_current_user_id

# Option 1: Router-level authentication (all endpoints)
router = APIRouter(
    prefix="/api/v1/myservice",
    dependencies=[Depends(get_current_user_id)]
)

@router.get("/data")
async def get_data():
    # All requests automatically authenticated
    pass

# Option 2: Endpoint-level authentication
@router.get("/data")
async def get_data(user_id: str = Depends(get_current_user_id)):
    # user_id contains authenticated user's ID
    print(f"Request from user: {user_id}")
    pass
```

### Tracking User Activity

```python
from app.api.v1.ml_auth_utils import log_auth_success, get_user_prediction_count

@router.post("/predict")
async def predict(
    request: Request,
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    # Make prediction
    prediction = await make_prediction()
    
    # Save with user_id
    db_prediction = MLPrediction(
        user_id=user_id,
        symbol="AAPL",
        prediction=1.5
    )
    db.add(db_prediction)
    await db.commit()
    
    # Log success
    await log_auth_success(db, request, user_id, db_prediction.id)
    
    return prediction
```

### Implementing Rate Limiting

```python
from app.api.v1.ml_auth_utils import check_user_rate_limit
from fastapi import HTTPException

@router.post("/predict")
async def predict(
    user_id: str = Depends(get_current_user_id),
    db: AsyncSession = Depends(get_db)
):
    # Check rate limit
    is_allowed, count = await check_user_rate_limit(
        db=db,
        user_id=user_id,
        limit=100,
        time_window_minutes=60
    )
    
    if not is_allowed:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {count}/100 requests per hour"
        )
    
    # Process request
    return await make_prediction()
```

## Error Handling

### Common Error Responses

| Status Code | Error | Cause | Solution |
|-------------|-------|-------|----------|
| 401 | No authentication token provided | Missing Authorization header | Add `Authorization: Bearer <token>` header |
| 401 | Token has expired | Access token expired (>30 min) | Get new token via refresh or re-login |
| 401 | Invalid token | Malformed or invalid JWT | Check token format and validity |
| 401 | Token has been revoked | Token was manually revoked | Re-authenticate to get new token |
| 429 | Rate limit exceeded | Too many requests | Wait before making more requests |

### Handling Errors in Client Code

```python
import requests
from requests.exceptions import HTTPError

def make_prediction(client, symbol):
    try:
        return client.predict(symbol)
    except HTTPError as e:
        if e.response.status_code == 401:
            # Token expired, re-authenticate
            client._login(username, password)
            return client.predict(symbol)
        elif e.response.status_code == 429:
            # Rate limited, wait and retry
            time.sleep(60)
            return client.predict(symbol)
        else:
            raise
```

## Monitoring & Debugging

### Check Authentication Logs

```sql
-- Recent authentication failures
SELECT 
    timestamp,
    user_id,
    endpoint,
    ip_address,
    error_message
FROM ml_audit_logs
WHERE event_type = 'auth_failure'
ORDER BY timestamp DESC
LIMIT 20;
```

### Check User Activity

```sql
-- User prediction count in last hour
SELECT COUNT(*) as prediction_count
FROM ml_predictions
WHERE user_id = 'user_123'
  AND timestamp > NOW() - INTERVAL '1 hour';
```

### Debug Authentication Issues

```python
# Test token validity
from app.core.security import decode_token

try:
    payload = decode_token(access_token)
    print(f"Token valid for user: {payload.sub}")
    print(f"Expires at: {payload.exp}")
except Exception as e:
    print(f"Token invalid: {e}")
```

## Security Best Practices

### For API Consumers

1. **Store tokens securely**: Never commit tokens to version control
2. **Use HTTPS**: Always use HTTPS in production
3. **Handle token expiry**: Implement automatic token refresh
4. **Rotate credentials**: Change passwords regularly
5. **Limit token scope**: Use separate tokens for different services

### For Backend Developers

1. **Never log tokens**: Don't log JWT tokens in application logs
2. **Use short expiry**: Keep access token expiry short (15-30 min)
3. **Validate on every request**: Don't cache authentication results
4. **Rate limit aggressively**: Prevent abuse with rate limiting
5. **Monitor auth failures**: Alert on suspicious authentication patterns

## Configuration

### Environment Variables

```bash
# Required
SECRET_KEY=your-secret-key-min-32-chars
ALGORITHM=HS256

# Optional (with defaults)
ACCESS_TOKEN_EXPIRE_MINUTES=30
REFRESH_TOKEN_EXPIRE_DAYS=7
ML_RATE_LIMIT_PER_HOUR=100
```

### Generating Secret Key

```bash
# Generate secure secret key
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## Testing

### Manual Testing with curl

```bash
# 1. Login
TOKEN=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "test@example.com", "password": "password"}' \
  | jq -r '.access_token')

# 2. Test authenticated endpoint
curl -X POST http://localhost:8000/api/v1/ml/predict \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "timeframe": "1d"}'

# 3. Test without token (should return 401)
curl -X POST http://localhost:8000/api/v1/ml/predict \
  -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "timeframe": "1d"}'
```

### Automated Testing

```bash
# Run authentication tests
pytest backend/tests/unit/test_ml_auth.py -v

# Run integration tests
pytest backend/tests/integration/test_ml_auth_integration.py -v
```

## Troubleshooting

### Issue: "No authentication token provided"

**Cause**: Missing Authorization header

**Solution**:
```python
headers = {"Authorization": f"Bearer {access_token}"}
response = requests.post(url, headers=headers, json=data)
```

### Issue: "Token has expired"

**Cause**: Access token older than 30 minutes

**Solution**: Get new token via refresh endpoint or re-login

### Issue: Rate limit exceeded

**Cause**: Too many requests in time window

**Solution**: 
- Wait before making more requests
- Implement exponential backoff
- Request higher rate limit if needed

### Issue: Authentication works locally but fails in production

**Checklist**:
- [ ] SECRET_KEY is set in production environment
- [ ] HTTPS is enabled
- [ ] Redis is accessible for token revocation
- [ ] Database connection is working
- [ ] Firewall allows traffic on required ports

## Support

For issues or questions:
1. Check logs: `tail -f logs/app.log`
2. Check audit logs: Query `ml_audit_logs` table
3. Verify configuration: Check environment variables
4. Test token: Use `decode_token()` utility
5. Contact: backend-team@example.com
