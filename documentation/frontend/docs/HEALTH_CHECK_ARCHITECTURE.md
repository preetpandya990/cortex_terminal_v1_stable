# Health Check Architecture

## Overview

Production-ready health monitoring system for the Cortex AI trading platform. Implements industry-standard patterns for monitoring application availability and backend connectivity.

## Architecture

```
┌─────────────────┐
│  Load Balancer  │
│   / K8s / ECS   │
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────────┐
│         Next.js Frontend                │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │  HealthCheckWrapper (Client)     │  │
│  │  - Exponential backoff           │  │
│  │  - Circuit breaker pattern       │  │
│  │  - User notifications            │  │
│  └──────────┬───────────────────────┘  │
│             │                           │
│             ▼                           │
│  ┌──────────────────────────────────┐  │
│  │  /api/health (Next.js Route)     │  │
│  │  - Timeout handling (3s)         │  │
│  │  - Response time tracking        │  │
│  │  - Status aggregation            │  │
│  └──────────┬───────────────────────┘  │
└─────────────┼───────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────┐
│         FastAPI Backend                 │
│                                         │
│  ┌──────────────────────────────────┐  │
│  │  /health Endpoint                │  │
│  │  - Database connectivity         │  │
│  │  - Service status                │  │
│  │  - Version info                  │  │
│  └──────────────────────────────────┘  │
└─────────────────────────────────────────┘
```

## Components

### 1. Backend Health Endpoint (`/health`)

**Location:** `backend/app/main.py`

**Response:**
```json
{
  "status": "ok",
  "version": "1.0.0"
}
```

**Purpose:**
- Verify FastAPI server is running
- Check database connectivity (future enhancement)
- Report service version

### 2. Next.js Health API Route (`/api/health`)

**Location:** `frontend/src/app/api/health/route.ts`

**Features:**
- Proxies health check to backend
- 3-second timeout protection
- Response time tracking
- Detailed status reporting

**Response Formats:**

Healthy:
```json
{
  "status": "healthy",
  "frontend": "healthy",
  "backend": "healthy",
  "backendVersion": "1.0.0",
  "responseTime": 16,
  "timestamp": "2026-04-07T17:38:35.423Z"
}
```

Degraded (backend unreachable):
```json
{
  "status": "degraded",
  "frontend": "healthy",
  "backend": "unreachable",
  "error": "Backend connection failed",
  "responseTime": 3001,
  "timestamp": "2026-04-07T17:38:35.423Z"
}
```

### 3. HealthCheckWrapper Component

**Location:** `frontend/src/components/HealthCheckWrapper.tsx`

**Features:**

#### Exponential Backoff
- Initial retry: 5 seconds
- Max retry delay: 60 seconds
- Formula: `min(5000 * 2^failures, 60000)`

#### Circuit Breaker Pattern
- Opens after 3 consecutive failures
- Prevents cascade failures
- Requires manual retry when open

#### User Experience
- Non-intrusive banner notification
- Manual retry button
- Dismissible alerts
- Auto-recovery detection

#### Performance
- Minimal re-renders
- Proper cleanup on unmount
- Request cancellation support

## Configuration

### Environment Variables

```bash
# Backend URL for health checks
BACKEND_URL=http://localhost:8000
```

### Timeouts

```typescript
// Client-side timeout (HealthCheckWrapper)
const HEALTH_CHECK_TIMEOUT = 5000; // 5 seconds

// Server-side timeout (API route)
const HEALTH_CHECK_TIMEOUT = 3000; // 3 seconds
```

### Retry Configuration

```typescript
const INITIAL_RETRY_DELAY = 5000;      // 5 seconds
const MAX_RETRY_DELAY = 60000;         // 60 seconds
const MAX_CONSECUTIVE_FAILURES = 3;    // Circuit breaker threshold
const SUCCESS_RESET_DELAY = 30000;     // 30 seconds
```

## Usage

### For Developers

The health check system runs automatically. No manual integration required.

### For DevOps

#### Kubernetes Liveness Probe
```yaml
livenessProbe:
  httpGet:
    path: /api/health
    port: 3000
  initialDelaySeconds: 30
  periodSeconds: 10
  timeoutSeconds: 5
  failureThreshold: 3
```

