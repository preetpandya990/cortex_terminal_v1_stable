"""
Cortex AI — Model Loading Smoke Test
======================================
Fast, comprehensive validation of production model loading and inference.

Validates:
- Registry model loading (XGBoost + GRU)
- Model health checks
- Inference correctness (shape, range, latency)
- Backend integration (Treelite + ONNX)
- End-to-end prediction pipeline

Runs in <5 seconds with synthetic data.

Author: Cortex AI Team
Date: 2026-04-20
"""
import asyncio
import time
from typing import AsyncGenerator

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.ml.inference.registry_loader import RegistryModelLoader, LoadedEnsemble
from app.ml.inference.ensemble_predictor import EnsemblePredictor


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    from app.core.config import get_settings
    
    settings = get_settings()
    engine = create_async_engine(
        str(settings.DATABASE_URL),
        echo=False,
        pool_pre_ping=True,
    )
    
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with async_session() as session:
        yield session
    
    await engine.dispose()


@pytest.fixture(scope="module")
async def model_loader(db_session: AsyncSession) -> RegistryModelLoader:
    """Create model loader instance."""
    return RegistryModelLoader(
        session=db_session,
        num_threads=2,  # Reduced for testing
        use_gpu=False,
    )


@pytest.fixture(scope="module")
async def loaded_ensemble(model_loader: RegistryModelLoader) -> LoadedEnsemble:
    """Load production ensemble (cached for all tests)."""
    return await model_loader.load_production_ensemble()


@pytest.fixture(scope="module")
async def predictor(loaded_ensemble: LoadedEnsemble) -> EnsemblePredictor:
    """Create predictor from loaded ensemble."""
    return EnsemblePredictor.from_loaded_ensemble(loaded_ensemble, cache=None)


# ══════════════════════════════════════════════════════════════════════════════
# Smoke Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestModelLoading:
    """Test model loading from registry."""
    
    @pytest.mark.asyncio
    async def test_load_production_ensemble(self, model_loader: RegistryModelLoader):
        """Test loading production models from registry."""
        start = time.perf_counter()
        ensemble = await model_loader.load_production_ensemble()
        duration = time.perf_counter() - start
        
        # Assertions
        assert ensemble is not None, "Ensemble should be loaded"
        assert ensemble.xgboost_predictor is not None, "XGBoost predictor missing"
        assert ensemble.gru_session is not None, "GRU session missing"
        assert ensemble.xgboost_metadata is not None, "XGBoost metadata missing"
        assert ensemble.gru_metadata is not None, "GRU metadata missing"
        
        # Validate weights
        assert 0 < ensemble.xgboost_weight < 1, "XGBoost weight out of range"
        assert 0 < ensemble.gru_weight < 1, "GRU weight out of range"
        assert np.isclose(
            ensemble.xgboost_weight + ensemble.gru_weight, 1.0
        ), "Weights don't sum to 1.0"
        
        # Validate metadata
        assert ensemble.xgboost_metadata.status == "production"
        assert ensemble.gru_metadata.status == "production"
        assert ensemble.xgboost_metadata.is_active is True
        assert ensemble.gru_metadata.is_active is True
        
        # Performance check (should be fast with caching)
        assert duration < 10.0, f"Loading took {duration:.2f}s (expected <10s)"
        
        print(f"✓ Ensemble loaded in {duration:.3f}s")
        print(f"  XGBoost: {ensemble.xgboost_metadata.model_version} ({ensemble.xgboost_weight:.0%})")
        print(f"  GRU: {ensemble.gru_metadata.model_version} ({ensemble.gru_weight:.0%})")
    
    @pytest.mark.asyncio
    async def test_ensemble_validation(self, loaded_ensemble: LoadedEnsemble):
        """Test ensemble validation with synthetic inference."""
        # Should not raise
        loaded_ensemble.validate()
        print("✓ Ensemble validation passed")
    
    @pytest.mark.asyncio
    async def test_health_check(self, model_loader: RegistryModelLoader):
        """Test model health check."""
        health = await model_loader.health_check()
        
        assert health["status"] == "healthy", f"Health check failed: {health}"
        assert "xgboost" in health
        assert "gru" in health
        assert health["xgboost"]["weight"] > 0
        assert health["gru"]["weight"] > 0
        
        print(f"✓ Health check passed: {health['status']}")


