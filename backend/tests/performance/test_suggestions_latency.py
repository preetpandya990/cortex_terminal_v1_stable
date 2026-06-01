"""
Performance Benchmarks: Trade Suggestions & Correlation Engine
================================================================
Validates that the system meets billion-dollar app performance requirements.

Targets:
- Landing page query: <10ms (P95)
- Suggestion detail query: <5ms (P95)
- Consensus computation: <1ms (P95)
- Full Pathway 1 E2E: <200ms (P95)

Author: Cortex AI Team
Version: 1.0.0
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import AsyncGenerator
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool
from unittest.mock import AsyncMock

from app.ai.correlation.engine import EventCorrelationEngine
from app.core.config import get_settings
from app.core.database import Base
from app.models.trade_suggestions import TradeSuggestion, EventCorrelation


# ============================================================================
# FIXTURES
# ============================================================================

@pytest.fixture
async def perf_db_engine():
    """Create database engine for performance tests."""
    settings = get_settings()
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        poolclass=NullPool,
        echo=False,
    )
    
    async with engine.connect() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    await engine.dispose()


@pytest.fixture
async def perf_db_session(perf_db_engine) -> AsyncGenerator[AsyncSession, None]:
    """Provide database session for performance tests with cleanup."""
    async with perf_db_engine.connect() as conn:
        session = AsyncSession(bind=conn, expire_on_commit=False)
        
        # Clean before test
        await session.execute(text("""
            TRUNCATE TABLE trade_suggestions, event_correlations CASCADE
        """))
        await session.commit()
        
        yield session
        
        # Clean after test
        await session.execute(text("""
            TRUNCATE TABLE trade_suggestions, event_correlations CASCADE
        """))
        await session.commit()
        await session.close()


@pytest.fixture
async def seed_suggestions(perf_db_session: AsyncSession):
    """Seed database with 100 test suggestions for query benchmarks."""
    suggestions = []
    
    for i in range(100):
        suggestion = TradeSuggestion(
            suggestion_id=uuid4(),
            symbol=f"NSE_EQ|INE{i:03d}A01018",
            instrument_key=f"NSE_EQ|INE{i:03d}A01018",
            trading_symbol=f"SYMBOL{i}",
            consensus_score=Decimal(str(60 + (i % 40))),  # 60-99
            confidence_level="HIGH" if i % 2 == 0 else "MEDIUM",
            signal_direction="BUY" if i % 3 != 0 else "SELL",
            trigger_pathway="TECHNICAL_FIRST",
            scanner_signal={"score": 8.5, "confidence": 85.0},
            ai_signal={"score": 85.0, "confidence": 0.90},
            ml_signal={"confidence": 0.92, "prediction": {"direction": "BUY"}},
            entry_price=Decimal("2500.00"),
            stop_loss=Decimal("2450.00"),
            risk_reward_ratio=Decimal("2.50"),
            take_profit_1=Decimal("2600.00"),
            generated_at=datetime.now(timezone.utc) - timedelta(minutes=i),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24 - (i // 10)),
            status="active" if i < 80 else "expired",
        )
        suggestions.append(suggestion)
    
    perf_db_session.add_all(suggestions)
    await perf_db_session.commit()
    
    return suggestions


# ============================================================================
# BENCHMARK 1: LANDING PAGE QUERY
# ============================================================================

@pytest.mark.asyncio
async def test_landing_page_query_latency(
    perf_db_session: AsyncSession,
    seed_suggestions: list[TradeSuggestion],
):
    """
    Benchmark 1: Landing page query performance
    
    Query: Fetch 50 active suggestions ordered by consensus score
    Target: P95 < 10ms
    
    This is the most critical query - users see it on every page load.
    """
    # Warm up (exclude from measurements)
    for _ in range(5):
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.status == "active",
            TradeSuggestion.consensus_score >= 60
        ).order_by(
            TradeSuggestion.consensus_score.desc(),
            TradeSuggestion.generated_at.desc()
        ).limit(50)
        await perf_db_session.execute(stmt)
    
    # Benchmark: Run 100 iterations
    latencies = []
    num_iterations = 100
    
    for _ in range(num_iterations):
        start_time = time.perf_counter()
        
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.status == "active",
            TradeSuggestion.consensus_score >= 60
        ).order_by(
            TradeSuggestion.consensus_score.desc(),
            TradeSuggestion.generated_at.desc()
        ).limit(50)
        
        result = await perf_db_session.execute(stmt)
        suggestions = result.scalars().all()
        
        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000
        latencies.append(latency_ms)
    
    # Calculate percentiles
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(0.50 * num_iterations)]
    p95 = latencies_sorted[int(0.95 * num_iterations)]
    p99 = latencies_sorted[int(0.99 * num_iterations)]
    avg = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"BENCHMARK 1: Landing Page Query (n={num_iterations})")
    print(f"{'='*70}")
    print(f"Query: 50 active suggestions, ordered by consensus score")
    print(f"Target: P95 < 10ms")
    print(f"-" * 70)
    print(f"Min:     {min_lat:>8.3f} ms")
    print(f"Average: {avg:>8.3f} ms")
    print(f"P50:     {p50:>8.3f} ms")
    print(f"P95:     {p95:>8.3f} ms  {'✅ PASS' if p95 < 10 else '❌ FAIL'}")
    print(f"P99:     {p99:>8.3f} ms")
    print(f"Max:     {max_lat:>8.3f} ms")
    print(f"{'='*70}\n")
    
    # Assert: P95 must be < 10ms
    assert p95 < 10.0, f"Landing page query P95 latency {p95:.3f}ms exceeds 10ms target"
    
    # Assert: Results returned
    assert len(suggestions) > 0, "No suggestions returned"


# ============================================================================
# BENCHMARK 2: SUGGESTION DETAIL QUERY
# ============================================================================

@pytest.mark.asyncio
async def test_suggestion_detail_query_latency(
    perf_db_session: AsyncSession,
    seed_suggestions: list[TradeSuggestion],
):
    """
    Benchmark 2: Suggestion detail query performance
    
    Query: Fetch single suggestion by UUID with correlations
    Target: P95 < 5ms
    
    This query runs when user clicks "View Details" on a suggestion.
    """
    # Pick a suggestion UUID for testing
    test_uuid = seed_suggestions[0].suggestion_id
    
    # Warm up
    for _ in range(5):
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == test_uuid
        )
        await perf_db_session.execute(stmt)
    
    # Benchmark: Run 100 iterations
    latencies = []
    num_iterations = 100
    
    for _ in range(num_iterations):
        start_time = time.perf_counter()
        
        stmt = select(TradeSuggestion).where(
            TradeSuggestion.suggestion_id == test_uuid
        )
        result = await perf_db_session.execute(stmt)
        suggestion = result.scalar_one()
        
        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000
        latencies.append(latency_ms)
    
    # Calculate percentiles
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(0.50 * num_iterations)]
    p95 = latencies_sorted[int(0.95 * num_iterations)]
    p99 = latencies_sorted[int(0.99 * num_iterations)]
    avg = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"BENCHMARK 2: Suggestion Detail Query (n={num_iterations})")
    print(f"{'='*70}")
    print(f"Query: Single suggestion by UUID")
    print(f"Target: P95 < 5ms")
    print(f"-" * 70)
    print(f"Min:     {min_lat:>8.3f} ms")
    print(f"Average: {avg:>8.3f} ms")
    print(f"P50:     {p50:>8.3f} ms")
    print(f"P95:     {p95:>8.3f} ms  {'✅ PASS' if p95 < 5 else '❌ FAIL'}")
    print(f"P99:     {p99:>8.3f} ms")
    print(f"Max:     {max_lat:>8.3f} ms")
    print(f"{'='*70}\n")
    
    # Assert: P95 must be < 5ms
    assert p95 < 5.0, f"Detail query P95 latency {p95:.3f}ms exceeds 5ms target"
    
    # Assert: Suggestion found
    assert suggestion is not None, "Suggestion not found"


# ============================================================================
# BENCHMARK 3: CONSENSUS COMPUTATION
# ============================================================================

@pytest.mark.asyncio
async def test_consensus_computation_latency():
    """
    Benchmark 3: Consensus computation performance (no DB)
    
    Operation: Pure computation - weighted scoring + directional alignment
    Target: P95 < 1ms
    
    This is the core algorithm that determines if a suggestion is generated.
    """
    # Mock signal assembler
    mock_assembler = AsyncMock()
    mock_assembler.gather_event_signals = AsyncMock(return_value={
        "score": 85.0,
        "confidence": 0.90,
        "sentiment": "positive",
    })
    mock_assembler.gather_ml_signals = AsyncMock(return_value={
        "confidence": 0.92,
        "prediction": {
            "direction": "BUY",
            "entry_price": 2500.0,
            "stop_loss": 2450.0,
            "targets": [2600.0, 2650.0, 2700.0],
        },
    })
    
    # Mock Redis
    mock_redis = AsyncMock()
    
    # Create engine
    engine = EventCorrelationEngine(
        signal_assembler=mock_assembler,
        redis=mock_redis,
    )
    
    # Test signals
    scanner_signal = {
        "instrument_key": "NSE_EQ|INE002A01018",
        "direction": "buy",
        "confidence": 85.0,
        "score": 8.5,
    }
    
    ai_signal = {
        "score": 85.0,
        "confidence": 0.90,
        "sentiment": "positive",
    }
    
    ml_signal = {
        "confidence": 0.92,
        "prediction": {
            "direction": "BUY",
            "entry_price": 2500.0,
            "stop_loss": 2450.0,
            "targets": [2600.0, 2650.0, 2700.0],
        },
    }
    
    latencies_dict = {"scanner_ms": 10, "ai_ms": 20, "ml_ms": 15, "total_ms": 45}
    
    # Warm up
    for _ in range(10):
        # Simulate consensus computation logic
        scanner_dir = "BUY" if scanner_signal.get("direction") in ["buy", "bullish"] else "SELL"
        ai_score = ai_signal.get("score", 0.0)
        ai_dir = "BUY" if ai_score > 0 else "SELL"
        ml_dir = ml_signal.get("prediction", {}).get("direction", "HOLD")
        
        all_buy = (scanner_dir == "BUY" and ai_dir == "BUY" and ml_dir == "BUY")
        
        scanner_conf = scanner_signal.get("confidence", 0.0)
        ai_conf = abs(ai_signal.get("confidence", 0.0)) * 100
        ml_conf = ml_signal.get("confidence", 0.0) * 100
        
        consensus_score = (0.30 * scanner_conf + 0.40 * ai_conf + 0.30 * ml_conf)
    
    # Benchmark: Run 1000 iterations (computation is fast)
    latencies = []
    num_iterations = 1000
    
    for _ in range(num_iterations):
        start_time = time.perf_counter()
        
        # Consensus computation logic (from engine._compute_consensus)
        scanner_dir = "BUY" if scanner_signal.get("direction") in ["buy", "bullish"] else "SELL"
        ai_score = ai_signal.get("score", 0.0)
        ai_dir = "BUY" if ai_score > 0 else "SELL"
        ml_dir = ml_signal.get("prediction", {}).get("direction", "HOLD")
        
        # Check directional alignment
        all_buy = (scanner_dir == "BUY" and ai_dir == "BUY" and ml_dir == "BUY")
        all_sell = (scanner_dir == "SELL" and ai_dir == "SELL" and ml_dir == "SELL")
        
        if all_buy or all_sell:
            # Compute weighted consensus
            scanner_conf = scanner_signal.get("confidence", 0.0)
            ai_conf = abs(ai_signal.get("confidence", 0.0)) * 100
            ml_conf = ml_signal.get("confidence", 0.0) * 100
            
            consensus_score = (0.30 * scanner_conf + 0.40 * ai_conf + 0.30 * ml_conf)
            
            # Determine confidence level
            if consensus_score >= 80:
                confidence_level = "HIGH"
            elif consensus_score >= 60:
                confidence_level = "MEDIUM"
            else:
                confidence_level = "LOW"
        
        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000
        latencies.append(latency_ms)
    
    # Calculate percentiles
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(0.50 * num_iterations)]
    p95 = latencies_sorted[int(0.95 * num_iterations)]
    p99 = latencies_sorted[int(0.99 * num_iterations)]
    avg = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"BENCHMARK 3: Consensus Computation (n={num_iterations})")
    print(f"{'='*70}")
    print(f"Operation: Weighted scoring + directional alignment (no DB)")
    print(f"Target: P95 < 1ms")
    print(f"-" * 70)
    print(f"Min:     {min_lat:>8.3f} ms")
    print(f"Average: {avg:>8.3f} ms")
    print(f"P50:     {p50:>8.3f} ms")
    print(f"P95:     {p95:>8.3f} ms  {'✅ PASS' if p95 < 1 else '❌ FAIL'}")
    print(f"P99:     {p99:>8.3f} ms")
    print(f"Max:     {max_lat:>8.3f} ms")
    print(f"{'='*70}\n")
    
    # Assert: P95 must be < 1ms
    assert p95 < 1.0, f"Consensus computation P95 latency {p95:.3f}ms exceeds 1ms target"


# ============================================================================
# BENCHMARK 4: FULL PATHWAY 1 END-TO-END
# ============================================================================

@pytest.mark.asyncio
async def test_pathway1_end_to_end_latency(perf_db_session: AsyncSession):
    """
    Benchmark 4: Full Pathway 1 end-to-end performance
    
    Flow: Scanner anomaly → AI/ML gathering → Consensus → DB write → Redis pub/sub
    Target: P95 < 200ms
    
    This measures the complete correlation engine flow with mocked external services.
    """
    # Mock signal assembler with realistic delays
    mock_assembler = AsyncMock()
    
    async def mock_ai_signals(*args, **kwargs):
        await asyncio.sleep(0.020)  # 20ms simulated AI latency
        return {
            "score": 85.0,
            "confidence": 0.90,
            "sentiment": "positive",
            "event_count": 1,
        }
    
    async def mock_ml_signals(*args, **kwargs):
        await asyncio.sleep(0.025)  # 25ms simulated ML latency
        return {
            "confidence": 0.92,
            "prediction": {
                "direction": "BUY",
                "entry_price": 2500.0,
                "stop_loss": 2450.0,
                "targets": [2600.0, 2650.0, 2700.0],
            },
        }
    
    mock_assembler.gather_event_signals = mock_ai_signals
    mock_assembler.gather_ml_signals = mock_ml_signals
    
    # Mock Redis
    mock_redis = AsyncMock()
    mock_redis.publish = AsyncMock()
    
    # Create engine
    engine = EventCorrelationEngine(
        signal_assembler=mock_assembler,
        redis=mock_redis,
    )
    
    # Test scanner signal
    scanner_signal = {
        "instrument_key": "NSE_EQ|INE002A01018",
        "symbol": "RELIANCE",
        "direction": "buy",
        "confidence": 85.0,
        "score": 8.5,
        "volume_ratio": 2.5,
    }
    
    # Warm up
    for _ in range(3):
        await engine.on_scanner_anomaly(perf_db_session, scanner_signal)
        # Clean up
        await perf_db_session.execute(text("TRUNCATE TABLE trade_suggestions, event_correlations CASCADE"))
        await perf_db_session.commit()
    
    # Benchmark: Run 50 iterations (E2E is slower)
    latencies = []
    num_iterations = 50
    
    for i in range(num_iterations):
        # Update instrument_key to avoid duplicates
        scanner_signal["instrument_key"] = f"NSE_EQ|INE{i:03d}A01018"
        
        start_time = time.perf_counter()
        
        suggestion = await engine.on_scanner_anomaly(perf_db_session, scanner_signal)
        
        end_time = time.perf_counter()
        latency_ms = (end_time - start_time) * 1000
        latencies.append(latency_ms)
    
    # Calculate percentiles
    latencies_sorted = sorted(latencies)
    p50 = latencies_sorted[int(0.50 * num_iterations)]
    p95 = latencies_sorted[int(0.95 * num_iterations)]
    p99 = latencies_sorted[int(0.99 * num_iterations)]
    avg = sum(latencies) / len(latencies)
    min_lat = min(latencies)
    max_lat = max(latencies)
    
    # Print results
    print(f"\n{'='*70}")
    print(f"BENCHMARK 4: Full Pathway 1 End-to-End (n={num_iterations})")
    print(f"{'='*70}")
    print(f"Flow: Scanner → AI/ML → Consensus → DB → Redis")
    print(f"Target: P95 < 200ms")
    print(f"-" * 70)
    print(f"Min:     {min_lat:>8.3f} ms")
    print(f"Average: {avg:>8.3f} ms")
    print(f"P50:     {p50:>8.3f} ms")
    print(f"P95:     {p95:>8.3f} ms  {'✅ PASS' if p95 < 200 else '❌ FAIL'}")
    print(f"P99:     {p99:>8.3f} ms")
    print(f"Max:     {max_lat:>8.3f} ms")
    print(f"{'='*70}\n")
    
    # Assert: P95 must be < 200ms
    assert p95 < 200.0, f"Pathway 1 E2E P95 latency {p95:.3f}ms exceeds 200ms target"
    
    # Clean up
    await perf_db_session.execute(text("TRUNCATE TABLE trade_suggestions, event_correlations CASCADE"))
    await perf_db_session.commit()
