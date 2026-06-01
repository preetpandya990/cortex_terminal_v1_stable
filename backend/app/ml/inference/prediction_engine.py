"""
Cortex AI — Prediction Engine
===============================
Production inference engine with ONNX Runtime and caching.

Features:
- Fast ONNX Runtime inference
- Multi-output prediction (7 outputs)
- Post-processing with consistency constraints
- Redis caching for low-latency serving
- Async feature loading
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
import onnxruntime as ort

from app.core.redis import CacheService


logger = logging.getLogger(__name__)


class PredictionEngine:
    """
    Production prediction engine with ONNX Runtime.
    
    Handles model loading, inference, post-processing,
    and caching for low-latency predictions.
    """

    def __init__(
        self,
        model_path: str,
        cache: CacheService | None = None,
        num_threads: int = 4,
    ):
        """
        Initialize prediction engine.

        Args:
            model_path: Path to ONNX model file
            cache: Redis cache service
            num_threads: Number of threads for ONNX Runtime
        """
        self.model_path = model_path
        self.cache = cache

        # Configure ONNX Runtime session
        sess_options = ort.SessionOptions()
        sess_options.intra_op_num_threads = num_threads
        sess_options.inter_op_num_threads = num_threads
        sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        # Create ONNX Runtime session
        self.session = ort.InferenceSession(
            model_path,
            sess_options=sess_options,
            providers=['CPUExecutionProvider'],
        )

        # Get input/output names
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [output.name for output in self.session.get_outputs()]

        logger.info(
            f"Initialized PredictionEngine: {model_path}, "
            f"threads={num_threads}, outputs={len(self.output_names)}"
        )

    async def predict(
        self,
        feature_vector: np.ndarray,
        symbol: str | None = None,
        timeframe: str | None = None,
        model_version: str | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """
        Run inference and return predictions.

        Args:
            feature_vector: Input features (sequence_length, num_features)
            symbol: Stock symbol (for caching)
            timeframe: Timeframe (for caching)
            model_version: Model version (for caching)
            use_cache: Whether to use cache

        Returns:
            Dictionary with 7 outputs and metadata

        Example:
            >>> engine = PredictionEngine("model.onnx")
            >>> prediction = await engine.predict(features, "NSE_EQ|INE002A01018", "1d")
            >>> print(f"Direction: {prediction['direction']}")
        """
        # Check cache first
        if use_cache and self.cache and symbol and timeframe and model_version:
            cached_prediction = await self._get_cached_prediction(
                symbol, timeframe, model_version
            )
            if cached_prediction:
                logger.debug(f"Prediction cache hit for {symbol} [{timeframe}]")
                return cached_prediction

        # Prepare input
        if feature_vector.ndim == 2:
            # Add batch dimension
            feature_vector = feature_vector.reshape(1, *feature_vector.shape)

        # Run inference
        onnx_inputs = {self.input_name: feature_vector.astype(np.float32)}
        onnx_outputs = self.session.run(self.output_names, onnx_inputs)

        # Parse outputs
        raw_prediction = {
            "direction": onnx_outputs[0][0],  # Logits (3 classes)
            "confidence": float(onnx_outputs[1][0][0]),
            "entry_price": float(onnx_outputs[2][0][0]),
            "tp1": float(onnx_outputs[3][0][0]),
            "tp2": float(onnx_outputs[4][0][0]),
            "tp3": float(onnx_outputs[5][0][0]),
            "stop_loss": float(onnx_outputs[6][0][0]),
            "volatility": float(onnx_outputs[7][0][0]),
        }

        # Post-process prediction
        prediction = self.post_process(raw_prediction)

        # Add metadata
        prediction["metadata"] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "model_version": model_version,
            "predicted_at": datetime.now(timezone.utc).isoformat(),
        }

        # Cache prediction
        if use_cache and self.cache and symbol and timeframe and model_version:
            await self._cache_prediction(
                symbol, timeframe, model_version, prediction
            )

        return prediction

    def post_process(self, raw_prediction: dict[str, Any]) -> dict[str, Any]:
        """
        Post-process raw model outputs with consistency constraints.

        Applies:
        1. Direction classification (argmax of logits)
        2. Confidence clamping [0, 1]
        3. TP ordering enforcement
        4. Entry/SL/TP relationship enforcement
        5. Volatility positivity

        Args:
            raw_prediction: Raw model outputs

        Returns:
            Post-processed prediction

        Example:
            >>> processed = engine.post_process(raw_outputs)
        """
        # 1. Direction classification
        direction_logits = raw_prediction["direction"]
        direction = int(np.argmax(direction_logits))
        direction_label = ["SELL", "HOLD", "BUY"][direction]

        # 2. Confidence clamping
        confidence = np.clip(raw_prediction["confidence"], 0.0, 1.0)

        # 3. Extract prices
        entry = raw_prediction["entry_price"]
        tp1 = raw_prediction["tp1"]
        tp2 = raw_prediction["tp2"]
        tp3 = raw_prediction["tp3"]
        sl = raw_prediction["stop_loss"]
        volatility = max(raw_prediction["volatility"], 0.0)  # Ensure positive

        # 4. Enforce TP ordering
        if direction == 2:  # BUY
            # Ensure TP1 < TP2 < TP3
            tp1 = max(tp1, entry * 1.001)  # At least 0.1% above entry
            tp2 = max(tp2, tp1 * 1.005)    # At least 0.5% above TP1
            tp3 = max(tp3, tp2 * 1.005)    # At least 0.5% above TP2
            
            # Ensure SL < Entry < TP1
            sl = min(sl, entry * 0.99)     # At least 1% below entry
            
        elif direction == 0:  # SELL
            # Ensure TP1 > TP2 > TP3 (prices going down)
            tp1 = min(tp1, entry * 0.999)  # At least 0.1% below entry
            tp2 = min(tp2, tp1 * 0.995)    # At least 0.5% below TP1
            tp3 = min(tp3, tp2 * 0.995)    # At least 0.5% below TP2
            
            # Ensure SL > Entry > TP1
            sl = max(sl, entry * 1.01)     # At least 1% above entry
            
        else:  # HOLD
            # For HOLD, set TP levels equal to entry
            tp1 = entry
            tp2 = entry
            tp3 = entry
            sl = entry

        # 5. Override direction to HOLD if confidence < 0.5
        if confidence < 0.5:
            direction = 1
            direction_label = "HOLD"

        processed = {
            "direction": direction,
            "direction_label": direction_label,
            "confidence": float(confidence),
            "entry_price": float(entry),
            "tp1": float(tp1),
            "tp2": float(tp2),
            "tp3": float(tp3),
            "stop_loss": float(sl),
            "volatility": float(volatility),
        }

        return processed

    async def _get_cached_prediction(
        self,
        symbol: str,
        timeframe: str,
        model_version: str,
    ) -> dict[str, Any] | None:
        """
        Retrieve cached prediction.

        Args:
            symbol: Stock symbol
            timeframe: Timeframe
            model_version: Model version

        Returns:
            Cached prediction or None
        """
        if not self.cache:
            return None

        cache_key = f"ml:prediction:{symbol}:{timeframe}:{model_version}"
        return await self.cache.get(cache_key)

    async def _cache_prediction(
        self,
        symbol: str,
        timeframe: str,
        model_version: str,
        prediction: dict[str, Any],
    ) -> None:
        """
        Cache prediction in Redis.

        Args:
            symbol: Stock symbol
            timeframe: Timeframe
            model_version: Model version
            prediction: Prediction to cache
        """
        if not self.cache:
            return

        cache_key = f"ml:prediction:{symbol}:{timeframe}:{model_version}"
        ttl = 300  # 5 minutes

        await self.cache.set(cache_key, prediction, ttl)
        logger.debug(f"Cached prediction for {symbol} [{timeframe}]")

    async def invalidate_cache(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
        model_version: str | None = None,
    ) -> int:
        """
        Invalidate prediction cache.

        Args:
            symbol: Specific symbol (None = all)
            timeframe: Specific timeframe (None = all)
            model_version: Specific model version (None = all)

        Returns:
            Number of cache keys deleted
        """
        if not self.cache:
            return 0

        symbol_part = symbol if symbol else "*"
        timeframe_part = timeframe if timeframe else "*"
        version_part = model_version if model_version else "*"

        pattern = f"ml:prediction:{symbol_part}:{timeframe_part}:{version_part}"
        count = await self.cache.delete_pattern(pattern)

        logger.info(f"Invalidated {count} prediction cache entries")

        return count

    def get_model_info(self) -> dict[str, Any]:
        """
        Get model information.

        Returns:
            Dictionary with model metadata
        """
        return {
            "model_path": self.model_path,
            "input_name": self.input_name,
            "output_names": self.output_names,
            "num_outputs": len(self.output_names),
            "providers": self.session.get_providers(),
        }


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def create_prediction_engine(
    model_path: str,
    cache: CacheService | None = None,
    num_threads: int = 4,
) -> PredictionEngine:
    """
    Factory function to create PredictionEngine instance.

    Args:
        model_path: Path to ONNX model
        cache: Redis cache service
        num_threads: Number of threads for inference

    Returns:
        Configured PredictionEngine instance
    """
    return PredictionEngine(
        model_path=model_path,
        cache=cache,
        num_threads=num_threads,
    )
