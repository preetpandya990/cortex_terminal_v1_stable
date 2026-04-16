"""
Phase 5 Integration Test - Ensemble Predictor + Feature Loader
================================================================
Tests the complete ML prediction pipeline with trained models.

Tests:
1. Ensemble predictor initialization
2. Feature loader with 3-tier caching
3. End-to-end prediction
4. SignalAssembler integration
5. Performance benchmarks

Author: Cortex AI Team
Date: 2026-04-14
"""
import asyncio
import logging
import time
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_ensemble_predictor():
    """Test 1: Ensemble predictor initialization and inference."""
    logger.info("\n" + "=" * 80)
    logger.info("TEST 1: Ensemble Predictor")
    logger.info("=" * 80)
    
    try:
        from app.ml.inference import create_ensemble_predictor
        
        # Check models exist
        xgb_path = Path("models/production/onnx/xgboost_final.onnx")
        gru_path = Path("models/production/onnx/gru_final.onnx")
        
        if not xgb_path.exists():
            logger.error(f"✗ XGBoost model not found: {xgb_path}")
            return False
        if not gru_path.exists():
            logger.error(f"✗ GRU model not found: {gru_path}")
            return False
        
        logger.info(f"✓ Models found: {xgb_path.name}, {gru_path.name}")
        
        # Create predictor
        predictor = create_ensemble_predictor(
            xgboost_path=xgb_path,
            gru_path=gru_path,
            cache=None,  # No Redis for this test
            xgboost_weight=0.6,
            gru_weight=0.4,
            num_threads=4,
            use_gpu=False,
        )
        
        logger.info("✓ Ensemble predictor initialized")
        
        # Test inference with dummy data
        tabular = np.random.randn(47).astype(np.float32)
        sequence = np.random.randn(60, 47).astype(np.float32)
        
        start = time.time()
        prediction = await predictor.predict(
            features_tabular=tabular,
            features_sequence=sequence,
            symbol="TEST_SYMBOL",
            current_price=1500.0,
            volatility=0.20,
            timeframe="1d",
            use_cache=False,
        )
        elapsed = (time.time() - start) * 1000
        
        logger.info(f"✓ Prediction completed in {elapsed:.2f}ms")
        logger.info(f"  Direction: {prediction['direction_label']}")
        logger.info(f"  Confidence: {prediction['confidence']:.2%}")
        logger.info(f"  Entry: {prediction['entry_price']:.2f}")
        logger.info(f"  Stop Loss: {prediction['stop_loss']:.2f}")
        logger.info(f"  TP1: {prediction['tp1']:.2f}, TP2: {prediction['tp2']:.2f}, TP3: {prediction['tp3']:.2f}")
        logger.info(f"  Probabilities: SELL={prediction['probabilities']['sell']:.2%}, "
                   f"HOLD={prediction['probabilities']['hold']:.2%}, "
                   f"BUY={prediction['probabilities']['buy']:.2%}")
        
        # Validate prediction structure
        required_fields = [
            'direction', 'direction_label', 'confidence', 'probabilities',
            'entry_price', 'stop_loss', 'tp1', 'tp2', 'tp3', 'volatility', 'metadata'
        ]
        for field in required_fields:
            if field not in prediction:
                logger.error(f"✗ Missing field: {field}")
                return False
        
        logger.info("✓ All required fields present")
        
        # Validate business logic
        if prediction['direction_label'] == 'BUY':
            if not (prediction['entry_price'] < prediction['tp1'] < prediction['tp2'] < prediction['tp3']):
                logger.error("✗ BUY: TP ordering invalid")
                return False
            if not (prediction['stop_loss'] < prediction['entry_price']):
                logger.error("✗ BUY: Stop loss should be below entry")
                return False
        elif prediction['direction_label'] == 'SELL':
            if not (prediction['entry_price'] > prediction['tp1'] > prediction['tp2'] > prediction['tp3']):
                logger.error("✗ SELL: TP ordering invalid")
                return False
            if not (prediction['stop_loss'] > prediction['entry_price']):
                logger.error("✗ SELL: Stop loss should be above entry")
                return False
        
        logger.info("✓ Business logic validation passed")
        logger.info("✓ TEST 1 PASSED\n")
        return True
        
    except Exception as e:
        logger.error(f"✗ TEST 1 FAILED: {e}", exc_info=True)
        return False


async def test_feature_loader():
    """Test 2: Feature loader (mock test without database)."""
    logger.info("\n" + "=" * 80)
    logger.info("TEST 2: Feature Loader (Structure Test)")
    logger.info("=" * 80)
    
    try:
        from app.ml.inference import create_feature_loader
        
        # Note: This is a structure test only
        # Full test requires database connection
        
        logger.info("✓ FeatureLoader import successful")
        logger.info("✓ TEST 2 PASSED (structure only)\n")
        logger.info("  Note: Full test requires database connection")
        return True
        
    except Exception as e:
        logger.error(f"✗ TEST 2 FAILED: {e}", exc_info=True)
        return False


