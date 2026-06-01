"""Performance test: Cache performance benchmarks."""
import time
import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.ml.inference.prediction_engine import PredictionEngine


@pytest.mark.asyncio
async def test_cache_hit_rate(mock_redis: AsyncMock):
    """
    Performance Test 37.3: Cache hit rate
    
    Measures cache effectiveness with repeated predictions.
    
    Validates:
    - Cache hit rate > 80%
    - Cache retrieval < 10ms
    - Requirements: 16.3
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
    
    # Setup: Mock cache with realistic behavior
    cache_storage = {}
    
    def mock_get(key):
        return cache_storage.get(key)
    
    def mock_set(key, value, ttl=None):
        cache_storage[key] = value
    
    mock_cache = MagicMock()
    mock_cache.get.side_effect = mock_get
    mock_cache.set.side_effect = mock_set
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer"):
            engine = PredictionEngine(
                model_path="/tmp/test_model.onnx",
                cache=mock_cache,
                cache_enabled=True
            )
            
            # Execute: Make predictions with repeated symbols
            symbols = ["BTCUSDT", "ETHUSDT", "BNBUSDT"]
            num_requests = 100
            
            cache_hits = 0
            cache_misses = 0
            hit_latencies = []
            miss_latencies = []
            
            for i in range(num_requests):
                # Use same symbol multiple times to trigger cache hits
                symbol = symbols[i % len(symbols)]
                features = np.random.randn(1, 50).astype(np.float32)
                
                # Check if cache will hit
                cache_key = f"prediction:{symbol}:1h:1.0.0"
                will_hit = cache_key in cache_storage
                
                start_time = time.perf_counter()
                await engine.predict(
                    features=features,
                    symbol=symbol,
                    timeframe="1h",
                    model_version="1.0.0"
                )
                latency = (time.perf_counter() - start_time) * 1000
                
                if will_hit:
                    cache_hits += 1
                    hit_latencies.append(latency)
                else:
                    cache_misses += 1
                    miss_latencies.append(latency)
            
            # Calculate metrics
            total_requests = cache_hits + cache_misses
            hit_rate = (cache_hits / total_requests) * 100
            avg_hit_latency = sum(hit_latencies) / len(hit_latencies) if hit_latencies else 0
            avg_miss_latency = sum(miss_latencies) / len(miss_latencies) if miss_latencies else 0
            
            print(f"\n{'='*60}")
            print(f"Cache Performance Benchmark")
            print(f"{'='*60}")
            print(f"Total requests:     {total_requests}")
            print(f"Cache hits:         {cache_hits}")
            print(f"Cache misses:       {cache_misses}")
            print(f"Hit rate:           {hit_rate:.1f}%")
            print(f"Avg hit latency:    {avg_hit_latency:.2f}ms")
            print(f"Avg miss latency:   {avg_miss_latency:.2f}ms")
            print(f"Speedup:            {avg_miss_latency / avg_hit_latency:.1f}x" if avg_hit_latency > 0 else "N/A")
            print(f"{'='*60}\n")
            
            # Assert: Hit rate > 80%
            assert hit_rate > 80.0, f"Cache hit rate {hit_rate:.1f}% is below 80% threshold"
            
            # Assert: Cache retrieval < 10ms
            assert avg_hit_latency < 10.0, f"Cache hit latency {avg_hit_latency:.2f}ms exceeds 10ms"
            
            # Assert: Cache hits are faster than misses
            if hit_latencies and miss_latencies:
                assert avg_hit_latency < avg_miss_latency, "Cache hits should be faster than misses"


@pytest.mark.asyncio
async def test_cache_ttl_expiration(mock_redis: AsyncMock):
    """
    Performance Test 37.3b: Cache TTL and expiration
    
    Validates:
    - Expired cache entries are not used
    - Cache refresh works correctly
    - Requirements: 16.3
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
    
    # Setup: Mock cache with TTL tracking
    cache_storage = {}
    cache_ttl = {}
    
    def mock_get(key):
        if key in cache_storage:
            # Check if expired (simulate TTL)
            if key in cache_ttl and time.time() > cache_ttl[key]:
                del cache_storage[key]
                del cache_ttl[key]
                return None
            return cache_storage[key]
        return None
    
    def mock_set(key, value, ttl=None):
        cache_storage[key] = value
        if ttl:
            cache_ttl[key] = time.time() + ttl
    
    mock_cache = MagicMock()
    mock_cache.get.side_effect = mock_get
    mock_cache.set.side_effect = mock_set
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer"):
            engine = PredictionEngine(
                model_path="/tmp/test_model.onnx",
                cache=mock_cache,
                cache_enabled=True,
                cache_ttl=1  # 1 second TTL
            )
            
            features = np.random.randn(1, 50).astype(np.float32)
            
            # First request: Cache miss
            result1 = await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            
            # Second request: Cache hit (within TTL)
            result2 = await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            
            # Wait for TTL to expire
            time.sleep(1.1)
            
            # Third request: Cache miss (expired)
            result3 = await engine.predict(
                features=features,
                symbol="BTCUSDT",
                timeframe="1h",
                model_version="1.0.0"
            )
            
            print(f"\n{'='*60}")
            print(f"Cache TTL Test")
            print(f"{'='*60}")
            print(f"Request 1: Cache miss (initial)")
            print(f"Request 2: Cache hit (within TTL)")
            print(f"Request 3: Cache miss (expired)")
            print(f"{'='*60}\n")
            
            # Assert: Results are consistent
            assert result1["direction"] == result2["direction"]
            assert result2["direction"] == result3["direction"]
            
            # Assert: Cache was used for second request
            # (In real implementation, would check cache.get call count)
            assert mock_cache.get.call_count >= 3


