"""
SHAP Explainer for ML Prediction System

Provides feature importance explanations for predictions using SHAP values.
Optimized for <50ms latency to fit within 250ms prediction budget.

Requirements: 7.1, 7.2, 7.3, 7.4
"""

import logging
import time
from typing import Dict, List, Optional, Tuple
import numpy as np
import asyncio
from dataclasses import dataclass
from functools import lru_cache

logger = logging.getLogger(__name__)


@dataclass
class FeatureContribution:
    """Feature contribution to prediction."""
    feature_name: str
    feature_value: float
    shap_value: float
    contribution_pct: float
    direction: str  # "positive" or "negative"
    interpretation: str


@dataclass
class Explanation:
    """SHAP explanation for a prediction."""
    top_features: List[FeatureContribution]
    total_contribution_pct: float
    formatted_text: str
    computation_time_ms: float


class SHAPExplainer:
    """
    SHAP-based prediction explainability.
    
    Provides feature importance explanations optimized for low latency:
    - Computes SHAP values for predictions
    - Identifies top K contributing features
    - Generates human-readable explanations
    - Completes within 50ms latency budget
    """
    
    def __init__(
        self,
        model=None,
        background_data: Optional[np.ndarray] = None,
        top_k: int = 5,
        timeout_ms: float = 50.0
    ):
        """
        Initialize SHAP explainer.
        
        Args:
            model: Trained model (for TreeExplainer or DeepExplainer)
            background_data: Background dataset for SHAP computation (small sample)
            top_k: Number of top features to return (default: 5)
            timeout_ms: Timeout for SHAP computation in milliseconds (default: 50ms)
        """
        self.model = model
        self.background_data = background_data
        self.top_k = top_k
        self.timeout_ms = timeout_ms
        self._explainer = None
        self._explainer_cache = {}
        
        # Feature interpretation templates
        self.interpretation_templates = self._load_interpretation_templates()
    
    def _load_interpretation_templates(self) -> Dict[str, Dict]:
        """
        Load interpretation templates for common technical indicators.
        
        Returns:
            Dictionary mapping feature names to interpretation rules
        """
        return {
            "rsi_14": {
                "overbought": (70, "RSI overbought (bearish signal)"),
                "oversold": (30, "RSI oversold (bullish signal)"),
                "neutral": "RSI neutral"
            },
            "macd_histogram": {
                "positive": "MACD bullish crossover",
                "negative": "MACD bearish crossover"
            },
            "bb_position": {
                "upper": (0.8, "Price near upper Bollinger Band (overbought)"),
                "lower": (0.2, "Price near lower Bollinger Band (oversold)"),
                "middle": "Price near Bollinger Band middle"
            },
            "volume_roc": {
                "high": (20, "Volume spike (accumulation/distribution)"),
                "low": (-20, "Volume decline"),
                "normal": "Normal volume"
            },
            "adx": {
                "strong": (25, "Strong trend strength"),
                "weak": (20, "Weak trend"),
                "very_strong": (50, "Very strong trend")
            }
        }
    
    async def explain(
        self,
        feature_vector: np.ndarray,
        feature_names: List[str],
        feature_values: Optional[Dict[str, float]] = None
    ) -> Explanation:
        """
        Generate SHAP explanation for a prediction.
        
        Args:
            feature_vector: Input feature vector (shape: [n_features])
            feature_names: List of feature names
            feature_values: Optional dict of feature name -> value for interpretation
            
        Returns:
            Explanation with top features and contributions
            
        Requirements: 7.1, 7.2, 7.3
        """
        start_time = time.perf_counter()
        
        try:
            # Compute SHAP values with timeout
            shap_values = await asyncio.wait_for(
                self._compute_shap_values(feature_vector),
                timeout=self.timeout_ms / 1000.0
            )
            
            # Get top K features
            top_features = self.get_top_features(
                shap_values,
                feature_names,
                feature_values or {},
                self.top_k
            )
            
            # Calculate total contribution
            total_contribution_pct = sum(f.contribution_pct for f in top_features)
            
            # Format explanation text
            formatted_text = self.format_explanation(top_features)
            
            computation_time_ms = (time.perf_counter() - start_time) * 1000
            
            return Explanation(
                top_features=top_features,
                total_contribution_pct=total_contribution_pct,
                formatted_text=formatted_text,
                computation_time_ms=computation_time_ms
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"SHAP computation exceeded timeout of {self.timeout_ms}ms")
            computation_time_ms = (time.perf_counter() - start_time) * 1000
            raise TimeoutError(f"SHAP computation timeout after {computation_time_ms:.2f}ms")
        
        except Exception as e:
            logger.error(f"Error computing SHAP explanation: {e}")
            raise
    
    async def _compute_shap_values(self, feature_vector: np.ndarray) -> np.ndarray:
        """
        Compute SHAP values for feature vector.
        
        Args:
            feature_vector: Input feature vector
            
        Returns:
            Array of SHAP values
        """
        # For now, use a fast approximation method
        # In production, this would use TreeExplainer or optimized DeepExplainer
        
        if self._explainer is None:
            # Initialize explainer (cached)
            self._explainer = self._create_explainer()
        
        # Run SHAP computation
        # This is a placeholder - actual implementation would use shap library
        shap_values = await asyncio.to_thread(
            self._compute_shap_sync,
            feature_vector
        )
        
        return shap_values
    
    def _create_explainer(self):
        """Create SHAP explainer instance (cached)."""
        # Placeholder for actual SHAP explainer creation
        # In production: return shap.TreeExplainer(self.model, self.background_data)
        return None
    
    def _compute_shap_sync(self, feature_vector: np.ndarray) -> np.ndarray:
        """
        Synchronous SHAP computation (runs in thread pool).
        
        This is a placeholder implementation. In production, this would use:
        - shap.TreeExplainer for tree-based models (fastest)
        - shap.DeepExplainer for neural networks
        - Approximate methods for speed
        """
        # Placeholder: return random SHAP values for demonstration
        # In production: return self._explainer.shap_values(feature_vector)
        n_features = len(feature_vector)
        
        # Simulate realistic SHAP values (some features more important than others)
        shap_values = np.random.exponential(scale=0.1, size=n_features)
        shap_values *= np.random.choice([-1, 1], size=n_features)  # Add direction
        
        return shap_values
    
    def get_top_features(
        self,
        shap_values: np.ndarray,
        feature_names: List[str],
        feature_values: Dict[str, float],
        top_k: int = 5
    ) -> List[FeatureContribution]:
        """
        Extract top K contributing features.
        
        Args:
            shap_values: Array of SHAP values
            feature_names: List of feature names
            feature_values: Dict of feature name -> value
            top_k: Number of top features to return
            
        Returns:
            List of FeatureContribution sorted by absolute SHAP value
            
        Requirements: 7.2
        """
        # Get absolute SHAP values for ranking
        abs_shap_values = np.abs(shap_values)
        total_importance = abs_shap_values.sum()
        
        # Get top K indices
        top_k_indices = np.argsort(abs_shap_values)[-top_k:][::-1]
        
        # Create FeatureContribution objects
        top_features = []
        for idx in top_k_indices:
            feature_name = feature_names[idx]
            shap_value = float(shap_values[idx])
            abs_shap_value = abs(shap_value)
            contribution_pct = (abs_shap_value / total_importance) * 100 if total_importance > 0 else 0
            
            direction = "positive" if shap_value > 0 else "negative"
            feature_value = feature_values.get(feature_name, 0.0)
            
            interpretation = self._interpret_feature(
                feature_name,
                feature_value,
                shap_value
            )
            
            top_features.append(FeatureContribution(
                feature_name=feature_name,
                feature_value=feature_value,
                shap_value=shap_value,
                contribution_pct=contribution_pct,
                direction=direction,
                interpretation=interpretation
            ))
        
        return top_features
    
    def _interpret_feature(
        self,
        feature_name: str,
        feature_value: float,
        shap_value: float
    ) -> str:
        """
        Generate human-readable interpretation for a feature.
        
        Args:
            feature_name: Name of the feature
            feature_value: Value of the feature
            shap_value: SHAP value (contribution)
            
        Returns:
            Human-readable interpretation string
            
        Requirements: 7.3
        """
        # Check if we have interpretation template for this feature
        if feature_name in self.interpretation_templates:
            template = self.interpretation_templates[feature_name]
            
            # RSI interpretation
            if feature_name == "rsi_14":
                if feature_value > template["overbought"][0]:
                    return template["overbought"][1]
                elif feature_value < template["oversold"][0]:
                    return template["oversold"][1]
                else:
                    return template["neutral"]
            
            # MACD interpretation
            elif feature_name == "macd_histogram":
                if feature_value > 0:
                    return template["positive"]
                else:
                    return template["negative"]
            
            # Bollinger Band position
            elif feature_name == "bb_position":
                if feature_value > template["upper"][0]:
                    return template["upper"][1]
                elif feature_value < template["lower"][0]:
                    return template["lower"][1]
                else:
                    return template["middle"]
            
            # Volume ROC
            elif feature_name == "volume_roc":
                if feature_value > template["high"][0]:
                    return template["high"][1]
                elif feature_value < template["low"][0]:
                    return template["low"][1]
                else:
                    return template["normal"]
            
            # ADX
            elif feature_name == "adx":
                if feature_value > template["very_strong"][0]:
                    return template["very_strong"][1]
                elif feature_value > template["strong"][0]:
                    return template["strong"][1]
                else:
                    return template["weak"][1]
        
        # Default interpretation
        direction = "bullish" if shap_value > 0 else "bearish"
        return f"{feature_name.replace('_', ' ').title()} ({direction} signal)"
    
    def format_explanation(self, contributions: List[FeatureContribution]) -> str:
        """
        Format explanation as human-readable text.
        
        Args:
            contributions: List of feature contributions
            
        Returns:
            Formatted explanation string
            
        Example: "RSI oversold (30%), MACD bullish (25%), Volume spike (20%)"
        
        Requirements: 7.3
        """
        parts = []
        for contrib in contributions:
            parts.append(f"{contrib.interpretation} ({contrib.contribution_pct:.0f}%)")
        
        return ", ".join(parts)
    
    async def explain_batch(
        self,
        feature_vectors: np.ndarray,
        feature_names: List[str]
    ) -> List[Explanation]:
        """
        Generate SHAP explanations for a batch of predictions.
        
        Args:
            feature_vectors: Batch of feature vectors (shape: [n_samples, n_features])
            feature_names: List of feature names
            
        Returns:
            List of Explanation objects
        """
        explanations = []
        
        for feature_vector in feature_vectors:
            try:
                explanation = await self.explain(feature_vector, feature_names)
                explanations.append(explanation)
            except Exception as e:
                logger.error(f"Error explaining prediction: {e}")
                # Return empty explanation on error
                explanations.append(Explanation(
                    top_features=[],
                    total_contribution_pct=0.0,
                    formatted_text="Explanation unavailable",
                    computation_time_ms=0.0
                ))
        
        return explanations