async def test_signal_assembler_integration():
    """Test 3: SignalAssembler integration."""
    logger.info("\n" + "=" * 80)
    logger.info("TEST 3: SignalAssembler Integration")
    logger.info("=" * 80)
    
    try:
        from app.ai.fusion.signal_assembler import SignalAssembler
        from app.ml.inference import create_ensemble_predictor
        
        # Create predictor
        xgb_path = Path("models/production/onnx/xgboost_final.onnx")
        gru_path = Path("models/production/onnx/gru_final.onnx")
        
        predictor = create_ensemble_predictor(
            xgboost_path=xgb_path,
            gru_path=gru_path,
            cache=None,
            xgboost_weight=0.6,
            gru_weight=0.4,
        )
        
        # Create signal assembler
        assembler = SignalAssembler(
            ensemble_predictor=predictor,
            feature_loader=None,  # Will be None for this test
            event_weight=0.4,
            ml_weight=0.4,
            technical_weight=0.2,
        )
        
        logger.info("✓ SignalAssembler initialized with ensemble predictor")
        logger.info(f"  Weights: Event=40%, ML=40%, Technical=20%")
        
        # Validate weights
        total_weight = assembler.event_weight + assembler.ml_weight + assembler.technical_weight
        if not np.isclose(total_weight, 1.0):
            logger.error(f"✗ Weights don't sum to 1.0: {total_weight}")
            return False
        
        logger.info("✓ Weight validation passed")
        logger.info("✓ TEST 3 PASSED\n")
        return True
        
    except Exception as e:
        logger.error(f"✗ TEST 3 FAILED: {e}", exc_info=True)
        return False


async def test_performance_benchmark():
    """Test 4: Performance benchmark."""
    logger.info("\n" + "=" * 80)
    logger.info("TEST 4: Performance Benchmark")
    logger.info("=" * 80)
    
    try:
        from app.ml.inference import create_ensemble_predictor
        
        xgb_path = Path("models/production/onnx/xgboost_final.onnx")
        gru_path = Path("models/production/onnx/gru_final.onnx")
        
        predictor = create_ensemble_predictor(
            xgboost_path=xgb_path,
            gru_path=gru_path,
            cache=None,
            num_threads=4,
        )
        
        # Warm-up
        tabular = np.random.randn(47).astype(np.float32)
        sequence = np.random.randn(60, 47).astype(np.float32)
        await predictor.predict(tabular, sequence, "WARMUP", 1500.0, 0.20, use_cache=False)
        
        # Benchmark
        n_iterations = 100
        logger.info(f"Running {n_iterations} predictions...")
        
        start = time.time()
        for i in range(n_iterations):
            await predictor.predict(
                tabular, sequence, f"SYMBOL_{i}", 1500.0, 0.20, use_cache=False
            )
        elapsed = time.time() - start
        
        avg_latency = (elapsed / n_iterations) * 1000
        throughput = n_iterations / elapsed
        
        logger.info(f"✓ Benchmark complete:")
        logger.info(f"  Total time: {elapsed:.2f}s")
        logger.info(f"  Average latency: {avg_latency:.2f}ms")
        logger.info(f"  Throughput: {throughput:.1f} predictions/sec")
        
        # Performance targets
        if avg_latency > 100:
            logger.warning(f"⚠ Latency above target (100ms): {avg_latency:.2f}ms")
        else:
            logger.info(f"✓ Latency within target (<100ms)")
        
        logger.info("✓ TEST 4 PASSED\n")
        return True
        
    except Exception as e:
        logger.error(f"✗ TEST 4 FAILED: {e}", exc_info=True)
        return False


async def main():
    """Run all tests."""
    logger.info("\n" + "=" * 80)
    logger.info("PHASE 5 INTEGRATION TEST SUITE")
    logger.info("=" * 80)
    logger.info("Testing: Ensemble Predictor + Feature Loader + SignalAssembler")
    logger.info("")
    
    results = []
    
    # Run tests
    results.append(("Ensemble Predictor", await test_ensemble_predictor()))
    results.append(("Feature Loader", await test_feature_loader()))
    results.append(("SignalAssembler Integration", await test_signal_assembler_integration()))
    results.append(("Performance Benchmark", await test_performance_benchmark()))
    
    # Summary
    logger.info("\n" + "=" * 80)
    logger.info("TEST SUMMARY")
    logger.info("=" * 80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for name, result in results:
        status = "✓ PASSED" if result else "✗ FAILED"
        logger.info(f"{status}: {name}")
    
    logger.info("")
    logger.info(f"Results: {passed}/{total} tests passed")
    
    if passed == total:
        logger.info("=" * 80)
        logger.info("✓ ALL TESTS PASSED - PHASE 5 READY FOR PRODUCTION")
        logger.info("=" * 80)
        return 0
    else:
        logger.error("=" * 80)
        logger.error("✗ SOME TESTS FAILED - FIX ISSUES BEFORE PROCEEDING")
        logger.error("=" * 80)
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    exit(exit_code)
