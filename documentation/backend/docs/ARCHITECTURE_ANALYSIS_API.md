# Cortex AI - Architecture Analysis Part 2: API Layer
**Date**: 2026-05-11  
**Part**: 2 of 4 - API Endpoints, Authentication, Rate Limiting

---

## 5. API Endpoint Architecture

### 5.1 Endpoint Structure

**File**: `backend/app/api/v1/ml_predictions.py`

**Pattern**: FastAPI router with dependency injection

```python
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.limiter import limiter
from app.schemas.ml_predictions import PredictionRequest, PredictionResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ML Predictions"])


@router.post(
    "/predict",
    response_model=PredictionResponse,
    status_code=status.HTTP_200_OK,
    summary="Generate ensemble prediction for an NSE instrument",
)
@limiter.limit("100/minute")
async def predict(
    request: Request,
    body: PredictionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(get_current_user),
    predictor=Depends(get_ml_predictor),
) -> PredictionResponse:
    user_id = current_user.get("user_id", "unknown")
    symbol = body.symbol
    
    try:
        # Business logic
        prediction = await predictor.predict(...)
        
        logger.info(
            "Prediction: user=%s symbol=%s direction=%s confidence=%.4f",
            user_id, symbol, prediction["direction_label"], prediction["confidence"],
        )
        
        return PredictionResponse(...)
    
    except HTTPException:
        raise  # Re-raise HTTP exceptions
    except Exception as exc:
        logger.error(
            "Prediction failed: user=%s symbol=%s error=%s",
            user_id, symbol, exc, exc_info=True
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "prediction_failed", "message": str(exc)},
        )
```

**Key Insights**:
- ✅ **Router pattern**: Separate router per domain (ml_predictions, patterns, etc.)
- ✅ **Dependency injection**: DB, auth, services injected via `Depends()`
- ✅ **Rate limiting**: Applied via decorator (`@limiter.limit()`)
- ✅ **Response models**: Pydantic schemas for validation
- ✅ **Structured logging**: User context included in logs
- ✅ **Error handling**: Catch-all with structured error responses

### 5.2 Endpoint Metadata

**Pattern**: OpenAPI documentation via decorators

```python
@router.post(
    "/predict",
    response_model=PredictionResponse,      # Response schema
    status_code=status.HTTP_200_OK,         # Success status
    summary="Generate ensemble prediction", # Short description
    description="...",                      # Long description (optional)
    tags=["ML Predictions"],                # API grouping
    responses={                             # Error responses
        503: {"description": "Models not loaded"},
        500: {"description": "Prediction failed"},
    }
)
```

**Key Insights**:
- ✅ **Auto-generated docs**: OpenAPI/Swagger UI
- ✅ **Type safety**: Response model validates output
- ✅ **Error documentation**: Expected error codes documented

### 5.3 Graceful Error Handling

**Pattern**: Return 200 with `available=False` for expected failures

```python
try:
    tabular, sequence, current_price, volatility = await feature_loader.load_features(
        symbol=symbol,
        timeframe=timeframe,
    )
except ValueError as exc:
    # No OHLCV history - not an error, just no data
    logger.info("No features available for %s: %s", symbol, exc)
    return PredictionResponse(
        symbol=symbol,
        available=False,
        unavailable_reason="insufficient_data",
    )
```

**Key Insights**:
- ✅ **User-friendly**: 200 status with `available=False` instead of 404
- ✅ **Reason provided**: `unavailable_reason` explains why
- ✅ **Frontend-friendly**: No error handling needed on client

---

## 6. Authentication & Authorization

### 6.1 Dependency Injection Pattern

**File**: `backend/app/api/deps.py`

**Pattern**: Reusable dependencies for auth and resources

```python
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db as get_db_session
from app.core.security import get_current_user_id


# Re-export for convenience
get_db = get_db_session


async def get_current_user(
    user_id: Annotated[str, Depends(get_current_user_id)]
) -> dict:
    """Get current authenticated user."""
    return {"user_id": user_id}


async def get_ml_predictor(request: Request):
    """Get ML predictor from application state."""
    predictor = getattr(request.app.state, "ml_predictor", None)
    
    if predictor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ML models not available.",
        )
    
    return predictor
```

**Key Insights**:
- ✅ **Reusable**: Dependencies used across multiple endpoints
- ✅ **Type annotations**: `Annotated` for dependency chaining
- ✅ **Service availability**: Check app state before returning
- ✅ **Graceful errors**: 503 if service not ready

### 6.2 Authentication Flow

**Pattern**: JWT token validation

```python
# In endpoint
async def predict(
    request: Request,
    body: PredictionRequest,
    current_user: dict = Depends(get_current_user),  # Auth required
):
    user_id = current_user.get("user_id", "unknown")
    # Use user_id for logging, authorization, etc.
```

**Key Insights**:
- ✅ **Automatic validation**: JWT verified before endpoint execution
- ✅ **User context**: User ID available in endpoint
- ✅ **Logging**: User ID included in all logs

### 6.3 Admin-Only Endpoints

**Pattern**: Role-based access control

