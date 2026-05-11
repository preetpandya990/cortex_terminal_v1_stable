# Trade Suggestions ORM Models Documentation

**File**: `backend/app/models/trade_suggestions.py`  
**Created**: 2026-04-22  
**SQLAlchemy Version**: 2.0+  
**Database**: PostgreSQL 16 with asyncpg driver

## Overview

Production-grade SQLAlchemy 2.0 ORM models for the Hawk-Eye Radar multi-agent trade suggestion system. These models represent high-conviction trade opportunities validated across three independent agents: Technical Scanner, AI Intelligence, and ML Predictor.

## Architecture Principles

### 1. **SQLAlchemy 2.0 Best Practices**
- ✅ Modern `Mapped[]` type hints for superior IDE support and type checking
- ✅ `mapped_column()` directive for ORM-specific column configuration
- ✅ Explicit `server_default` for database-level defaults
- ✅ PostgreSQL-specific types (`JSONB`, `UUID`) for optimal performance
- ✅ Bidirectional relationships with proper cascade rules

### 2. **Production Standards**
- ✅ Comprehensive docstrings for all models
- ✅ Type safety with Python 3.10+ union syntax (`str | None`)
- ✅ Explicit nullable/non-nullable declarations
- ✅ Database constraints documented in `__table_args__`
- ✅ Meaningful `__repr__` for debugging

### 3. **Performance Optimizations**
- ✅ UUID primary keys for distributed systems compatibility
- ✅ JSONB for flexible signal storage with indexing support
- ✅ Indexed foreign keys for fast joins
- ✅ Timezone-aware timestamps for global deployments

## Models

### TradeSuggestion

**Purpose**: Represents a validated trade opportunity with consensus from all three agents.

**Table**: `trade_suggestions`

#### Columns

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | BIGINT | No | Auto | Sequential primary key |
| `suggestion_id` | UUID | No | gen_random_uuid() | Distributed-safe unique identifier |
| `symbol` | VARCHAR(50) | No | - | Trading symbol (e.g., "RELIANCE") |
| `instrument_key` | VARCHAR(100) | No | - | Broker instrument key |
| `trading_symbol` | VARCHAR(50) | Yes | NULL | Display name |
| `consensus_score` | NUMERIC(5,2) | No | - | Weighted score 0-100 |
| `confidence_level` | VARCHAR(20) | No | - | HIGH/MEDIUM/LOW |
| `signal_direction` | VARCHAR(10) | No | - | BUY/SELL |
| `trigger_pathway` | VARCHAR(20) | No | - | TECHNICAL_FIRST/FUNDAMENTAL_FIRST |
| `scanner_signal` | JSONB | No | - | Technical analysis output |
| `ai_signal` | JSONB | No | - | News/sentiment analysis output |
| `ml_signal` | JSONB | No | - | ML prediction output |
| `entry_price` | NUMERIC(12,2) | Yes | NULL | Recommended entry price |
| `stop_loss` | NUMERIC(12,2) | Yes | NULL | Stop loss price |
| `risk_reward_ratio` | NUMERIC(5,2) | Yes | NULL | Risk/reward ratio |
| `take_profit_1` | NUMERIC(12,2) | Yes | NULL | First target |
| `take_profit_2` | NUMERIC(12,2) | Yes | NULL | Second target |
| `take_profit_3` | NUMERIC(12,2) | Yes | NULL | Third target |
| `generated_at` | TIMESTAMPTZ | No | NOW() | When suggestion was created |
| `expires_at` | TIMESTAMPTZ | No | - | When suggestion expires |
| `status` | VARCHAR(20) | No | 'active' | active/expired/executed/invalidated |
| `created_at` | TIMESTAMPTZ | No | NOW() | Record creation timestamp |
| `updated_at` | TIMESTAMPTZ | No | NOW() | Record update timestamp |

#### Constraints

```sql
CHECK (consensus_score >= 0 AND consensus_score <= 100)
CHECK (confidence_level IN ('HIGH', 'MEDIUM', 'LOW'))
CHECK (signal_direction IN ('BUY', 'SELL'))
CHECK (trigger_pathway IN ('TECHNICAL_FIRST', 'FUNDAMENTAL_FIRST'))
CHECK (status IN ('active', 'expired', 'executed', 'invalidated'))
```

#### Relationships

- **correlations**: One-to-many with `EventCorrelation`
  - Cascade: `all, delete-orphan` (deleting suggestion deletes correlations)
  - Back-populates: `suggestion`

#### Usage Example

