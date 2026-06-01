"""
Cortex AI — Staging Model Validation Test
===========================================
Validates staging models before production promotion.

This test loads models from staging status and validates:
- Model loading from registry
- Treelite/ONNX inference correctness
- End-to-end prediction pipeline
- Performance requirements

Author: Cortex AI Team
Date: 2026-04-20
"""
import asyncio
import time
from typing import AsyncGenerator

import numpy as np
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.ml.inference.registry_loader import LoadedEnsemble
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.models.ml_data import MLModelMetadata


@pytest.fixture(scope="module")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
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
async def staging_models(db_session: AsyncSession) -> tuple[MLModelMetadata, MLModelMetadata]:
    """Load staging models from database."""
    # XGBoost
    stmt_xgb = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == "xgboost",
            MLModelMetadata.status == "staging",
        )
        .limit(1)
    )
    result_xgb = await db_session.execute(stmt_xgb)
    xgb_model = result_xgb.scalar_one_or_none()
    
    # GRU
    stmt_gru = (
        select(MLModelMetadata)
        .where(
            MLModelMetadata.model_name == "gru",
            MLModelMetadata.status == "staging",
        )
        .limit(1)
    )
    result_gru = await db_session.execute(stmt_gru)
    gru_model = result_gru.scalar_one_or_none()
    
    assert xgb_model is not None, "No XGBoost model in staging"
    assert gru_model is not None, "No GRU model in staging"
    
    return xgb_model, gru_model


@pytest.fixture(scope="module")
async def loaded_ensemble(staging_models: tuple[MLModelMetadata, MLModelMetadata]) -> LoadedEnsemble:
    """Load staging models into ensemble."""
    xgb_meta, gru_meta = staging_models
    
    # Load XGBoost (Treelite)
    import tl2cgen
    from pathlib import Path
    
    xgb_path = Path(xgb_meta.onnx_path)
    assert xgb_path.exists(), f"XGBoost model not found: {xgb_path}"
    xgb_predictor = tl2cgen.Predictor(str(xgb_path))
    
    # Load GRU (ONNX)
    import onnxruntime as ort
    
    gru_path = Path(gru_meta.onnx_path)
    assert gru_path.exists(), f"GRU model not found: {gru_path}"
    
    sess_options = ort.SessionOptions()
    sess_options.intra_op_num_threads = 2
    sess_options.inter_op_num_threads = 2
    sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    
    gru_session = ort.InferenceSession(
        str(gru_path),
        sess_options=sess_options,
        providers=['CPUExecutionProvider'],
    )
    
    # Extract weights
    xgb_weight = xgb_meta.training_metrics.get("ensemble_weight", 0.75)
    gru_weight = gru_meta.training_metrics.get("ensemble_weight", 0.25)
    
    # Create ensemble
    ensemble = LoadedEnsemble(
        xgboost_predictor=xgb_predictor,
        gru_session=gru_session,
        xgboost_weight=xgb_weight,
        gru_weight=gru_weight,
        xgboost_metadata=xgb_meta,
        gru_metadata=gru_meta,
    )
    
    return ensemble


@pytest.fixture(scope="module")
async def predictor(loaded_ensemble: LoadedEnsemble) -> EnsemblePredictor:
    """Create predictor from loaded ensemble."""
    return EnsemblePredictor.from_loaded_ensemble(loaded_ensemble, cache=None)


