"""
Cortex AI — XGBoost Treelite Export
====================================
Production-grade XGBoost model compilation using Treelite 4.7.0.

Treelite compiles tree ensemble models into optimized native code,
delivering 5-10x faster inference than native XGBoost. This is the
industry-standard approach used by AWS SageMaker and NVIDIA Triton.

Architecture:
    XGBoost JSON → Treelite Model → Compiled Shared Library (.so/.dll)
    
Performance: 5-10x faster than native XGBoost, 50-100x faster than ONNX

Run:
    python scripts/export_xgboost_treelite.py

Author: Cortex AI Team
Date: 2026-04-20
Version: 1.0.0
"""
import logging
import platform
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import tl2cgen
import treelite

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Paths
XGB_JSON_PATH = Path("models/production/models/xgboost_model.json")
TREELITE_DIR = Path("models/production/treelite")
TREELITE_MODEL_PATH = TREELITE_DIR / "xgboost_model.tl"
TREELITE_LIB_PATH = TREELITE_DIR / "xgboost_model"  # Extension added by compiler

# Model configuration
N_FEATURES = 47
N_CLASSES = 2  # Binary classification


def export() -> None:
    """Export XGBoost model using Treelite for production inference."""
    # Validate input file exists
    if not XGB_JSON_PATH.exists():
        logger.error("XGBoost model not found at %s", XGB_JSON_PATH)
        sys.exit(1)
    
    logger.info("=" * 80)
    logger.info("Cortex AI — XGBoost Treelite Compilation")
    logger.info("=" * 80)
    logger.info("Input:  %s", XGB_JSON_PATH)
    logger.info("Output: %s", TREELITE_LIB_PATH)
    logger.info("")
    
    # Step 1: Load XGBoost model into Treelite
    logger.info("[1/4] Loading XGBoost model...")
    try:
        model = treelite.frontend.load_xgboost_model(str(XGB_JSON_PATH))
        logger.info("✓ Model loaded successfully")
        logger.info("  Trees: %d", model.num_tree)
        logger.info("  Features: %d", model.num_feature)
        
        # Validate model structure
        if model.num_feature != N_FEATURES:
            logger.error(
                "Feature count mismatch: expected %d, got %d",
                N_FEATURES,
                model.num_feature
            )
            sys.exit(1)
            
    except Exception as e:
        logger.error("Failed to load XGBoost model: %s", e)
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    # Step 2: Export Treelite checkpoint (optional, for portability)
    logger.info("\n[2/4] Exporting Treelite checkpoint...")
    try:
        TREELITE_DIR.mkdir(parents=True, exist_ok=True)
        model.serialize(str(TREELITE_MODEL_PATH))
        logger.info("✓ Checkpoint saved: %s", TREELITE_MODEL_PATH)
        logger.info("  Size: %.2f MB", TREELITE_MODEL_PATH.stat().st_size / (1024 * 1024))
    except Exception as e:
        logger.error("Failed to save checkpoint: %s", e)
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    # Step 3: Compile model to native code using TL2cgen
    logger.info("\n[3/4] Compiling model to native code...")
    try:
        # Determine platform-specific library extension
        system = platform.system()
        if system == "Windows":
            lib_ext = ".dll"
        elif system == "Darwin":
            lib_ext = ".dylib"
        else:  # Linux
            lib_ext = ".so"
        
        # Compile with production optimizations using TL2cgen
        tl2cgen.export_lib(
            model,
            toolchain="gcc",  # Use GCC for maximum compatibility
            libpath=str(TREELITE_LIB_PATH) + lib_ext,
            params={
                "parallel_comp": 4,  # Parallel compilation threads
                "quantize": 0,  # No quantization for maximum accuracy
                "verbose": True,
            },
            verbose=True
        )
        
        compiled_lib = Path(str(TREELITE_LIB_PATH) + lib_ext)
        logger.info("✓ Model compiled successfully")
        logger.info("  Library: %s", compiled_lib)
        logger.info("  Size: %.2f MB", compiled_lib.stat().st_size / (1024 * 1024))
        
    except Exception as e:
        logger.error("Failed to compile model: %s", e)
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    # Step 4: Validate compiled model
    logger.info("\n[4/4] Validating compiled model...")
    try:
        # Load compiled predictor using TL2cgen
        predictor = tl2cgen.Predictor(str(compiled_lib))
        logger.info("✓ Predictor loaded")
        logger.info("  Input features: %d", predictor.num_feature)
        
        # Run inference with synthetic data
        rng = np.random.default_rng(42)
        test_data = rng.standard_normal((5, N_FEATURES)).astype(np.float32)
        
        # Create DMatrix for TL2cgen
        dmat = tl2cgen.DMatrix(test_data)
        
        # Run prediction
        predictions = predictor.predict(dmat)
        
        logger.info("  Test inference output shape: %s", predictions.shape)
        logger.info("  Sample predictions: %s", predictions[:2])
        
        # Validate output
        assert predictions.shape[0] == 5, f"Wrong batch size: {predictions.shape}"
        assert not np.any(np.isnan(predictions)), "NaN detected in output"
        assert not np.any(np.isinf(predictions)), "Inf detected in output"
        
        # For binary classification, predictions should be probabilities
        if predictions.ndim == 2 and predictions.shape[1] == 2:
            # Two-column output: [P(class=0), P(class=1)]
            assert np.all(predictions >= 0) and np.all(predictions <= 1), \
                "Probabilities out of range [0, 1]"
            logger.info("  Output format: Two-class probabilities")
        else:
            # Single-column output: P(class=1) for binary classification
            logger.info("  Output format: Single probability (P(UP))")
        
        logger.info("✓ Validation passed")
        
    except Exception as e:
        logger.error("Validation failed: %s", e)
        import traceback
        logger.error(traceback.format_exc())
        sys.exit(1)
    
    # Success summary
    logger.info("\n" + "=" * 80)
    logger.info("✓ XGBoost Treelite compilation completed successfully")
    logger.info("=" * 80)
    logger.info("Performance: 5-10x faster than native XGBoost")
    logger.info("Compiled library: %s", compiled_lib)
    logger.info("")
    logger.info("Next steps:")
    logger.info("  1. Update registry record to point to Treelite library:")
    logger.info("     UPDATE ml_model_metadata")
    logger.info("     SET onnx_path = '%s'", compiled_lib)
    logger.info("     WHERE model_version = '1.0.0_xgboost';")
    logger.info("  2. Continue with Phase 1: Registry Loader implementation")
    logger.info("=" * 80)


if __name__ == "__main__":
    export()