```python
from app.models import TradeSuggestion
from sqlalchemy import select
from decimal import Decimal
from datetime import datetime, timedelta, timezone

# Create a new suggestion
suggestion = TradeSuggestion(
    symbol="RELIANCE",
    instrument_key="NSE_EQ|INE002A01018",
    trading_symbol="RELIANCE",
    consensus_score=Decimal("85.50"),
    confidence_level="HIGH",
    signal_direction="BUY",
    trigger_pathway="TECHNICAL_FIRST",
    scanner_signal={"direction": "buy", "confidence": 90},
    ai_signal={"score": 0.8, "sentiment": "positive"},
    ml_signal={"prediction": "BUY", "confidence": 0.85},
    entry_price=Decimal("2450.00"),
    stop_loss=Decimal("2400.00"),
    risk_reward_ratio=Decimal("3.0"),
    take_profit_1=Decimal("2550.00"),
    expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
)

async with session.begin():
    session.add(suggestion)
    await session.commit()

# Query active suggestions
stmt = (
    select(TradeSuggestion)
    .where(TradeSuggestion.status == "active")
    .where(TradeSuggestion.consensus_score >= 80)
    .order_by(TradeSuggestion.consensus_score.desc())
)
results = await session.execute(stmt)
suggestions = results.scalars().all()
```

---

### EventCorrelation

**Purpose**: Audit trail for bidirectional signal validation events, tracking agent response times and consensus decisions.

**Table**: `event_correlations`

#### Columns

| Column | Type | Nullable | Default | Description |
|--------|------|----------|---------|-------------|
| `id` | BIGINT | No | Auto | Sequential primary key |
| `correlation_id` | UUID | No | gen_random_uuid() | Unique correlation identifier |
| `suggestion_id` | UUID | Yes | NULL | FK to trade_suggestions (SET NULL on delete) |
| `trigger_type` | VARCHAR(20) | No | - | SCANNER_ANOMALY/NEWS_EVENT |
| `trigger_timestamp` | TIMESTAMPTZ | No | - | When correlation started |
| `scanner_response_ms` | INTEGER | Yes | NULL | Scanner latency in milliseconds |
| `ai_response_ms` | INTEGER | Yes | NULL | AI latency in milliseconds |
| `ml_response_ms` | INTEGER | Yes | NULL | ML latency in milliseconds |
| `total_latency_ms` | INTEGER | Yes | NULL | Total consensus latency |
| `consensus_reached` | BOOLEAN | No | - | Whether consensus was achieved |
| `rejection_reason` | VARCHAR(200) | Yes | NULL | Why consensus failed |
| `scanner_output` | JSONB | Yes | NULL | Raw scanner output for debugging |
| `ai_output` | JSONB | Yes | NULL | Raw AI output for debugging |
| `ml_output` | JSONB | Yes | NULL | Raw ML output for debugging |
| `created_at` | TIMESTAMPTZ | No | NOW() | Record creation timestamp |

#### Constraints

```sql
CHECK (trigger_type IN ('SCANNER_ANOMALY', 'NEWS_EVENT'))
```

#### Foreign Keys

```sql
FOREIGN KEY (suggestion_id) 
  REFERENCES trade_suggestions(suggestion_id) 
  ON DELETE SET NULL
```

#### Relationships

- **suggestion**: Many-to-one with `TradeSuggestion`
  - Back-populates: `correlations`
  - Nullable: Yes (correlation can exist without suggestion if consensus failed)

#### Usage Example

```python
from app.models import EventCorrelation
from datetime import datetime, timezone

# Record a correlation event
correlation = EventCorrelation(
    suggestion_id=suggestion.suggestion_id,  # Can be None if consensus failed
    trigger_type="SCANNER_ANOMALY",
    trigger_timestamp=datetime.now(timezone.utc),
    scanner_response_ms=15,
    ai_response_ms=120,
    ml_response_ms=45,
    total_latency_ms=180,
    consensus_reached=True,
    scanner_output={"signals": [...]},
    ai_output={"events": [...]},
    ml_output={"prediction": {...}},
)

async with session.begin():
    session.add(correlation)
    await session.commit()

# Query failed correlations for debugging
stmt = (
    select(EventCorrelation)
    .where(EventCorrelation.consensus_reached == False)
    .where(EventCorrelation.trigger_timestamp >= cutoff_time)
    .order_by(EventCorrelation.trigger_timestamp.desc())
)
failed = await session.execute(stmt)
```

## Type Safety

### Python Type Hints

All models use SQLAlchemy 2.0's `Mapped[]` type hints for maximum type safety:

