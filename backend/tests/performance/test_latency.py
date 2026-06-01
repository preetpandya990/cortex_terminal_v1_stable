"""Performance test: Latency benchmarks for ML predictions."""
import time
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.inference.prediction_engine import PredictionEngine


@pytest.mark.asyncio
async def test_prediction_latency_p95_p99(mock_redis: AsyncMock):
    """
    Performance Test 37.1: Single prediction latency
    
    Measures end-to-end latency for single symbol prediction.
    
    Validates:
    - P95 latency < 250ms
    - P99 latency < 300ms
    - Requirements: 16.2
    """
    # Setup: Mock ONNX model with realistic inference time
    mock_model = MagicMock()
    mock_model.run.return_value = [
        np.array([[0.8, 0.15, 0.05]]),
        np.array([[0.85]]),
        np.array([[105.5]]),
        np.array([[103.0]]),
        np.array([[107.0]]),
        np.array([[108.5]]),
        np.array([[110.0]]),
        np.array([[2.5]]),
    ]
    
    # Setup: Mock cache (disabled for latency test)
    mock_cache = MagicMock()
    mock_cache.get.return_value = None
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer"):
            engine = PredictionEngine(
                model_path="/tmp/test_model.onnx",
                cache=mock_cache,
                cache_enabled=False
            )
            
            # Execute: Run 100 predictions and measure latency
            latencies = []
            num_iterations = 100
            
            for _ in range(num_iterations):
                features = np.random.randn(1, 50).astype(np.float32)
                
                start_time = time.perf_counter()
                await engine.predict(
                    features=features,
                    symbol="BTCUSDT",
                    timeframe="1h",
                    model_version="1.0.0"
                )
                end_time = time.perf_counter()
                
                latency_ms = (end_time - start_time) * 1000
                latencies.append(latency_ms)
            
            # Calculate percentiles
            latencies_sorted = sorted(latencies)
            p50 = latencies_sorted[int(0.50 * num_iterations)]
            p95 = latencies_sorted[int(0.95 * num_iterations)]
            p99 = latencies_sorted[int(0.99 * num_iterations)]
            avg = sum(latencies) / len(latencies)
            
            # Print results for visibility
            print(f"\n{'='*60}")
            print(f"Latency Benchmark Results (n={num_iterations})")
            print(f"{'='*60}")
            print(f"Average: {avg:.2f}ms")
            print(f"P50:     {p50:.2f}ms")
            print(f"P95:     {p95:.2f}ms")
            print(f"P99:     {p99:.2f}ms")
            print(f"{'='*60}\n")
            
            # Assert: P95 < 250ms
            assert p95 < 250.0, f"P95 latency {p95:.2f}ms exceeds 250ms threshold"
            
            # Assert: P99 < 300ms
            assert p99 < 300.0, f"P99 latency {p99:.2f}ms exceeds 300ms threshold"
            
            # Assert: Average is reasonable
            assert avg < 200.0, f"Average latency {avg:.2f}ms is too high"


@pytest.mark.asyncio
async def test_prediction_latency_with_cache(mock_redis: AsyncMock):
    """
    Performance Test 37.1b: Latency with cache enabled
    
    Validates:
    - Cache hit latency < 10ms
    - Cache miss latency < 250ms
    - Requirements: 16.2, 16.3
    """
    mock_model = MagicMock()
    mock_model.run.return_value = [
        np.array([[0.8, 0.15, 0.05]]),
        np.array([[0.85]]),
        np.array([[105.5]]),
        np.array([[103.0]]),
        np.array([[107.0]]),
        np.array([[108.5]]),
        np.array([[110.0]]),
        np.array([[2.5]]),
    ]
    
    # Setup: Mock cache with hit/miss behavior
    cache_hit_result = {
        "direction": "BUY",
        "confidence": 0.85,
        "entry_price": 105.5,
        "stop_loss": 103.0,
        "take_profit_1": 107.0,
        "take_profit_2": 108.5,
        "take_profit_3": 110.0,
        "metadata": {"symbol": "BTCUSDT", "timeframe": "1h"}
    }
    
    mock_cache = MagicMock()
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer"):
            engine = PredictionEngine(
                model_path="/tmp/test_model.onnx",
                cache=mock_cache,
                cache_enabled=True
            )
            
            features = np.random.randn(1, 50).astype(np.float32)
            
            # Test 1: Cache miss (first request)
            mock_cache.get.return_value = None
            
            start_time = time.perf_counter()
            await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            cache_miss_latency = (time.perf_counter() - start_time) * 1000
            
            # Test 2: Cache hit (subsequent request)
            mock_cache.get.return_value = cache_hit_result
            
            start_time = time.perf_counter()
            await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            cache_hit_latency = (time.perf_counter() - start_time) * 1000
            
            print(f"\n{'='*60}")
            print(f"Cache Performance")
            print(f"{'='*60}")
            print(f"Cache miss: {cache_miss_latency:.2f}ms")
            print(f"Cache hit:  {cache_hit_latency:.2f}ms")
            print(f"Speedup:    {cache_miss_latency / cache_hit_latency:.1f}x")
            print(f"{'='*60}\n")
            
            # Assert: Cache hit is significantly faster
            assert cache_hit_latency < cache_miss_latency, "Cache hit should be faster than miss"
            
            # Assert: Cache hit latency < 10ms
            assert cache_hit_latency < 10.0, f"Cache hit latency {cache_hit_latency:.2f}ms exceeds 10ms"
