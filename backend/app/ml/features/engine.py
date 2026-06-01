"""
Cortex AI — Feature Computation Engine
========================================
Orchestrates feature computation from OHLCV data with validation and error handling.

The FeatureEngine is responsible for:
- Computing all 42 technical indicators from OHLCV data
- Validating OHLCV data integrity (high >= low, close in [low, high])
- Handling missing data with forward-fill (max 3 consecutive values)
- Rejecting datasets with >5% missing data
- Logging validation errors with detailed diagnostics
- Graceful degradation for failed feature computations
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

from app.ml.features.technical_indicators import FEATURE_FUNCTIONS
from app.ml.config import FEATURE_DEFINITIONS


logger = logging.getLogger(__name__)


class FeatureEngine:
    """
    Feature computation engine that orchestrates all feature calculations.
    
    This class provides a clean interface for computing all technical indicators
    from OHLCV data while ensuring data quality and handling errors gracefully.
    """

    def __init__(self):
        """Initialize the feature engine with registered feature functions."""
        self.feature_functions = FEATURE_FUNCTIONS
        self.feature_definitions = FEATURE_DEFINITIONS
        
        # Validate that all defined features have computation functions
        missing_functions = set(FEATURE_DEFINITIONS.keys()) - set(FEATURE_FUNCTIONS.keys())
        if missing_functions:
            logger.warning(
                f"Feature definitions without computation functions: {missing_functions}"
            )

    def compute_all_features(
        self,
        df: pd.DataFrame,
        validate: bool = True,
    ) -> dict[str, float]:
        """
        Compute all technical indicators from OHLCV data.

        Args:
            df: DataFrame with OHLCV columns (open, high, low, close, volume)
            validate: Whether to validate OHLCV data before computation (default: True)

        Returns:
            Dictionary mapping feature names to computed values

        Raises:
            ValueError: If OHLCV data validation fails
        """
        # Validate OHLCV data if requested
        if validate:
            self.validate_ohlcv_data(df)

        # Compute all features
        feature_vector: dict[str, float] = {}
        failed_features: list[str] = []

        for feature_name, compute_fn in self.feature_functions.items():
            try:
                value = compute_fn(df)
                
                # Check for NaN values
                if pd.isna(value):
                    logger.debug(
                        f"Feature '{feature_name}' returned NaN "
                        f"(likely insufficient data: {len(df)} rows)"
                    )
                    value = 0.0  # Default to 0.0 for missing features
                
                feature_vector[feature_name] = float(value)
                
            except Exception as e:
                logger.error(
                    f"Failed to compute feature '{feature_name}': {e}",
                    exc_info=True
                )
                failed_features.append(feature_name)
                feature_vector[feature_name] = 0.0  # Graceful degradation

        # Log summary
        if failed_features:
            logger.warning(
                f"Failed to compute {len(failed_features)}/{len(self.feature_functions)} "
                f"features: {failed_features}"
            )
        else:
            logger.debug(
                f"Successfully computed all {len(feature_vector)} features"
            )

        return feature_vector

    def validate_ohlcv_data(self, df: pd.DataFrame) -> None:
        """
        Validate OHLCV data integrity and reject invalid datasets.

        Validation rules:
        1. Required columns: open, high, low, close, volume
        2. High >= Low for all rows
        3. Close in [Low, High] for all rows
        4. No more than 5% missing data
        5. No more than 3 consecutive missing values (forward-fill limit)

        Args:
            df: DataFrame with OHLCV columns

        Raises:
            ValueError: If validation fails with detailed diagnostics
        """
        # Check required columns
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            raise ValueError(
                f"Missing required OHLCV columns: {missing_cols}. "
                f"Available columns: {list(df.columns)}"
            )

        # Check for empty dataframe
        if len(df) == 0:
            raise ValueError("OHLCV dataframe is empty")

        # Validate High >= Low
        invalid_high_low = df[df['high'] < df['low']]
        if len(invalid_high_low) > 0:
            indices = invalid_high_low.index.tolist()[:5]  # Show first 5
            raise ValueError(
                f"Found {len(invalid_high_low)} rows where High < Low. "
                f"First invalid indices: {indices}"
            )

        # Validate Close in [Low, High]
        invalid_close = df[(df['close'] < df['low']) | (df['close'] > df['high'])]
        if len(invalid_close) > 0:
            indices = invalid_close.index.tolist()[:5]  # Show first 5
            raise ValueError(
                f"Found {len(invalid_close)} rows where Close not in [Low, High]. "
                f"First invalid indices: {indices}"
            )

        # Check missing data percentage
        total_values = len(df) * len(required_cols)
        missing_values = df[required_cols].isna().sum().sum()
        missing_pct = (missing_values / total_values) * 100

        if missing_pct > 5.0:
            raise ValueError(
                f"Dataset has {missing_pct:.2f}% missing data (threshold: 5%). "
                f"Missing values: {missing_values}/{total_values}"
            )

        # Check for consecutive missing values (>3 consecutive)
        for col in required_cols:
            consecutive_missing = self._max_consecutive_missing(df[col])
            if consecutive_missing > 3:
                raise ValueError(
                    f"Column '{col}' has {consecutive_missing} consecutive missing values "
                    f"(max allowed: 3). Forward-fill cannot handle this gap."
                )

        logger.debug(
            f"OHLCV validation passed: {len(df)} rows, "
            f"{missing_pct:.2f}% missing data"
        )

    def _max_consecutive_missing(self, series: pd.Series) -> int:
        """
        Calculate the maximum number of consecutive missing values in a series.

        Args:
            series: Pandas Series to check

        Returns:
            Maximum consecutive missing count
        """
        is_missing = series.isna()
        
        # If no missing values, return 0
        if not is_missing.any():
            return 0

        # Find consecutive groups of missing values
        groups = (is_missing != is_missing.shift()).cumsum()
        consecutive_counts = is_missing.groupby(groups).sum()
        
        return int(consecutive_counts.max())

    def handle_missing_data(
        self,
        df: pd.DataFrame,
        method: str = 'ffill',
        limit: int = 3
    ) -> pd.DataFrame:
        """
        Handle missing data in OHLCV dataframe.

        Args:
            df: DataFrame with OHLCV columns
            method: Fill method ('ffill' for forward-fill, 'bfill' for backward-fill)
            limit: Maximum number of consecutive NaN values to fill

        Returns:
            DataFrame with missing values filled

        Raises:
            ValueError: If missing data cannot be handled within limits
        """
        # Validate before filling
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        
        # Check if we can handle the missing data
        for col in required_cols:
            consecutive_missing = self._max_consecutive_missing(df[col])
            if consecutive_missing > limit:
                raise ValueError(
                    f"Cannot handle missing data in '{col}': "
                    f"{consecutive_missing} consecutive missing values exceeds limit of {limit}"
                )

        # Fill missing values
        df_filled = df.copy()
        
        if method == 'ffill':
            df_filled[required_cols] = df_filled[required_cols].fillna(method='ffill', limit=limit)
        elif method == 'bfill':
            df_filled[required_cols] = df_filled[required_cols].fillna(method='bfill', limit=limit)
        else:
            raise ValueError(f"Unknown fill method: {method}. Use 'ffill' or 'bfill'")

        # Check if any missing values remain
        remaining_missing = df_filled[required_cols].isna().sum().sum()
        if remaining_missing > 0:
            logger.warning(
                f"After {method} with limit={limit}, {remaining_missing} missing values remain"
            )

        return df_filled

    def get_feature_metadata(self) -> dict[str, dict[str, Any]]:
        """
        Get metadata for all registered features.

        Returns:
            Dictionary mapping feature names to their metadata
        """
        return self.feature_definitions.copy()

    def get_feature_count(self) -> int:
        """
        Get the total number of registered features.

        Returns:
            Number of features
        """
        return len(self.feature_functions)


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_feature_engine_instance: FeatureEngine | None = None


def get_feature_engine() -> FeatureEngine:
    """
    Get the singleton FeatureEngine instance.

    Returns:
        FeatureEngine instance
    """
    global _feature_engine_instance
    
    if _feature_engine_instance is None:
        _feature_engine_instance = FeatureEngine()
    
    return _feature_engine_instance