```python
# Non-nullable fields
symbol: Mapped[str] = mapped_column(String(50), nullable=False)

# Nullable fields (Python 3.10+ union syntax)
trading_symbol: Mapped[str | None] = mapped_column(String(50))

# Complex types
scanner_signal: Mapped[dict] = mapped_column(JSONB, nullable=False)
consensus_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

### UUID Handling

UUIDs are stored as native PostgreSQL UUID type for optimal performance:

```python
from uuid import UUID, uuid4

suggestion_id: Mapped[UUID] = mapped_column(
    PG_UUID(as_uuid=True),  # Converts to/from Python UUID objects
    unique=True,
    nullable=False,
    default=uuid4,  # Python-side default
    server_default=text("gen_random_uuid()")  # Database-side default
)
```

## Performance Considerations

### Indexes

Indexes are created at the migration level but documented here:

**TradeSuggestion**:
- `idx_suggestions_symbol_status` - Partial index on (symbol, status) WHERE status='active'
- `idx_suggestions_generated_at_desc` - Descending index on generated_at
- `idx_suggestions_consensus_score` - Partial index on consensus_score WHERE status='active'
- `idx_suggestions_landing_page` - Composite index for landing page queries

**EventCorrelation**:
- `idx_correlations_suggestion` - Index on suggestion_id for joins
- `idx_correlations_trigger_timestamp` - Descending index on trigger_timestamp
- `idx_correlations_latency` - Partial index on total_latency_ms WHERE consensus_reached=TRUE

### JSONB Optimization

JSONB columns support GIN indexing for fast JSON queries:

```sql
-- Can be added if needed for specific query patterns
CREATE INDEX idx_scanner_signal_gin ON trade_suggestions USING GIN (scanner_signal);
```

### Query Patterns

**Efficient landing page query** (uses composite index):
```python
stmt = (
    select(TradeSuggestion)
    .where(TradeSuggestion.status == "active")
    .where(TradeSuggestion.consensus_score >= 60)
    .order_by(
        TradeSuggestion.consensus_score.desc(),
        TradeSuggestion.generated_at.desc()
    )
    .limit(50)
)
```

**Latency monitoring query**:
```python
from sqlalchemy import func

stmt = (
    select(func.avg(EventCorrelation.total_latency_ms))
    .where(EventCorrelation.consensus_reached == True)
    .where(EventCorrelation.trigger_timestamp >= cutoff)
)
avg_latency = await session.scalar(stmt)
```

## Migration Compatibility

These models are designed to work with migration `0011_trade_suggestions.py`. The migration creates:
- Tables with exact column definitions
- All indexes for optimal query performance
- CHECK constraints for data integrity
- Foreign key with SET NULL on delete

**Important**: Always apply migrations before using these models in production.

## Testing

### Unit Test Example

```python
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from app.models import TradeSuggestion, EventCorrelation

@pytest.mark.asyncio
async def test_create_suggestion(async_session):
    """Test creating a trade suggestion with all required fields."""
    suggestion = TradeSuggestion(
        symbol="TEST",
        instrument_key="NSE_EQ|TEST",
        consensus_score=Decimal("75.00"),
        confidence_level="HIGH",
        signal_direction="BUY",
        trigger_pathway="TECHNICAL_FIRST",
        scanner_signal={"test": "data"},
        ai_signal={"test": "data"},
        ml_signal={"test": "data"},
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    
    async_session.add(suggestion)
    await async_session.commit()
    
    assert suggestion.id is not None
    assert suggestion.suggestion_id is not None
    assert suggestion.status == "active"
```

## Security Considerations

1. **UUID Primary Keys**: Prevents enumeration attacks
2. **JSONB Validation**: Application-level validation required before storing
3. **Timestamp Timezone**: All timestamps are timezone-aware (UTC)
4. **Cascade Rules**: Deleting suggestions automatically cleans up correlations
5. **Nullable Foreign Keys**: Correlations persist even if suggestion is deleted

## Maintenance

### Adding New Fields

When adding fields, follow this pattern:

1. Create new migration with `alembic revision -m "description"`
2. Add column to migration's `upgrade()` function
3. Add corresponding `Mapped[]` field to model
4. Update `__table_args__` if adding constraints
5. Run tests to verify compatibility

### Modifying Constraints

Constraints are defined in both migration and model:
- Migration: Creates actual database constraints
- Model: Documents constraints for ORM awareness

Always keep them in sync.

## See Also

- Migration: `backend/alembic/versions/0011_trade_suggestions.py`
- Schemas: `backend/app/schemas/trade_suggestions.py` (to be created)
- Service: `backend/app/ai/correlation/engine.py` (to be created)
- API: `backend/app/api/v1/trade_suggestions.py` (to be created)

---

**Status**: ✅ Production Ready  
**Last Updated**: 2026-04-22  
**Maintainer**: Cortex AI Team