#### Kubernetes Readiness Probe
```yaml
readinessProbe:
  httpGet:
    path: /api/health
    port: 3000
  initialDelaySeconds: 10
  periodSeconds: 5
  timeoutSeconds: 3
  failureThreshold: 2
```

#### Docker Healthcheck
```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -f http://localhost:3000/api/health || exit 1
```

### For Monitoring Services

#### Datadog
```yaml
init_config:

instances:
  - url: https://your-app.com/api/health
    name: cortex-frontend
    timeout: 5
    http_response_status_code: 200
```

#### New Relic Synthetics
```javascript
$http.get('https://your-app.com/api/health', function(err, response, body) {
  assert.equal(response.statusCode, 200, 'Expected 200 OK');
  const data = JSON.parse(body);
  assert.equal(data.status, 'healthy', 'Expected healthy status');
});
```

## Best Practices

### 1. Never Cache Health Checks
```typescript
cache: 'no-store'  // Always fresh data
```

### 2. Use Appropriate Timeouts
- Client timeout > Server timeout
- Prevents hanging requests

### 3. Implement Circuit Breaker
- Prevents cascade failures
- Reduces unnecessary load

### 4. Track Response Time
- Identify performance degradation
- Set up alerts for slow responses

### 5. Graceful Degradation
- App remains functional during backend outages
- User can continue viewing cached data

## Monitoring & Alerts

### Recommended Alerts

1. **Backend Unreachable**
   - Trigger: 3 consecutive failures
   - Severity: Critical
   - Action: Page on-call engineer

2. **Slow Response Time**
   - Trigger: Response time > 1000ms
   - Severity: Warning
   - Action: Investigate performance

3. **Circuit Breaker Open**
   - Trigger: Circuit breaker opens
   - Severity: Critical
   - Action: Immediate investigation

### Metrics to Track

- Health check success rate
- Average response time
- P95/P99 response time
- Circuit breaker state changes
- Backend availability percentage

## Troubleshooting

### Health Check Fails Locally

1. Verify backend is running:
   ```bash
   curl http://localhost:8000/health
   ```

2. Check environment variables:
   ```bash
   echo $BACKEND_URL
   ```

3. Review logs:
   ```bash
   # Frontend logs
   npm run dev

   # Backend logs
   uvicorn app.main:app --reload
   ```

### Health Check Fails in Production

1. Check network connectivity
2. Verify firewall rules
3. Review load balancer configuration
4. Check backend logs for errors
5. Verify DNS resolution

### Circuit Breaker Stuck Open

1. Check backend health directly
2. Review backend logs for errors
3. Verify database connectivity
4. Check for resource exhaustion
5. Manual retry from UI

## Security Considerations

### 1. No Sensitive Data
Health endpoints should never expose:
- Database credentials
- API keys
- Internal IP addresses
- Detailed error messages

### 2. Rate Limiting
Consider rate limiting health checks to prevent abuse:
```typescript
// Example: Max 10 requests per minute per IP
```

### 3. Authentication (Optional)
For internal health checks, consider adding authentication:
```typescript
if (req.headers['x-api-key'] !== process.env.HEALTHCHECK_KEY) {
  return NextResponse.json({ error: 'Unauthorized' }, { status: 401 });
}
```

## Future Enhancements

1. **Database Health Check**
   - Add database connectivity test
   - Report connection pool status

2. **Dependency Health**
   - Check external API availability
   - Report third-party service status

3. **Detailed Metrics**
   - Memory usage
   - CPU utilization
   - Active connections

4. **Historical Data**
   - Store health check history
   - Generate uptime reports

## References

- [AWS Health Check Best Practices](https://aws.amazon.com/builders-library/implementing-health-checks/)
- [Kubernetes Liveness/Readiness Probes](https://kubernetes.io/docs/tasks/configure-pod-container/configure-liveness-readiness-startup-probes/)
- [Circuit Breaker Pattern](https://martinfowler.com/bliki/CircuitBreaker.html)
- [Exponential Backoff](https://en.wikipedia.org/wiki/Exponential_backoff)

## Support

For issues or questions:
- Create an issue in the repository
- Contact the DevOps team
- Review application logs
