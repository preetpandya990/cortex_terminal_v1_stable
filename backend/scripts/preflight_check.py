#!/usr/bin/env python3
"""
Pre-flight Check for ML Training Pipeline
==========================================
Verifies all dependencies and configurations before training continues.
"""

import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

def check_imports():
    """Check all required imports"""
    print("Checking imports...")
    errors = []
    
    try:
        import tensorflow as tf
        gpus = tf.config.list_physical_devices('GPU')
        if gpus:
            print(f"  ✓ TensorFlow {tf.__version__} with GPU: {len(gpus)} device(s)")
        else:
            print(f"  ⚠ TensorFlow {tf.__version__} - No GPU detected")
    except Exception as e:
        errors.append(f"TensorFlow: {e}")
    
    try:
        import torch
        print(f"  ✓ PyTorch {torch.__version__} (CPU: {not torch.cuda.is_available()})")
    except Exception as e:
        errors.append(f"PyTorch: {e}")
    
    try:
        import xgboost as xgb
        print(f"  ✓ XGBoost {xgb.__version__}")
    except Exception as e:
        errors.append(f"XGBoost: {e}")
    
    try:
        import keras
        print(f"  ✓ Keras {keras.__version__}")
    except Exception as e:
        errors.append(f"Keras: {e}")
    
    try:
        import keras_tuner as kt
        print(f"  ✓ Keras Tuner {kt.__version__}")
    except Exception as e:
        errors.append(f"Keras Tuner: {e}")
    
    try:
        import onnxmltools
        print(f"  ✓ onnxmltools {onnxmltools.__version__}")
    except Exception as e:
        errors.append(f"onnxmltools: {e}")
    
    try:
        import tf2onnx
        print(f"  ✓ tf2onnx {tf2onnx.__version__}")
    except Exception as e:
        errors.append(f"tf2onnx: {e}")
    
    try:
        import skl2onnx
        print(f"  ✓ skl2onnx {skl2onnx.__version__}")
    except Exception as e:
        errors.append(f"skl2onnx: {e}")
    
    try:
        from cryptography.fernet import Fernet
        print(f"  ✓ cryptography (Fernet encryption)")
    except Exception as e:
        errors.append(f"cryptography: {e}")
    
    try:
        import numpy as np
        print(f"  ✓ NumPy {np.__version__}")
        if np.__version__.startswith('2.'):
            print(f"    ⚠ NumPy 2.x detected - may cause scipy/scikit-learn issues")
    except Exception as e:
        errors.append(f"NumPy: {e}")
    
    try:
        import pandas as pd
        print(f"  ✓ Pandas {pd.__version__}")
    except Exception as e:
        errors.append(f"Pandas: {e}")
    
    try:
        import sklearn
        print(f"  ✓ scikit-learn {sklearn.__version__}")
    except Exception as e:
        errors.append(f"scikit-learn: {e}")
    
    return errors


def check_modules():
    """Check custom modules"""
    print("\nChecking custom modules...")
    errors = []
    
    try:
        from app.ml.training.xgboost_trainer import XGBoostTrainer
        print("  ✓ XGBoostTrainer")
    except Exception as e:
        errors.append(f"XGBoostTrainer: {e}")
    
    try:
        from app.ml.training.gru_trainer import GRUTrainer
        print("  ✓ GRUTrainer")
    except Exception as e:
        errors.append(f"GRUTrainer: {e}")
    
    try:
        from app.ml.training.ensemble_trainer import EnsembleTrainer
        print("  ✓ EnsembleTrainer")
    except Exception as e:
        errors.append(f"EnsembleTrainer: {e}")
    
    try:
        from app.ml.model_registry import ModelRegistry
        print("  ✓ ModelRegistry")
    except Exception as e:
        errors.append(f"ModelRegistry: {e}")
    
    try:
        from app.ml.inference.onnx_converter import ONNXConverter
        print("  ✓ ONNXConverter")
    except Exception as e:
        errors.append(f"ONNXConverter: {e}")
    
    return errors