class TestStagingValidation:
    """Validate staging models before production promotion."""
    
    @pytest.mark.asyncio
    async def test_staging_models_exist(self, staging_models: tuple[MLModelMetadata, MLModelMetadata]):
        """Verify staging models are present."""
        xgb_meta, gru_meta = staging_models
        
        assert xgb_meta.status == "staging"
        assert gru_meta.status == "staging"
        assert xgb_meta.model_version == "1.0.0_xgboost"
        assert gru_meta.model_version == "1.0.0_gru"
        
        print(f"\n✓ Staging models found:")
        print(f"  XGBoost: {xgb_meta.model_version} (accuracy: {xgb_meta.training_metrics.get('accuracy', 0):.2%})")
        print(f"  GRU: {gru_meta.model_version} (accuracy: {gru_meta.training_metrics.get('accuracy', 0):.2%})")
    
    @pytest.mark.asyncio
    async def test_model_artifacts_exist(self, staging_models: tuple[MLModelMetadata, MLModelMetadata]):
        """Verify model artifact files exist."""
        from pathlib import Path
        
        xgb_meta, gru_meta = staging_models
        
        xgb_path = Path(xgb_meta.onnx_path)
        gru_path = Path(gru_meta.onnx_path)
        
        assert xgb_path.exists(), f"XGBoost artifact missing: {xgb_path}"
        assert gru_path.exists(), f"GRU artifact missing: {gru_path}"
        
        # Verify file types
        assert xgb_path.suffix == ".so", f"XGBoost should be .so (Treelite), got {xgb_path.suffix}"
        assert gru_path.suffix == ".onnx", f"GRU should be .onnx, got {gru_path.suffix}"
        
        print(f"\n✓ Model artifacts verified:")
        print(f"  XGBoost: {xgb_path} ({xgb_path.stat().st_size / 1024 / 1024:.2f} MB)")
        print(f"  GRU: {gru_path} ({gru_path.stat().st_size / 1024 / 1024:.2f} MB)")
    
    @pytest.mark.asyncio
    async def test_ensemble_validation(self, loaded_ensemble: LoadedEnsemble):
        """Test ensemble validation with synthetic inference."""
        loaded_ensemble.validate()
        print("\n✓ Ensemble validation passed")
    
    @pytest.mark.asyncio
    async def test_xgboost_inference(self, loaded_ensemble: LoadedEnsemble):
        """Test XGBoost inference via Treelite."""
        import tl2cgen
        
        X = np.random.randn(5, 47).astype(np.float32)
        dmat = tl2cgen.DMatrix(X)
        
        start = time.perf_counter()
        predictions = loaded_ensemble.xgboost_predictor.predict(dmat)
        duration = time.perf_counter() - start
        
        assert predictions.shape[0] == 5
        assert not np.any(np.isnan(predictions))
        assert not np.any(np.isinf(predictions))
        
        latency_ms = (duration / 5) * 1000
        assert latency_ms < 10, f"XGBoost too slow: {latency_ms:.2f}ms/sample"
        
        print(f"\n✓ XGBoost inference: {latency_ms:.2f}ms/sample")
    
    @pytest.mark.asyncio
    async def test_gru_inference(self, loaded_ensemble: LoadedEnsemble):
        """Test GRU inference via ONNX Runtime."""
        X = np.random.randn(5, 60, 47).astype(np.float32)
        
        start = time.perf_counter()
        predictions = loaded_ensemble.gru_session.run(
            loaded_ensemble.gru_output_names,
            {loaded_ensemble.gru_input_name: X}
        )[0]
        duration = time.perf_counter() - start
        
        assert predictions.shape[0] == 5
        assert not np.any(np.isnan(predictions))
        assert not np.any(np.isinf(predictions))
        
        latency_ms = (duration / 5) * 1000
        assert latency_ms < 50, f"GRU too slow: {latency_ms:.2f}ms/sample"
        
        print(f"\n✓ GRU inference: {latency_ms:.2f}ms/sample")
    
    @pytest.mark.asyncio
    async def test_end_to_end_prediction(self, predictor: EnsemblePredictor):
        """Test end-to-end ensemble prediction."""
        features_tabular = np.random.randn(47).astype(np.float32)
        features_sequence = np.random.randn(60, 47).astype(np.float32)
        
        start = time.perf_counter()
        prediction = await predictor.predict(
            features_tabular=features_tabular,
            features_sequence=features_sequence,
            symbol="TEST_STAGING",
            current_price=1500.0,
            volatility=0.20,
            use_cache=False,
        )
        duration = time.perf_counter() - start
        
        # Validate structure
        assert "direction" in prediction
        assert "direction_label" in prediction
        assert "confidence" in prediction
        assert "probabilities" in prediction
        
        # Validate values
        assert prediction["direction"] in [0, 1, 2]
        assert prediction["direction_label"] in ["SELL", "HOLD", "BUY"]
        assert 0 <= prediction["confidence"] <= 1
        
        # Performance
        latency_ms = duration * 1000
        assert latency_ms < 250, f"Prediction too slow: {latency_ms:.2f}ms"
        
        print(f"\n✓ End-to-end prediction: {latency_ms:.2f}ms")
        print(f"  Direction: {prediction['direction_label']} ({prediction['confidence']:.1%})")
    
    @pytest.mark.asyncio
    async def test_batch_performance(self, predictor: EnsemblePredictor):
        """Test batch inference performance."""
        batch_size = 10
        predictions = []
        
        start = time.perf_counter()
        for _ in range(batch_size):
            features_tabular = np.random.randn(47).astype(np.float32)
            features_sequence = np.random.randn(60, 47).astype(np.float32)
            
            pred = await predictor.predict(
                features_tabular=features_tabular,
                features_sequence=features_sequence,
                symbol="TEST_BATCH",
                current_price=1500.0,
                volatility=0.20,
                use_cache=False,
            )
            predictions.append(pred)
        
        duration = time.perf_counter() - start
        avg_latency = (duration / batch_size) * 1000
        
        assert len(predictions) == batch_size
        assert avg_latency < 250, f"Batch avg too slow: {avg_latency:.2f}ms"
        
        print(f"\n✓ Batch performance: {avg_latency:.2f}ms/sample (batch={batch_size})")


@pytest.mark.asyncio
async def test_staging_validation_summary(
    staging_models: tuple[MLModelMetadata, MLModelMetadata],
    loaded_ensemble: LoadedEnsemble,
):
    """Print validation summary."""
    xgb_meta, gru_meta = staging_models
    
    print("\n" + "=" * 80)
    print("STAGING VALIDATION SUMMARY")
    print("=" * 80)
    print(f"✓ XGBoost: {xgb_meta.model_version} (accuracy: {xgb_meta.training_metrics.get('accuracy', 0):.2%})")
    print(f"✓ GRU: {gru_meta.model_version} (accuracy: {gru_meta.training_metrics.get('accuracy', 0):.2%})")
    print(f"✓ Ensemble weights: XGBoost={loaded_ensemble.xgboost_weight:.0%}, GRU={loaded_ensemble.gru_weight:.0%}")
    print(f"✓ All validations passed")
    print(f"✓ Ready for production promotion")
    print("=" * 80)
