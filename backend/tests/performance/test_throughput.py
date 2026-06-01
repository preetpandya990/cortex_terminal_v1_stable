"""Performance test: Throughput benchmarks for concurrent predictions."""
import asyncio
import time
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ml.inference.prediction_engine import PredictionEngine


@pytest.mark.asyncio
async def test_prediction_throughput(mock_redis: AsyncMock):
    """
    Performance Test 37.2: Concurrent prediction throughput
    
    Measures throughput under concurrent load.
    
    Validates:
    - Throughput > 50 requests/second
    - No errors under load
    - Requirements: 16.2
    """
    # Setup: Mock ONNX model
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
            
            # Execute: 100 concurrent requests
            num_requests = 100
            
            async def make_prediction(request_id: int):
                """Single prediction request."""
                try:
                    features = np.random.randn(1, 50).astype(np.float32)
                    result = await engine.predict(
                        features=features,
                        symbol="BTCUSDT",
                        timeframe="1h",
                        model_version="1.0.0"
                    )
                    return {"success": True, "request_id": request_id, "result": result}
                except Exception as e:
                    return {"success": False, "request_id": request_id, "error": str(e)}
            
            # Measure throughput
            start_time = time.perf_counter()
            
            # Run all requests concurrently
            tasks = [make_prediction(i) for i in range(num_requests)]
            results = await asyncio.gather(*tasks)
            
            end_time = time.perf_counter()
            duration = end_time - start_time
            
            # Calculate metrics
            successful = sum(1 for r in results if r["success"])
            failed = sum(1 for r in results if not r["success"])
            throughput = num_requests / duration
            
            print(f"\n{'='*60}")
            print(f"Throughput Benchmark Results")
            print(f"{'='*60}")
            print(f"Total requests:     {num_requests}")
            print(f"Successful:         {successful}")
            print(f"Failed:             {failed}")
            print(f"Duration:           {duration:.2f}s")
            print(f"Throughput:         {throughput:.2f} req/s")
            print(f"{'='*60}\n")
            
            # Assert: No errors
            assert failed == 0, f"{failed} requests failed under load"
            
            # Assert: Throughput > 50 req/s
            assert throughput > 50.0, f"Throughput {throughput:.2f} req/s is below 50 req/s threshold"
            
            # Assert: All requests successful
            assert successful == num_requests, f"Only {successful}/{num_requests} requests succeeded"


@pytest.mark.asyncio
async def test_sustained_throughput(mock_redis: AsyncMock):
    """
    Performance Test 37.2b: Sustained throughput over time
    
    Validates:
    - Consistent throughput over 5 seconds
    - No performance degradation
    - Requirements: 16.2
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
            
            # Execute: Sustained load for 5 seconds
            duration_seconds = 5
            request_count = 0
            errors = 0
            
            async def continuous_requests():
                """Make continuous requests."""
                nonlocal request_count, errors
                while True:
                    try:
                        features = np.random.randn(1, 50).astype(np.float32)
                        await engine.predict(
                            features=features,
                            symbol="BTCUSDT",
                            timeframe="1h",
                            model_version="1.0.0"
                        )
                        request_count += 1
                    except Exception:
                        errors += 1
            
            # Run for specified duration
            try:
                await asyncio.wait_for(continuous_requests(), timeout=duration_seconds)
            except asyncio.TimeoutError:
                pass  # Expected timeout
            
            throughput = request_count / duration_seconds
            
            print(f"\n{'='*60}")
            print(f"Sustained Throughput Test")
            print(f"{'='*60}")
            print(f"Duration:           {duration_seconds}s")
            print(f"Total requests:     {request_count}")
            print(f"Errors:             {errors}")
            print(f"Avg throughput:     {throughput:.2f} req/s")
            print(f"{'='*60}\n")
            
            # Assert: Sustained throughput > 50 req/s
            assert throughput > 50.0, f"Sustained throughput {throughput:.2f} req/s below threshold"
            
            # Assert: Error rate < 1%
            error_rate = (errors / request_count * 100) if request_count > 0 else 0
            assert error_rate < 1.0, f"Error rate {error_rate:.2f}% exceeds 1%"