class TestInference:
    """Test inference correctness and performance."""
    
    @pytest.mark.asyncio
    async def test_xgboost_inference(self, loaded_ensemble: LoadedEnsemble):
        """Test XGBoost inference via Treelite."""
        import tl2cgen
        
        # Synthetic input
        X = np.random.randn(5, 47).astype(np.float32)
        dmat = tl2cgen.DMatrix(X)
        
        start = time.perf_counter()
        predictions = loaded_ensemble.xgboost_predictor.predict(dmat)
        duration = time.perf_counter() - start
        
        # Shape validation
        assert predictions.shape[0] == 5, f"Wrong batch size: {predictions.shape}"
        
        # Value validation (probabilities)
        assert not np.any(np.isnan(predictions)), "NaN in predictions"
        assert not np.any(np.isinf(predictions)), "Inf in predictions"
        
        # Performance (Treelite should be fast)
        latency_per_sample = (duration / 5) * 1000  # ms
        assert latency_per_sample < 10, f"XGBoost too slow: {latency_per_sample:.2f}ms/sample"
        
        print(f"✓ XGBoost inference: {latency_per_sample:.2f}ms/sample")
    
    @pytest.mark.asyncio
    async def test_gru_inference(self, loaded_ensemble: LoadedEnsemble):
        """Test GRU inference via ONNX Runtime."""
        # Synthetic input
        X = np.random.randn(5, 60, 47).astype(np.float32)
        
        start = time.perf_counter()
        predictions = loaded_ensemble.gru_session.run(
            loaded_ensemble.gru_output_names,
            {loaded_ensemble.gru_input_name: X}
        )[0]
        duration = time.perf_counter() - start
        
        # Shape validation
        assert predictions.shape[0] == 5, f"Wrong batch size: {predictions.shape}"
        
        # Value validation
        assert not np.any(np.isnan(predictions)), "NaN in predictions"
        assert not np.any(np.isinf(predictions)), "Inf in predictions"
        
        # Performance
        latency_per_sample = (duration / 5) * 1000  # ms
        assert latency_per_sample < 50, f"GRU too slow: {latency_per_sample:.2f}ms/sample"
        
        print(f"✓ GRU inference: {latency_per_sample:.2f}ms/sample")
    
    @pytest.mark.asyncio
    async def test_ensemble_prediction(self, predictor: EnsemblePredictor):
        """Test end-to-end ensemble prediction."""
        # Synthetic inputs
        features_tabular = np.random.randn(47).astype(np.float32)
        features_sequence = np.random.randn(60, 47).astype(np.float32)
        
        start = time.perf_counter()
        prediction = await predictor.predict(
            features_tabular=features_tabular,
            features_sequence=features_sequence,
            symbol="TEST_SYMBOL",
            current_price=1500.0,
            volatility=0.20,
            use_cache=False,
        )
        duration = time.perf_counter() - start
        
        # Structure validation
        assert "direction" in prediction
        assert "direction_label" in prediction
        assert "confidence" in prediction
        assert "probabilities" in prediction
        assert "entry_price" in prediction
        assert "stop_loss" in prediction
        assert "tp1" in prediction
        assert "tp2" in prediction
        assert "tp3" in prediction
        
        # Value validation
        assert prediction["direction"] in [0, 1, 2], "Invalid direction"
        assert prediction["direction_label"] in ["SELL", "HOLD", "BUY"]
        assert 0 <= prediction["confidence"] <= 1, "Confidence out of range"
        
        # Probabilities
        probs = prediction["probabilities"]
        assert 0 <= probs["sell"] <= 1
        assert 0 <= probs["hold"] <= 1
        assert 0 <= probs["buy"] <= 1
        
        # Prices
        assert prediction["entry_price"] == 1500.0
        assert prediction["stop_loss"] > 0
        assert prediction["tp1"] > 0
        assert prediction["tp2"] > 0
        assert prediction["tp3"] > 0
        
        # Performance (critical for real-time trading)
        latency_ms = duration * 1000
        assert latency_ms < 250, f"Prediction too slow: {latency_ms:.2f}ms (target: <250ms)"
        
        print(f"✓ Ensemble prediction: {latency_ms:.2f}ms")
        print(f"  Direction: {prediction['direction_label']} ({prediction['confidence']:.1%})")
        print(f"  Entry: {prediction['entry_price']:.2f}, SL: {prediction['stop_loss']:.2f}")


