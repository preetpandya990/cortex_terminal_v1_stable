"""
Cortex AI — ML Inference Engine
=================================
Production inference engine with ONNX optimization, probability calibration,
and SHAP explainability.
"""
from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.inference.onnx_converter import ONNXConverter, convert_model_to_onnx
from app.ml.inference.shap_explainer import SHAPExplainer
from app.ml.inference.prediction_engine import PredictionEngine, create_prediction_engine
from app.ml.inference.ensemble_predictor import EnsemblePredictor
from app.ml.inference.feature_loader import FeatureLoader, create_feature_loader


def create_shap_explainer(*args, **kwargs):
    return SHAPExplainer(*args, **kwargs)


__all__ = [
    "ConfidenceCalibrator",
    "ONNXConverter",
    "convert_model_to_onnx",
    "SHAPExplainer",
    "create_shap_explainer",
    "PredictionEngine",
    "create_prediction_engine",
    "EnsemblePredictor",
    "FeatureLoader",
    "create_feature_loader",
]
