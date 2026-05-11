# Structured Logging Migration Guide

## Overview

This guide shows how to migrate existing logging calls to use structured logging with contextual fields.

## Key Principles

1. **Use `extra={}` for structured fields** - Add contextual data as key-value pairs
2. **Keep messages human-readable** - Don't put data in the message string
3. **Use consistent field names** - Follow naming conventions below
4. **Add request_id when available** - Use `get_request_id()` for correlation

## Field Naming Conventions

```python
# Standard fields (automatically added by middleware)
request_id: str          # Unique request identifier
correlation_id: str      # Cross-service correlation ID
user_id: str            # Authenticated user ID
endpoint: str           # API endpoint path
method: str             # HTTP method
duration_ms: float      # Request duration

# Business logic fields (add via extra={})
symbol: str             # Trading symbol (e.g., "RELIANCE")
suggestion_id: str      # Trade suggestion UUID
consensus_score: float  # Consensus score (0-100)
action: str            # Trading action (BUY/SELL/HOLD)
model_id: str          # ML model identifier
event_type: str        # Event type (news, anomaly, etc.)
status: str            # Status (success, failed, pending)
error: str             # Error message (for errors only)
```

## Migration Examples

### Before (Plain Logging)

```python
logger.info(f"Processing suggestion {suggestion_id} for {symbol}")
logger.error(f"Failed to generate consensus: {error}")
logger.warning(f"Circuit breaker opened for {agent_name}")
```

### After (Structured Logging)

```python
from app.core.request_context import get_request_id

logger.info(
    "Processing trade suggestion",
    extra={
        "request_id": get_request_id(),
        "suggestion_id": str(suggestion_id),
        "symbol": symbol,
        "action": "process",
    }
)

logger.error(
    "Consensus generation failed",
    extra={
        "request_id": get_request_id(),
        "suggestion_id": str(suggestion_id),
        "error": str(error),
        "status": "failed",
    },
    exc_info=True,  # Include stack trace
)

logger.warning(
    "Circuit breaker opened",
    extra={
        "agent_name": agent_name,
        "failure_count": failure_count,
        "status": "circuit_open",
    }
)
```

## Critical Modules to Update

### Priority 1 (High Traffic)
- `app/api/v1/trade_suggestions.py` - Trade suggestions API
- `app/ai/correlation/engine.py` - Correlation engine
- `app/middleware/metrics.py` - Metrics middleware
- `app/worker.py` - Background worker loops

### Priority 2 (Business Logic)
- `app/ai/intelligence/event_processor.py` - Event processing
- `app/services/data_ingestion_worker.py` - Data ingestion
- `app/ai/safety/safety_trigger_engine.py` - Safety monitoring
- `app/ml/inference/ensemble_predictor.py` - ML predictions

### Priority 3 (Supporting Services)
- `app/services/upstox_client.py` - Upstox client
- `app/ai/ingestion/rss_fetcher.py` - RSS fetching
- `app/ai/intelligence/llm_client.py` - LLM client

## PII Masking

Use explicit masking for sensitive data:

```python
from app.core.logging_config import mask_email, mask_token, mask_pii

logger.info(
    "User authenticated",
    extra={
        "user_id": user_id,
        "email": mask_email(user.email),
        "token": mask_token(access_token),
    }
)

# For complex objects
user_data = {"email": "user@example.com", "password": "secret123"}
logger.debug(
    "User data received",
    extra={"user_data": mask_pii(user_data)}
)
```

## Testing Structured Logs

```python
import json
import logging
from io import StringIO

# Capture logs
log_stream = StringIO()
handler = logging.StreamHandler(log_stream)
handler.setFormatter(ContextualJsonFormatter())
logger.addHandler(handler)

# Generate log
logger.info("Test message", extra={"test_field": "test_value"})

# Parse and validate
log_output = log_stream.getvalue()
log_json = json.loads(log_output)

assert log_json["message"] == "Test message"
assert log_json["test_field"] == "test_value"
assert "timestamp" in log_json
assert "level" in log_json
```

## Benefits

1. **Faster Debugging** - Search logs by request_id, user_id, symbol, etc.
2. **Better Monitoring** - Aggregate metrics from structured fields
3. **Compliance** - Audit trails with full context
4. **Distributed Tracing** - Follow requests across services via correlation_id
5. **Machine Readable** - Easy integration with log aggregation tools (ELK, Loki, CloudWatch)

## Next Steps

1. Update critical modules (Priority 1) first
2. Add integration tests for structured logging
3. Configure log aggregation in production
4. Set up alerts based on structured fields
5. Create Grafana dashboards from log metrics
