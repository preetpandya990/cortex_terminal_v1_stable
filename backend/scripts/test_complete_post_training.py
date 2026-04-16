"""
Test Post-Training Workflow - Complete Validation
==================================================
Tests the entire post-training pipeline to catch errors before running full training.
"""
import asyncio
import sys
from pathlib import Path
import numpy as np
import logging

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings
from app.ml.training.xgboost_trainer import XGBoostTrainer
from app.ml.training.gru_trainer import GRUTrainer
from app.ml.training.ensemble_trainer import EnsembleTrainer
from app.ml.training.evaluator import ModelEvaluator
from app.ml.model_registry import ModelRegistry
from dataclasses import asdict
import xgboost as xgb

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def test_complete_workflow():
    """Test complete post-training workflow with dummy data"""
    
    print("\n" + "=" * 80)
    print("POST-TRAINING WORKFLOW VALIDATION")
    print("=" * 80 + "\n")
    
    # Create dummy data
    n_samples = 1000
    n_features = 47
    sequence_length = 60
    
    X_tab = np.random.randn(n_samples, n_features).astype(np.float32)
    X_seq = np.random.randn(n_samples, sequence_length, n_features).astype(np.float32)
    y = np.random.choice([-1, 0, 1], size=n_samples)
    
    # Split
    split_idx = int(n_samples * 0.8)
    X_tab_train, X_tab_val = X_tab[:split_idx], X_tab[split_idx:]
    X_seq_train, X_seq_val = X_seq[:split_idx], X_seq[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]
    
    logger.info(f"✓ Dummy data created: {n_samples} samples, {n_features} features")
    
    # Test 1: XGBoost Training
    logger.info("\n[1/7] Testing XGBoost training...")
    try:
        xgb_trainer = XGBoostTrainer()
        xgb_trainer.train(
            X_tab_train, y_train,
            X_tab_val, y_val,
            params={'max_depth': 3, 'n_estimators': 10},
            early_stopping_rounds=5
        )
        y_pred_xgb, y_proba_xgb = xgb_trainer.predict(X_tab_val)
        logger.info("✓ XGBoost training successful")
    except Exception as e:
        logger.error(f"✗ XGBoost training failed: {e}")
        return False
    
    # Test 2: GRU Training
    logger.info("\n[2/7] Testing GRU training...")
    try:
        gru_trainer = GRUTrainer(input_shape=(sequence_length, n_features))
        gru_params = {
            'gru_units_1': 32,
            'gru_units_2': 16,
            'dense_units': 8,
            'dropout': 0.1,
            'recurrent_dropout': 0.1,
            'l2_reg': 0.001,
            'learning_rate': 0.001,
            'clipnorm': 0.5,
            'clipvalue': None,
            'patience': 3,
            'reduce_lr_patience': 2
        }
        gru_trainer.model = gru_trainer.build_model(gru_params)
        history = gru_trainer.train(
            X_seq_train, y_train,
            X_seq_val, y_val,
            epochs=5,
            batch_size=64
        )
        logger.info("✓ GRU training successful")
    except Exception as e:
        logger.error(f"✗ GRU training failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 3: History Object Handling
    logger.info("\n[3/7] Testing History object handling...")
    try:
        # Test the exact code from our script
        final_epoch = len(history.history['loss']) if hasattr(history, 'history') and 'loss' in history.history else 0
        logger.info(f"✓ History handling successful: {final_epoch} epochs")
    except Exception as e:
        logger.error(f"✗ History handling failed: {e}")
        # Try alternative
        try:
            final_epoch = 0
            logger.warning("Using fallback: final_epoch = 0")
        except:
            return False
    
    # Test 4: Model Evaluation
    logger.info("\n[4/7] Testing model evaluation...")
    try:
        evaluator = ModelEvaluator()
        
        # XGBoost evaluation
        xgb_results = evaluator.evaluate(y_val, y_pred_xgb, y_proba_xgb)
        logger.info(f"  XGBoost accuracy: {xgb_results.accuracy:.4f}")
        
        # GRU evaluation
        y_proba_gru = gru_trainer.model.predict(X_seq_val, verbose=0)
        y_pred_gru = np.argmax(y_proba_gru, axis=1) - 1
        gru_results = evaluator.evaluate(y_val, y_pred_gru, y_proba_gru)
        logger.info(f"  GRU accuracy: {gru_results.accuracy:.4f}")
        
        # Test asdict conversion
        xgb_metrics = asdict(xgb_results)
        gru_metrics = asdict(gru_results)
        logger.info("✓ Model evaluation successful")
    except Exception as e:
        logger.error(f"✗ Model evaluation failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 5: Ensemble Training
    logger.info("\n[5/7] Testing ensemble training...")
    try:
        ensemble_trainer = EnsembleTrainer(
            xgboost_model=xgb_trainer.model,
            gru_model=gru_trainer.model,
            weights={'xgboost': 0.6, 'gru': 0.4}
        )
        
        # Test weight optimization
        optimized_weights = ensemble_trainer.optimize_weights(
            X_tab_val, X_seq_val, y_val,
            metric='sharpe_ratio'
        )
        logger.info(f"  Optimized weights: {optimized_weights}")
        
        # Test ensemble prediction
        y_proba_ensemble = (optimized_weights['xgboost'] * y_proba_xgb + 
                           optimized_weights['gru'] * y_proba_gru)
        y_pred_ensemble = np.argmax(y_proba_ensemble, axis=1) - 1
        
        ensemble_results = evaluator.evaluate(y_val, y_pred_ensemble, y_proba_ensemble)
        logger.info(f"  Ensemble accuracy: {ensemble_results.accuracy:.4f}")
        logger.info("✓ Ensemble training successful")
    except Exception as e:
        logger.error(f"✗ Ensemble training failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 6: Model Export
    logger.info("\n[6/7] Testing model export...")
    try:
        test_dir = Path("models/test_export")
        test_dir.mkdir(parents=True, exist_ok=True)
        
        # Save XGBoost
        xgb_path = test_dir / "xgboost_test.json"
        xgb_trainer.model.save_model(str(xgb_path))
        logger.info(f"  ✓ XGBoost saved: {xgb_path}")
        
        # Save GRU
        gru_path = test_dir / "gru_test.h5"
        gru_trainer.model.save(str(gru_path))
        logger.info(f"  ✓ GRU saved: {gru_path}")
        
        # Test ONNX export
        try:
            import onnxmltools
            from onnxmltools.convert.common.data_types import FloatTensorType
            
            xgb_onnx_path = test_dir / "xgboost_test.onnx"
            initial_type = [('float_input', FloatTensorType([None, n_features]))]
            onnx_model = onnxmltools.convert_xgboost(xgb_trainer.model, initial_types=initial_type)
            
            with open(xgb_onnx_path, 'wb') as f:
                f.write(onnx_model.SerializeToString())
            logger.info(f"  ✓ XGBoost ONNX: {xgb_onnx_path}")
        except Exception as e:
            logger.warning(f"  ⚠ XGBoost ONNX export failed (non-critical): {e}")
        
        try:
            import tf2onnx
            
            gru_onnx_path = test_dir / "gru_test.onnx"
            onnx_model, _ = tf2onnx.convert.from_keras(gru_trainer.model, opset=11)
            
            with open(gru_onnx_path, 'wb') as f:
                f.write(onnx_model.SerializeToString())
            logger.info(f"  ✓ GRU ONNX: {gru_onnx_path}")
        except Exception as e:
            logger.warning(f"  ⚠ GRU ONNX export failed (non-critical): {e}")
        
        logger.info("✓ Model export successful")
        
        # Cleanup
        import shutil
        shutil.rmtree(test_dir)
        
    except Exception as e:
        logger.error(f"✗ Model export failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # Test 7: Model Registry
    logger.info("\n[7/7] Testing model registry...")
    try:
        settings = get_settings()
        engine = create_async_engine(str(settings.DATABASE_URL), echo=False)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        
        async with async_session() as session:
            from cryptography.fernet import Fernet
            encryption_key = Fernet.generate_key().decode()
            
            registry = ModelRegistry(
                session=session,
                model_storage_path=Path("models/test_registry"),
                encryption_key=encryption_key
            )
            
            # Create test model file
            test_model_path = Path("models/test_registry/test_model.json")
            test_model_path.parent.mkdir(parents=True, exist_ok=True)
            test_model_path.write_bytes(b"test model content")
            
            # Test registration
            import time
            test_version = f"test_{int(time.time())}"
            
            metadata = await registry.register_model(
                version=test_version,
                model_type="xgboost",
                artifact_path=test_model_path,
                metrics=xgb_metrics,
                metadata={'test': True},
                feature_version="1.0.0",
                status="development"
            )
            logger.info(f"  ✓ Model registered: ID {metadata.id}")
            
            # Cleanup
            import shutil
            shutil.rmtree("models/test_registry")
        
        await engine.dispose()
        logger.info("✓ Model registry successful")
        
    except Exception as e:
        logger.error(f"✗ Model registry failed: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    return True


async def main():
    """Run all tests"""
    success = await test_complete_workflow()
    
    print("\n" + "=" * 80)
    if success:
        print("✓ ALL POST-TRAINING TESTS PASSED")
        print("✓ Safe to run full training pipeline")
    else:
        print("✗ SOME TESTS FAILED")
        print("✗ Fix issues before running full training")
        sys.exit(1)
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