def check_environment():
    """Check environment variables"""
    print("\nChecking environment...")
    import os
    from pathlib import Path
    
    errors = []
    warnings = []
    
    # Check .env file
    env_file = Path(__file__).parent.parent / '.env'
    if not env_file.exists():
        errors.append(".env file not found")
        return errors, warnings
    
    print(f"  ✓ .env file exists")
    
    # Load .env
    with open(env_file) as f:
        env_content = f.read()
    
    # Check critical variables
    if 'DATABASE_URL' in env_content:
        print("  ✓ DATABASE_URL configured")
    else:
        errors.append("DATABASE_URL not found in .env")
    
    if 'SECRET_KEY' in env_content:
        print("  ✓ SECRET_KEY configured")
    else:
        warnings.append("SECRET_KEY not found in .env")
    
    if 'ML_MODEL_ENCRYPTION_KEY' in env_content:
        print("  ✓ ML_MODEL_ENCRYPTION_KEY configured")
    else:
        warnings.append("ML_MODEL_ENCRYPTION_KEY not found (will be auto-generated)")
    
    return errors, warnings


def check_directories():
    """Check required directories"""
    print("\nChecking directories...")
    from pathlib import Path
    
    base_dir = Path(__file__).parent.parent
    required_dirs = [
        'models/production',
        'models/production/logs',
        'models/production/onnx',
        'data/features',
    ]
    
    errors = []
    for dir_path in required_dirs:
        full_path = base_dir / dir_path
        if full_path.exists():
            print(f"  ✓ {dir_path}")
        else:
            print(f"  ⚠ {dir_path} - creating...")
            full_path.mkdir(parents=True, exist_ok=True)
            print(f"    ✓ Created")
    
    return errors


def check_gpu():
    """Check GPU availability and configuration"""
    print("\nChecking GPU...")
    
    try:
        import tensorflow as tf
        
        gpus = tf.config.list_physical_devices('GPU')
        if not gpus:
            print("  ⚠ No GPU detected - training will be VERY slow")
            return ["No GPU available"]
        
        print(f"  ✓ {len(gpus)} GPU(s) detected")
        
        for i, gpu in enumerate(gpus):
            print(f"    GPU {i}: {gpu.name}")
            
            # Check memory growth
            try:
                tf.config.experimental.set_memory_growth(gpu, True)
                print(f"    ✓ Memory growth enabled")
            except:
                print(f"    ⚠ Could not enable memory growth")
        
        # Test GPU computation
        with tf.device('/GPU:0'):
            a = tf.constant([[1.0, 2.0]])
            b = tf.constant([[3.0], [4.0]])
            c = tf.matmul(a, b)
            result = c.numpy()
        
        print(f"  ✓ GPU computation test passed")
        
        return []
        
    except Exception as e:
        return [f"GPU check failed: {e}"]


def main():
    """Run all checks"""
    print("=" * 80)
    print("ML TRAINING PIPELINE PRE-FLIGHT CHECK")
    print("=" * 80)
    print()
    
    all_errors = []
    all_warnings = []
    
    # Run checks
    errors = check_imports()
    all_errors.extend(errors)
    
    errors = check_modules()
    all_errors.extend(errors)
    
    errors, warnings = check_environment()
    all_errors.extend(errors)
    all_warnings.extend(warnings)
    
    errors = check_directories()
    all_errors.extend(errors)
    
    errors = check_gpu()
    all_errors.extend(errors)
    
    # Summary
    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    
    if all_warnings:
        print(f"\n⚠ WARNINGS ({len(all_warnings)}):")
        for warning in all_warnings:
            print(f"  - {warning}")
    
    if all_errors:
        print(f"\n✗ ERRORS ({len(all_errors)}):")
        for error in all_errors:
            print(f"  - {error}")
        print("\n❌ Pre-flight check FAILED")
        print("Please fix the errors above before continuing training.")
        return 1
    else:
        print("\n✅ Pre-flight check PASSED")
        print("All systems ready for training pipeline.")
        if all_warnings:
            print("\nNote: Warnings detected but training can proceed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