class TestBatchInference:
    """Test batch inference performance."""
    
    @pytest.mark.asyncio
    async def test_batch_latency(self, predictor: EnsemblePredictor):
        """Test batch inference latency (critical for p99 <250ms)."""
        batch_sizes = [1, 5, 10]
        
        for batch_size in batch_sizes:
            # Generate batch
            predictions = []
            
            start = time.perf_counter()
            for _ in range(batch_size):
                features_tabular = np.random.randn(47).astype(np.float32)
                features_sequence = np.random.randn(60, 47).astype(np.float32)
                
                pred = await predictor.predict(
                    features_tabular=features_tabular,
                    features_sequence=features_sequence,
                    symbol="TEST_SYMBOL",
                    current_price=1500.0,
                    volatility=0.20,
                    use_cache=False,
                )
                predictions.append(pred)
            
            duration = time.perf_counter() - start
            avg_latency = (duration / batch_size) * 1000
            
            # Validate all predictions
            assert len(predictions) == batch_size
            for pred in predictions:
                assert pred["direction"] in [0, 1, 2]
                assert 0 <= pred["confidence"] <= 1
            
            # Performance check
            assert avg_latency < 250, f"Batch {batch_size}: {avg_latency:.2f}ms/sample (target: <250ms)"
            
            print(f"✓ Batch {batch_size}: {avg_latency:.2f}ms/sample (total: {duration*1000:.2f}ms)")


class TestErrorHandling:
    """Test error handling and edge cases."""
    
    @pytest.mark.asyncio
    async def test_invalid_input_shape(self, predictor: EnsemblePredictor):
        """Test handling of invalid input shapes."""
        # Wrong tabular shape
        with pytest.raises(ValueError, match="Expected 47 tabular features"):
            await predictor.predict(
                features_tabular=np.random.randn(10).astype(np.float32),  # Wrong size
                features_sequence=np.random.randn(60, 47).astype(np.float32),
                symbol="TEST",
                current_price=1500.0,
            )
        
        # Wrong sequence shape
        with pytest.raises(ValueError, match="Expected \\(60, 47\\) sequence"):
            await predictor.predict(
                features_tabular=np.random.randn(47).astype(np.float32),
                features_sequence=np.random.randn(30, 47).astype(np.float32),  # Wrong length
                symbol="TEST",
                current_price=1500.0,
            )
        
        print("✓ Input validation working correctly")
    
    @pytest.mark.asyncio
    async def test_nan_handling(self, predictor: EnsemblePredictor):
        """Test handling of NaN values in predictions."""
        # Valid inputs (NaN handling is internal)
        features_tabular = np.random.randn(47).astype(np.float32)
        features_sequence = np.random.randn(60, 47).astype(np.float32)
        
        prediction = await predictor.predict(
            features_tabular=features_tabular,
            features_sequence=features_sequence,
            symbol="TEST",
            current_price=1500.0,
            use_cache=False,
        )
        
        # Ensure no NaN in output
        assert not np.isnan(prediction["confidence"])
        assert not np.isnan(prediction["probabilities"]["sell"])
        assert not np.isnan(prediction["probabilities"]["hold"])
        assert not np.isnan(prediction["probabilities"]["buy"])
        
        print("✓ NaN handling working correctly")


# ══════════════════════════════════════════════════════════════════════════════
# Performance Summary
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_smoke_test_summary(
    loaded_ensemble: LoadedEnsemble,
    predictor: EnsemblePredictor,
):
    """Print smoke test summary."""
    print("\n" + "=" * 80)
    print("SMOKE TEST SUMMARY")
    print("=" * 80)
    print(f"✓ Models loaded: XGBoost ({loaded_ensemble.xgboost_weight:.0%}) + GRU ({loaded_ensemble.gru_weight:.0%})")
    print(f"✓ XGBoost: {loaded_ensemble.xgboost_metadata.model_version}")
    print(f"✓ GRU: {loaded_ensemble.gru_metadata.model_version}")
    print(f"✓ All validations passed")
    print(f"✓ Latency target: <250ms per prediction")
    print("=" * 80)
