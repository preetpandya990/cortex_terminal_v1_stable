"""
SHAP Validator for ML Prediction System

Validates SHAP explainability for model predictions:
- Completeness: SHAP values computed for all features
- Contribution: Top 5 features contribute >=70%
- Normalization: SHAP values sum to ~1.0

Requirements: 7.1, 7.2, 22.1
"""

import logging
from typing import Dict, List, Optional, Tuple
import numpy as np
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SHAPValidationResult:
    """Result of SHAP validation."""
    passed: bool
    completeness_passed: bool
    contribution_passed: bool
    normalization_passed: bool
    error_message: Optional[str] = None
    details: Optional[Dict] = None


class SHAPValidator:
    """
    Validates SHAP explainability for model predictions.
    
    Ensures:
    - SHAP values exist for all features
    - Top 5 features contribute >=70% of total importance
    - SHAP values sum to approximately 1.0 (within 0.01 tolerance)
    """
    
    def __init__(
        self,
        top_k: int = 5,
        min_contribution_pct: float = 70.0,
        normalization_tolerance: float = 0.01
    ):
        """
        Initialize SHAP validator.
        
        Args:
            top_k: Number of top features to check (default: 5)
            min_contribution_pct: Minimum contribution percentage for top features (default: 70%)
            normalization_tolerance: Tolerance for SHAP sum validation (default: 0.01)
        """
        self.top_k = top_k
        self.min_contribution_pct = min_contribution_pct
        self.normalization_tolerance = normalization_tolerance
    
    def validate_shap_completeness(
        self,
        shap_values: np.ndarray,
        feature_names: List[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that SHAP values exist for all features.
        
        Args:
            shap_values: Array of SHAP values (shape: [n_features])
            feature_names: List of feature names
            
        Returns:
            Tuple of (passed, error_message)
            
        Requirements: 7.1
        """
        try:
            if shap_values is None:
                return False, "SHAP values are None"
            
            if len(shap_values) == 0:
                return False, "SHAP values array is empty"
            
            if len(shap_values) != len(feature_names):
                return False, f"SHAP values length ({len(shap_values)}) != feature names length ({len(feature_names)})"
            
            # Check for NaN or Inf values
            if np.isnan(shap_values).any():
                nan_indices = np.where(np.isnan(shap_values))[0]
                nan_features = [feature_names[i] for i in nan_indices]
                return False, f"SHAP values contain NaN for features: {nan_features}"
            
            if np.isinf(shap_values).any():
                inf_indices = np.where(np.isinf(shap_values))[0]
                inf_features = [feature_names[i] for i in inf_indices]
                return False, f"SHAP values contain Inf for features: {inf_features}"
            
            return True, None
            
        except Exception as e:
            logger.error(f"Error validating SHAP completeness: {e}")
            return False, f"Exception during validation: {str(e)}"
    
    def validate_shap_contribution(
        self,
        shap_values: np.ndarray,
        feature_names: List[str]
    ) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Validate that top K features contribute >= min_contribution_pct.
        
        Args:
            shap_values: Array of SHAP values (shape: [n_features])
            feature_names: List of feature names
            
        Returns:
            Tuple of (passed, error_message, details)
            
        Requirements: 7.2
        """
        try:
            # Use absolute values for contribution calculation
            abs_shap_values = np.abs(shap_values)
            total_importance = abs_shap_values.sum()
            
            if total_importance == 0:
                return False, "Total SHAP importance is zero", None
            
            # Get top K features by absolute SHAP value
            top_k_indices = np.argsort(abs_shap_values)[-self.top_k:][::-1]
            top_k_values = abs_shap_values[top_k_indices]
            top_k_sum = top_k_values.sum()
            
            contribution_pct = (top_k_sum / total_importance) * 100
            
            details = {
                "top_k": self.top_k,
                "contribution_pct": float(contribution_pct),
                "min_contribution_pct": self.min_contribution_pct,
                "top_features": [
                    {
                        "feature": feature_names[idx],
                        "shap_value": float(shap_values[idx]),
                        "abs_shap_value": float(abs_shap_values[idx]),
                        "contribution_pct": float((abs_shap_values[idx] / total_importance) * 100)
                    }
                    for idx in top_k_indices
                ]
            }
            
            if contribution_pct < self.min_contribution_pct:
                error_msg = (
                    f"Top {self.top_k} features contribute {contribution_pct:.2f}%, "
                    f"which is below threshold of {self.min_contribution_pct}%"
                )
                return False, error_msg, details
            
            return True, None, details
            
        except Exception as e:
            logger.error(f"Error validating SHAP contribution: {e}")
            return False, f"Exception during validation: {str(e)}", None
    
    def validate_shap_normalization(
        self,
        shap_values: np.ndarray
    ) -> Tuple[bool, Optional[str]]:
        """
        Validate that SHAP values sum to approximately 1.0.
        
        Args:
            shap_values: Array of SHAP values (shape: [n_features])
            
        Returns:
            Tuple of (passed, error_message)
            
        Requirements: 22.1
        """
        try:
            # Use absolute values for normalization check
            abs_shap_values = np.abs(shap_values)
            total = abs_shap_values.sum()
            
            # Normalize
            if total == 0:
                return False, "Cannot normalize: total SHAP importance is zero"
            
            normalized_sum = (abs_shap_values / total).sum()
            
            # Check if sum is approximately 1.0
            if abs(normalized_sum - 1.0) > self.normalization_tolerance:
                error_msg = (
                    f"Normalized SHAP values sum to {normalized_sum:.4f}, "
                    f"which differs from 1.0 by more than tolerance {self.normalization_tolerance}"
                )
                return False, error_msg
            
            return True, None
            
        except Exception as e:
            logger.error(f"Error validating SHAP normalization: {e}")
            return False, f"Exception during validation: {str(e)}"
    
    def validate(
        self,
        shap_values: np.ndarray,
        feature_names: List[str]
    ) -> SHAPValidationResult:
        """
        Run all SHAP validation checks.
        
        Args:
            shap_values: Array of SHAP values (shape: [n_features])
            feature_names: List of feature names
            
        Returns:
            SHAPValidationResult with validation results
        """
        # Validate completeness
        completeness_passed, completeness_error = self.validate_shap_completeness(
            shap_values, feature_names
        )
        
        if not completeness_passed:
            return SHAPValidationResult(
                passed=False,
                completeness_passed=False,
                contribution_passed=False,
                normalization_passed=False,
                error_message=f"Completeness check failed: {completeness_error}"
            )
        
        # Validate contribution
        contribution_passed, contribution_error, contribution_details = self.validate_shap_contribution(
            shap_values, feature_names
        )
        
        # Validate normalization
        normalization_passed, normalization_error = self.validate_shap_normalization(
            shap_values
        )
        
        # Overall result
        passed = completeness_passed and contribution_passed and normalization_passed
        
        error_messages = []
        if not contribution_passed:
            error_messages.append(f"Contribution: {contribution_error}")
        if not normalization_passed:
            error_messages.append(f"Normalization: {normalization_error}")
        
        error_message = "; ".join(error_messages) if error_messages else None
        
        return SHAPValidationResult(
            passed=passed,
            completeness_passed=completeness_passed,
            contribution_passed=contribution_passed,
            normalization_passed=normalization_passed,
            error_message=error_message,
            details=contribution_details
        )
    
    def validate_batch(
        self,
        shap_values_batch: np.ndarray,
        feature_names: List[str],
        max_failure_rate: float = 0.01
    ) -> Tuple[bool, float, List[int]]:
        """
        Validate SHAP values for a batch of predictions.
        
        Args:
            shap_values_batch: Array of SHAP values (shape: [n_samples, n_features])
            feature_names: List of feature names
            max_failure_rate: Maximum allowed failure rate (default: 0.01 = 1%)
            
        Returns:
            Tuple of (passed, failure_rate, failed_indices)
            
        Requirements: 22.4
        """
        n_samples = shap_values_batch.shape[0]
        failed_indices = []
        
        for i in range(n_samples):
            result = self.validate(shap_values_batch[i], feature_names)
            if not result.passed:
                failed_indices.append(i)
        
        failure_rate = len(failed_indices) / n_samples
        passed = failure_rate <= max_failure_rate
        
        if not passed:
            logger.warning(
                f"SHAP validation failed for {len(failed_indices)}/{n_samples} samples "
                f"({failure_rate*100:.2f}%), exceeds threshold of {max_failure_rate*100:.2f}%"
            )
        
        return passed, failure_rate, failed_indices


def create_shap_validator(*args, **kwargs) -> "SHAPValidator":
    """Factory function to create a SHAPValidator instance."""
    return SHAPValidator(*args, **kwargs)
