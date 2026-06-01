"""
Cortex AI — Ensemble Prediction Engine
========================================
Production-grade ensemble predictor with XGBoost + GRU models.

Features:
- Dual-model ensemble (configurable weights)
- Multi-backend support (Treelite for XGBoost, ONNX for GRU)
- Intelligent post-processing (entry/SL/TP calculation)
- Redis caching for <1ms latency
- Comprehensive error handling and fallbacks

Author: Cortex AI Team
Date: 2026-04-20
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Protocol

import numpy as np
import onnxruntime as ort

from app.core.redis import CacheService
from app.ml.inference.calibrator import ConfidenceCalibrator
from app.ml.monitoring.metrics import (
    feature_cache_hit_rate,
    model_inference_duration_seconds,
    prediction_requests_total,
)

logger = logging.getLogger(__name__)


class ModelBackend(Protocol):
    """Protocol for model inference backends."""
    
    def predict(self, input_data: np.ndarray) -> np.ndarray:
        """Run inference and return probabilities."""
        ...


class TreeliteBackend:
    """Treelite backend for XGBoost models."""
    
    def __init__(self, predictor: Any):
        """
        Initialize Treelite backend.
        
        Args:
            predictor: tl2cgen.Predictor instance
        """
        self.predictor = predictor
    
    def predict(self, input_data: np.ndarray) -> np.ndarray:
        """
        Run inference via Treelite.
        
        Args:
            input_data: Shape (batch_size, n_features)
            
        Returns:
            Probabilities array (batch_size, n_classes)
        """
        import tl2cgen
        dmat = tl2cgen.DMatrix(input_data)
        output = self.predictor.predict(dmat)
        
        # Treelite output shape: (batch, 1, 1) for binary classification
        # Squeeze to get (batch,) then convert to (batch, 2) format
        output = output.squeeze()  # (batch,) or scalar
        
        # Ensure 1D array
        if output.ndim == 0:
            output = np.array([output])
        
        # Binary classification: single probability P(class=1)
        # Convert to [P(class=0), P(class=1)]
        prob_class_1 = output.astype(np.float32)
        prob_class_0 = 1.0 - prob_class_1
        
        # Stack to (batch, 2)
        return np.column_stack([prob_class_0, prob_class_1])


class ONNXBackend:
    """ONNX Runtime backend for neural network models."""
    
    def __init__(self, session: ort.InferenceSession, input_name: str, output_names: list[str]):
        """
        Initialize ONNX backend.
        
        Args:
            session: ONNX Runtime InferenceSession
            input_name: Input tensor name
            output_names: Output tensor names
        """
        self.session = session
        self.input_name = input_name
        self.output_names = output_names
    
    def predict(self, input_data: np.ndarray) -> np.ndarray:
        """
        Run inference via ONNX Runtime.
        
        Args:
            input_data: Input tensor (shape depends on model)
            
        Returns:
            Probabilities array
        """
        outputs = self.session.run(
            self.output_names,
            {self.input_name: input_data}
        )
        
        # Return first output (probabilities)
        return outputs[0].astype(np.float32)


class EnsemblePredictor:
    """
    Production ensemble predictor combining XGBoost and GRU models.
    
    Implements weighted ensemble, intelligent post-processing,
    and sub-millisecond caching for real-time trading.
    
    Supports multiple backends:
    - Treelite for XGBoost (5-10x faster)
    - ONNX Runtime for GRU
    """

    def __init__(
        self,
        xgboost_backend:  ModelBackend,
        gru_backend:      ModelBackend | None = None,
        cache:            CacheService | None = None,
        xgboost_weight:   float = 0.75,
        gru_weight:       float = 0.25,
        n_features:       int = 37,
        sequence_length:  int = 60,
        feature_names:    tuple[str, ...] = (),
        xgb_calibrator:   ConfidenceCalibrator | None = None,
        gru_calibrator:   ConfidenceCalibrator | None = None,
    ):
        """
        Initialize ensemble predictor with pre-loaded backends.

        Args:
            xgboost_backend:  XGBoost inference backend (Treelite or ONNX) — required.
            gru_backend:      GRU inference backend (ONNX) — optional.  When None the
                              predictor operates in XGBoost-only mode: xgboost_weight is
                              forced to 1.0 and gru_weight to 0.0 regardless of the
                              values passed in.  This supports the Single-Active-Member
                              Ensemble pattern where GRU is registered but dormant.
            cache:            Redis cache service.
            xgboost_weight:   Weight for XGBoost predictions.
            gru_weight:       Weight for GRU predictions (ignored when gru_backend=None).
            n_features:       Feature count — always sourced from LoadedEnsemble, which reads
                              it from the model artifact (never a config constant).
            sequence_length:  GRU sequence length, also sourced from LoadedEnsemble.
            feature_names:    Ordered feature manifest from training_features DB column.
                              Empty tuple when the manifest is not yet stored (old models).
            xgb_calibrator:   Beta calibrator for XGBoost (None = passthrough).
            gru_calibrator:   Temperature-scaling calibrator for GRU (None = passthrough).
        """
        self.xgboost_backend = xgboost_backend
        self.gru_backend     = gru_backend
        self.cache           = cache
        self.n_features      = n_features
        self.sequence_length = sequence_length
        self.feature_names   = feature_names
        self.xgb_calibrator  = xgb_calibrator
        self.gru_calibrator  = gru_calibrator

        if gru_backend is None:
            # XGBoost-only mode — normalise weights regardless of what was passed in.
            self.xgboost_weight = 1.0
            self.gru_weight     = 0.0
            logger.info(
                "EnsemblePredictor initialised: XGBoost-only (100%%, GRU dormant)  "
                "| n_features=%d  manifest=%s  calibrators: XGB=%s",
                n_features, "yes" if feature_names else "no",
                "yes" if xgb_calibrator else "no",
            )
        else:
            self.xgboost_weight = xgboost_weight
            self.gru_weight     = gru_weight
            if not np.isclose(xgboost_weight + gru_weight, 1.0):
                raise ValueError(
                    f"Weights must sum to 1.0, got {xgboost_weight + gru_weight}"
                )
            logger.info(
                "EnsemblePredictor initialised: XGBoost (%.0f%%) + GRU (%.0f%%)  "
                "| n_features=%d  manifest=%s  calibrators: XGB=%s  GRU=%s",
                xgboost_weight * 100, gru_weight * 100,
                n_features, "yes" if feature_names else "no",
                "yes" if xgb_calibrator else "no",
                "yes" if gru_calibrator else "no",
            )
    
    @classmethod
    def from_loaded_ensemble(
        cls,
        loaded_ensemble: Any,  # LoadedEnsemble from registry_loader
        cache: CacheService | None = None,
    ) -> EnsemblePredictor:
        """
        Factory method to create EnsemblePredictor from LoadedEnsemble.
        
        This bridges the registry loader with the prediction engine.
        
        Args:
            loaded_ensemble: LoadedEnsemble instance from RegistryModelLoader
            cache: Redis cache service
            
        Returns:
            Configured EnsemblePredictor instance
            
        Example:
            >>> loader = RegistryModelLoader(session)
            >>> ensemble = await loader.load_production_ensemble()
            >>> predictor = EnsemblePredictor.from_loaded_ensemble(ensemble, cache)
            >>> # Ready for inference
        """
        xgb_backend = TreeliteBackend(loaded_ensemble.xgboost_predictor)
        gru_backend: ONNXBackend | None = None
        if loaded_ensemble.gru_session is not None:
            gru_backend = ONNXBackend(
                session=loaded_ensemble.gru_session,
                input_name=loaded_ensemble.gru_input_name,
                output_names=loaded_ensemble.gru_output_names,
            )

        return cls(
            xgboost_backend = xgb_backend,
            gru_backend     = gru_backend,
            cache           = cache,
            xgboost_weight  = loaded_ensemble.xgboost_weight,
            gru_weight      = loaded_ensemble.gru_weight,
            n_features      = loaded_ensemble.n_features,
            sequence_length = loaded_ensemble.sequence_length,
            feature_names   = loaded_ensemble.feature_names,
            xgb_calibrator  = loaded_ensemble.xgb_calibrator,
            gru_calibrator  = loaded_ensemble.gru_calibrator,
        )

    async def predict(
        self,
        features_tabular: np.ndarray,
        features_sequence: np.ndarray,
        symbol: str,
        current_price: float,
        volatility: float | None = None,
        timeframe: str = "1d",
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """
        Generate ensemble prediction with intelligent post-processing.

        Args:
            features_tabular: Tabular features for XGBoost (40 features)
            features_sequence: Sequence features for GRU (60, 40)
            symbol: Stock symbol
            current_price: Current market price
            volatility: Historical volatility (optional, computed if None)
            timeframe: Timeframe (for caching)
            use_cache: Whether to use Redis cache

        Returns:
            Prediction dict with direction, confidence, entry, SL, TPs

        Example:
            >>> predictor = EnsemblePredictor("xgb.onnx", "gru.onnx")
            >>> pred = await predictor.predict(
            ...     features_tabular=np.random.randn(40),
            ...     features_sequence=np.random.randn(60, 40),
            ...     symbol="NSE_EQ|INE002A01018",
            ...     current_price=1500.0,
            ...     volatility=0.02
            ... )
            >>> print(f"{pred['direction_label']}: {pred['confidence']:.2%}")
        """
        # Check cache
        if use_cache and self.cache:
            cached = await self._get_cached_prediction(symbol, timeframe)
            if cached:
                logger.debug("Cache hit: symbol=%s timeframe=%s", symbol, timeframe)
                feature_cache_hit_rate.labels(cache_type="prediction").set(100.0)
                prediction_requests_total.labels(
                    symbol=symbol, timeframe=timeframe, status="cache_hit"
                ).inc()
                return cached
        feature_cache_hit_rate.labels(cache_type="prediction").set(0.0)

        # Validate inputs against the feature contract read from the model artifact
        if features_tabular.shape[-1] != self.n_features:
            raise ValueError(
                f"Expected {self.n_features} tabular features, got {features_tabular.shape[-1]}. "
                f"Ensure FeatureLoader uses the same feature set as the active model."
            )
        expected_seq_shape = (self.sequence_length, self.n_features)
        if features_sequence.shape != expected_seq_shape:
            raise ValueError(
                f"Expected sequence shape {expected_seq_shape}, got {features_sequence.shape}."
            )

        # Prepare inputs
        xgb_input = features_tabular.reshape(1, -1).astype(np.float32)
        gru_input = features_sequence.reshape(1, self.sequence_length, self.n_features).astype(np.float32)

        # Run inference via backends
        _t0 = time.perf_counter()
        try:
            xgb_proba = self.xgboost_backend.predict(xgb_input)[0]  # (2,)

            # Apply post-hoc calibration before mixing.
            # Calibrating before the weighted average ensures each model's
            # probabilities are individually reliable; calibrating the mixture
            # after blending would confound the two error sources.
            if self.xgb_calibrator is not None:
                xgb_proba = self.xgb_calibrator.calibrate(xgb_proba)  # (2,)

            if self.gru_backend is not None:
                gru_proba = self.gru_backend.predict(gru_input)[0]    # (2,)
                if self.gru_calibrator is not None:
                    gru_proba = self.gru_calibrator.calibrate(gru_proba)
                # Weighted average — both outputs are valid probability distributions
                # summing to 1.0; the weighted sum preserves that property.
                ensemble_proba = self.xgboost_weight * xgb_proba + self.gru_weight * gru_proba
                _model_name = "ensemble_xgb_gru"
            else:
                # XGBoost-only mode (GRU dormant) — weight is already 1.0.
                ensemble_proba = xgb_proba
                _model_name = "xgboost_only"

            model_inference_duration_seconds.labels(
                model_name=_model_name, model_version="ensemble_v1.0"
            ).observe(time.perf_counter() - _t0)
            prediction_requests_total.labels(
                symbol=symbol, timeframe=timeframe, status="success"
            ).inc()

        except Exception as exc:
            model_inference_duration_seconds.labels(
                model_name="ensemble", model_version="ensemble_v1.0"
            ).observe(time.perf_counter() - _t0)
            prediction_requests_total.labels(
                symbol=symbol, timeframe=timeframe, status="error"
            ).inc()
            logger.error("Inference failed: symbol=%s error=%s", symbol, exc, exc_info=True)
            raise RuntimeError(f"Model inference failed: {exc}") from exc

        # Post-process prediction
        prediction = self._post_process(
            probabilities=ensemble_proba,
            current_price=current_price,
            volatility=volatility or self._estimate_volatility(features_tabular),
            symbol=symbol,
            timeframe=timeframe,
        )

        # Cache prediction
        if use_cache and self.cache:
            await self._cache_prediction(symbol, timeframe, prediction)

        return prediction

    def _post_process(
        self,
        probabilities: np.ndarray,
        current_price: float,
        volatility: float,
        symbol: str,
        timeframe: str,
    ) -> dict[str, Any]:
        """
        Post-process raw probabilities into actionable trading signals.

        Processing steps:
        1. Direction classification and raw confidence extraction
        2. Volatility-regime adaptive confidence threshold (replaces static 0.60)
        3. Soft conviction scale — 0.0 at threshold, 1.0 at full confidence
        4. Volatility-based stop loss and take-profit levels
        5. HOLD override when confidence < threshold (price levels preserved)

        The HOLD override only changes the direction label — stop/TP levels are
        always computed and returned so that downstream position sizing
        (qty_suggester) and risk display remain valid even on overridden signals.

        Args:
            probabilities: Class probabilities [DOWN, UP] for binary classification,
                           or [SELL, HOLD, BUY] for 3-class.
            current_price: Current market price
            volatility:    Annualized historical volatility (proxy for India VIX regime)
            symbol:        NSE trading symbol
            timeframe:     Prediction timeframe

        Returns:
            Prediction dict including direction, confidence, conviction_scale,
            entry/SL/TP levels, and observability metadata.
        """
        # ── Step 1: Direction and raw confidence ──────────────────────────────
        if len(probabilities) == 2:
            prob_down, prob_up = float(probabilities[0]), float(probabilities[1])
            prob_hold = 0.0
            if prob_up > prob_down:
                direction, direction_label, confidence = 2, "BUY",  prob_up
            else:
                direction, direction_label, confidence = 0, "SELL", prob_down
        else:
            # 3-class: SELL=0, HOLD=1, BUY=2
            direction = int(np.argmax(probabilities))
            direction_label = ["SELL", "HOLD", "BUY"][direction]
            confidence = float(np.max(probabilities))
            prob_down = float(probabilities[0])
            prob_hold = float(probabilities[1])
            prob_up   = float(probabilities[2])

        # ── Step 2: Volatility-regime adaptive threshold ───────────────────────
        # Annualized vol > 0.35 ≈ India VIX elevated (stressed regime) — raise
        # the gate to avoid overtrading on noisy, low-conviction signals.
        # Annualized vol < 0.20 ≈ benign / low-vol regime — relax the gate so
        # valid setups in calm markets are not suppressed.
        if volatility > 0.35:
            threshold = 0.70   # high-volatility: elevated India VIX equivalent
        elif volatility < 0.20:
            threshold = 0.55   # low-volatility: benign / range-bound regime
        else:
            threshold = 0.60   # normal regime

        # ── Step 3: Soft conviction scale ─────────────────────────────────────
        # Linear interpolation from 0.0 (at threshold) to 1.0 (at full confidence).
        # Avoids the cliff-edge of a binary BUY/HOLD gate; enables qty_suggester
        # to apply graduated sizing — a marginally confident signal gets a smaller
        # position, a highly confident one gets the full Kelly-sized quantity.
        conviction_scale = max(
            0.0,
            (confidence - threshold) / max(1.0 - threshold, 1e-6),
        )

        # ── Step 4: Volatility-based price levels ─────────────────────────────
        # Computed BEFORE the HOLD override so they remain valid for position
        # sizing and contextual UI display even when direction is overridden.
        entry_price = current_price
        daily_vol = volatility / np.sqrt(252)   # annualised → daily
        sl_pct    = 1.5 * daily_vol             # 1.5× daily vol stop distance

        if direction == 2:   # BUY
            stop_loss = entry_price * (1.0 - sl_pct)
            tp1       = entry_price * (1.0 + 1.5 * sl_pct)
            tp2       = entry_price * (1.0 + 2.5 * sl_pct)
            tp3       = entry_price * (1.0 + 4.0 * sl_pct)
        elif direction == 0:  # SELL
            stop_loss = entry_price * (1.0 + sl_pct)
            tp1       = entry_price * (1.0 - 1.5 * sl_pct)
            tp2       = entry_price * (1.0 - 2.5 * sl_pct)
            tp3       = entry_price * (1.0 - 4.0 * sl_pct)
        else:                 # HOLD (3-class model produced HOLD directly)
            stop_loss = entry_price
            tp1 = tp2 = tp3 = entry_price

        # ── Step 5: HOLD override ─────────────────────────────────────────────
        # Direction label only — price levels are intentionally preserved above.
        if confidence < threshold:
            direction       = 1
            direction_label = "HOLD"

        # ── Sanitize NaN values for safe JSON serialization ───────────────────
        def _safe(v: float) -> float:
            return 0.0 if (v != v) else v   # NaN check without importing math

        return {
            "direction":       direction,
            "direction_label": direction_label,
            "confidence":      _safe(confidence),
            "conviction_scale": _safe(conviction_scale),
            "threshold":       threshold,
            "probabilities": {
                "sell": _safe(prob_down),
                "hold": _safe(prob_hold),
                "buy":  _safe(prob_up),
            },
            "entry_price": float(entry_price),
            "stop_loss":   float(stop_loss),
            "tp1":         float(tp1),
            "tp2":         float(tp2),
            "tp3":         float(tp3),
            "volatility":  float(volatility),
            "metadata": {
                "symbol":          symbol,
                "timeframe":       timeframe,
                "model_version":   "ensemble_v1.0",
                "xgboost_weight":  self.xgboost_weight,
                "gru_weight":      self.gru_weight,
                "predicted_at":    datetime.now(timezone.utc).isoformat(),
            },
        }

    @staticmethod
    def _estimate_volatility(features: np.ndarray) -> float:
        """
        Estimate volatility from features if not provided.
        
        Uses feature index 1 (returns_1d) as proxy for volatility.
        Returns annualized volatility (default: 20% if unavailable).
        """
        try:
            # Feature 1 is typically returns_1d
            daily_return = abs(features[1]) if len(features) > 1 else 0.01
            # Annualize: daily_vol * sqrt(252)
            return float(daily_return * np.sqrt(252))
        except Exception:
            return 0.20  # Default 20% annualized volatility

    async def _get_cached_prediction(
        self,
        symbol: str,
        timeframe: str,
    ) -> dict[str, Any] | None:
        """Retrieve cached prediction from Redis."""
        if not self.cache:
            return None

        cache_key = f"ml:ensemble:prediction:{symbol}:{timeframe}"
        return await self.cache.get(cache_key)

    async def _cache_prediction(
        self,
        symbol: str,
        timeframe: str,
        prediction: dict[str, Any],
    ) -> None:
        """Cache prediction in Redis with 5-minute TTL."""
        if not self.cache:
            return

        cache_key = f"ml:ensemble:prediction:{symbol}:{timeframe}"
        ttl = 300  # 5 minutes (balance freshness vs. load)

        await self.cache.set(cache_key, prediction, ttl)
        logger.debug("Cached prediction: symbol=%s timeframe=%s ttl=%ds", symbol, timeframe, ttl)

    async def predict_batch(
        self,
        batch: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Batch inference for N symbols in a single GPU call.

        Decouples feature loading (I/O, done by caller) from inference (GPU).
        Running N sequential inferences on a 4 GB VRAM card risks VRAM
        fragmentation; a single batched call avoids that entirely.

        Args:
            batch: List of dicts, each containing:
                - symbol       (str)
                - tabular      (np.ndarray shape (40,))
                - sequence     (np.ndarray shape (60, 40))
                - current_price (float)
                - volatility   (float | None)
                - timeframe    (str, default "1d")

        Returns:
            List of prediction dicts in the same order as `batch`.
            Symbols whose features are None are returned with a HOLD/zero result.

        Note:
            Each result is cached individually with the standard 5-minute TTL.
        """
        if not batch:
            return []

        # ── Step 1: resolve cache hits ──────────────────────────────────────
        results: list[dict[str, Any] | None] = [None] * len(batch)
        uncached_indices: list[int] = []

        for i, item in enumerate(batch):
            symbol = item["symbol"]
            timeframe = item.get("timeframe", "1d")
            if self.cache:
                cached = await self._get_cached_prediction(symbol, timeframe)
                if cached:
                    results[i] = cached
                    continue
            uncached_indices.append(i)

        if not uncached_indices:
            return results  # type: ignore[return-value]

        # ── Step 2: batch inference on uncached symbols ─────────────────────
        uncached = [batch[i] for i in uncached_indices]

        # Stack tabular inputs — always needed for XGBoost.
        xgb_inputs = np.vstack(
            [item["tabular"].reshape(1, -1).astype(np.float32) for item in uncached]
        )

        try:
            xgb_probas = self.xgboost_backend.predict(xgb_inputs)  # (N, 2)
            if self.xgb_calibrator is not None:
                xgb_probas = self.xgb_calibrator.calibrate(xgb_probas)  # (N, 2)

            if self.gru_backend is not None:
                gru_inputs = np.stack(
                    [item["sequence"].reshape(self.sequence_length, self.n_features).astype(np.float32)
                     for item in uncached]
                )
                gru_probas = self.gru_backend.predict(gru_inputs)        # (N, 2)
                if self.gru_calibrator is not None:
                    gru_probas = self.gru_calibrator.calibrate(gru_probas)
            else:
                gru_probas = None  # XGBoost-only mode

        except Exception as exc:
            logger.error("Batch inference failed: %s", exc, exc_info=True)
            # Degrade gracefully — return HOLD for all uncached symbols
            for i in uncached_indices:
                item = batch[i]
                results[i] = self._hold_result(
                    symbol=item["symbol"],
                    current_price=item.get("current_price", 0.0),
                    timeframe=item.get("timeframe", "1d"),
                )
            return results  # type: ignore[return-value]

        # ── Step 3: post-process and cache ──────────────────────────────────
        for idx, orig_i in enumerate(uncached_indices):
            item = batch[orig_i]
            symbol = item["symbol"]
            timeframe = item.get("timeframe", "1d")
            current_price = item.get("current_price", 0.0)
            volatility = item.get("volatility") or self._estimate_volatility(item["tabular"])

            if gru_probas is not None:
                ensemble_proba = (
                    self.xgboost_weight * xgb_probas[idx]
                    + self.gru_weight * gru_probas[idx]
                )
            else:
                ensemble_proba = xgb_probas[idx]

            prediction = self._post_process(
                probabilities=ensemble_proba,
                current_price=current_price,
                volatility=volatility,
                symbol=symbol,
                timeframe=timeframe,
            )

            if self.cache:
                await self._cache_prediction(symbol, timeframe, prediction)

            results[orig_i] = prediction

        return results  # type: ignore[return-value]

    @staticmethod
    def _hold_result(symbol: str, current_price: float, timeframe: str) -> dict[str, Any]:
        """Return a neutral HOLD prediction for error/degraded cases."""
        return {
            "direction":        1,
            "direction_label":  "HOLD",
            "confidence":       0.0,
            "conviction_scale": 0.0,
            "threshold":        0.60,
            "probabilities":    {"sell": 0.0, "hold": 1.0, "buy": 0.0},
            "entry_price":      current_price,
            "stop_loss":        current_price,
            "tp1":              current_price,
            "tp2":              current_price,
            "tp3":              current_price,
            "volatility":       0.20,
            "metadata": {
                "symbol":         symbol,
                "timeframe":      timeframe,
                "model_version":  "ensemble_v1.0_degraded",
                "xgboost_weight": 0.0,
                "gru_weight":     0.0,
                "predicted_at":   datetime.now(timezone.utc).isoformat(),
            },
        }

    async def invalidate_cache(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> int:
        """
        Invalidate prediction cache.

        Args:
            symbol: Specific symbol (None = all)
            timeframe: Specific timeframe (None = all)

        Returns:
            Number of cache keys deleted
        """
        if not self.cache:
            return 0

        symbol_part = symbol if symbol else "*"
        timeframe_part = timeframe if timeframe else "*"

        pattern = f"ml:ensemble:prediction:{symbol_part}:{timeframe_part}"
        count = await self.cache.delete_pattern(pattern)

        logger.info("Invalidated %d ensemble prediction cache entries", count)
        return count