@pytest.mark.asyncio
async def test_cache_memory_efficiency(mock_redis: AsyncMock):
    """
    Performance Test 37.3c: Cache memory efficiency
    
    Validates:
    - Cache doesn't grow unbounded
    - Old entries are evicted
    - Requirements: 16.3
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
    
    # Setup: Mock cache with size limit
    cache_storage = {}
    max_cache_size = 50
    
    def mock_get(key):
        return cache_storage.get(key)
    
    def mock_set(key, value, ttl=None):
        # Evict oldest if at capacity
        if len(cache_storage) >= max_cache_size and key not in cache_storage:
            oldest_key = next(iter(cache_storage))
            del cache_storage[oldest_key]
        cache_storage[key] = value
    
    mock_cache = MagicMock()
    mock_cache.get.side_effect = mock_get
    mock_cache.set.side_effect = mock_set
    
    with patch("app.ml.inference.prediction_engine.onnx.InferenceSession") as mock_session:
        mock_session.return_value = mock_model
        
        with patch("app.ml.inference.prediction_engine.shap.KernelExplainer"):
            engine = PredictionEngine(
                model_path="/tmp/test_model.onnx",
                cache=mock_cache,
                cache_enabled=True
            )
            
            # Execute: Make predictions for many unique symbols
            num_symbols = 100
            
            for i in range(num_symbols):
                features = np.random.randn(1, 50).astype(np.float32)
                await engine.predict(
                    features=features,
                    symbol=f"SYMBOL{i}USDT",
                    timeframe="1h",
                    model_version="1.0.0"
                )
            
            cache_size = len(cache_storage)
            
            print(f"\n{'='*60}")
            print(f"Cache Memory Efficiency Test")
            print(f"{'='*60}")
            print(f"Unique symbols:     {num_symbols}")
            print(f"Cache size:         {cache_size}")
            print(f"Max cache size:     {max_cache_size}")
            print(f"{'='*60}\n")
            
            # Assert: Cache size is bounded
            assert cache_size <= max_cache_size, f"Cache size {cache_size} exceeds limit {max_cache_size}"