```python
from app.core.auth import require_admin_role

@router.post(
    "/admin/reload",
    summary="Hot-reload production models",
    include_in_schema=False,  # Hide from public docs
)
async def admin_reload(
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin_user_id: str = Depends(require_admin_role),  # Admin only
):
    # Admin-only logic
    pass
```

**Key Insights**:
- ✅ **Role enforcement**: `require_admin_role` dependency
- ✅ **Hidden from docs**: `include_in_schema=False`
- ✅ **Audit trail**: Admin user ID captured

---

## 7. Rate Limiting

### 7.1 Rate Limiter Pattern

**Pattern**: Decorator-based rate limiting

```python
from app.core.limiter import limiter

@router.post("/predict")
@limiter.limit("100/minute")  # 100 requests per minute per user
async def predict(request: Request, ...):
    pass
```

**Key Insights**:
- ✅ **Per-user limits**: Based on JWT user ID
- ✅ **Configurable**: Different limits per endpoint
- ✅ **Automatic enforcement**: No manual checking needed

### 7.2 Rate Limit Configuration

**Common Patterns**:
```python
@limiter.limit("100/minute")   # Standard endpoints
@limiter.limit("10/minute")    # Expensive operations
@limiter.limit("1000/minute")  # High-frequency endpoints
```

**Key Insights**:
- ✅ **Tiered limits**: Based on operation cost
- ✅ **Protection**: Prevents abuse and overload
- ✅ **User experience**: Reasonable limits for normal usage

---

## 8. Request/Response Schemas

### 8.1 Pydantic Schema Pattern

**File**: `backend/app/schemas/ml_predictions.py`

**Pattern**: Pydantic models with validation

```python
from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime

class PredictionRequest(BaseModel):
    """Request schema for ML prediction."""
    symbol: str = Field(..., min_length=1, max_length=50)
    timeframe: str = Field("1D", pattern="^(1m|5m|15m|1h|4h|1D|1W)$")


class PredictionResponse(BaseModel):
    """Response schema for ML prediction."""
    model_config = ConfigDict(protected_namespaces=())
    
    symbol: str
    available: bool = True
    unavailable_reason: Optional[str] = None
    
    direction: Optional[str] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit_1: Optional[float] = None
    predicted_at: Optional[datetime] = None
```

**Key Insights**:
- ✅ **Validation**: Field constraints enforced automatically
- ✅ **Type safety**: IDE autocomplete and type checking
- ✅ **Documentation**: Field descriptions in OpenAPI
- ✅ **Optional fields**: Nullable fields for graceful degradation

### 8.2 Schema Inheritance

**Pattern**: Base class for common configuration

```python
class _MLBase(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class PredictionResponse(_MLBase):
    # Inherits configuration
    symbol: str
    # ...
```

**Key Insights**:
- ✅ **DRY principle**: Common config in base class
- ✅ **Consistency**: All schemas share configuration

---

## 9. Logging Patterns

### 9.1 Structured Logging

**Pattern**: Context-rich log messages

```python
logger.info(
    "Prediction: user=%s symbol=%s direction=%s confidence=%.4f",
    user_id, symbol, prediction["direction_label"], prediction["confidence"],
)

logger.error(
    "Prediction failed: user=%s symbol=%s error=%s",
    user_id, symbol, exc, exc_info=True  # Include stack trace
)
```

**Key Insights**:
- ✅ **Structured format**: Key=value pairs for parsing
- ✅ **User context**: User ID in all logs
- ✅ **Stack traces**: `exc_info=True` for errors
- ✅ **Log levels**: INFO for success, ERROR for failures

### 9.2 Logger Configuration

**Pattern**: Module-level logger

```python
import logging

logger = logging.getLogger(__name__)  # Use module name
```

**Key Insights**:
- ✅ **Module-specific**: Each module has its own logger
- ✅ **Hierarchical**: Can configure by module path
- ✅ **Standard library**: No external dependencies

---

## 10. Summary - API Layer

### Patterns to Follow

1. **Endpoint Structure**:
   - Use `APIRouter` with tags
   - Apply rate limiting via decorator
   - Inject dependencies (db, auth, services)
   - Return Pydantic response models
   - Include OpenAPI metadata

2. **Authentication**:
   - Use `Depends(get_current_user)` for auth
   - Extract user_id for logging
   - Use `require_admin_role` for admin endpoints

3. **Error Handling**:
   - Catch all exceptions at endpoint level
   - Log with user context
   - Return structured error responses
   - Use 200 + `available=False` for expected failures

4. **Schemas**:
   - Pydantic models for request/response
   - Field validation with constraints
   - Optional fields for graceful degradation

5. **Logging**:
   - Structured format (key=value)
   - User context in all logs
   - Stack traces for errors

### Integration Checklist

- [ ] Router created with appropriate tags
- [ ] Rate limiting applied
- [ ] Authentication dependency added
- [ ] Request/response schemas defined
- [ ] Error handling with try-except
- [ ] Structured logging with user context
- [ ] OpenAPI metadata complete

---

**Next**: Part 3 - Redis Caching and Testing Patterns

See: `ARCHITECTURE_ANALYSIS_REDIS.md`
