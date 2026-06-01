"""
Cortex AI — Data Validation Module
====================================
Validates OHLCV data quality for ML training pipeline.

Validation checks:
- OHLCV constraints (high >= low, close in [low, high])
- Data completeness (reject if >5% missing)
- Outlier detection (flag extreme price movements)
- Temporal consistency (no future data leakage)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)


class DataValidator:
    """
    Data validator ensuring OHLCV data quality for ML training.
    
    This class performs comprehensive validation checks to ensure
    training data meets quality standards and prevents model degradation.
    """

    def __init__(
        self,
        max_missing_pct: float = 5.0,
        outlier_std_threshold: float = 5.0,
    ):
        """
        Initialize data validator.

        Args:
            max_missing_pct: Maximum allowed missing data percentage (default: 5.0%)
            outlier_std_threshold: Standard deviations for outlier detection (default: 5.0)
        """
        self.max_missing_pct = max_missing_pct
        self.outlier_std_threshold = outlier_std_threshold

    def validate_ohlcv(self, df: pd.DataFrame) -> tuple[bool, list[str]]:
        """
        Validate OHLCV data constraints.

        Checks:
        1. Required columns present (open, high, low, close, volume)
        2. High >= Low for all rows
        3. Close in [Low, High] for all rows
        4. No negative prices or volumes
        5. Timestamps are monotonically increasing

        Args:
            df: DataFrame with OHLCV columns

        Returns:
            Tuple of (is_valid, error_messages)

        Example:
            >>> validator = DataValidator()
            >>> is_valid, errors = validator.validate_ohlcv(df)
            >>> if not is_valid:
            ...     print(f"Validation failed: {errors}")
        """
        errors = []

        # Check required columns
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            errors.append(f"Missing required columns: {missing_cols}")
            return False, errors

        # Check for empty dataframe
        if len(df) == 0:
            errors.append("DataFrame is empty")
            return False, errors

        # Validate High >= Low
        invalid_high_low = df[df['high'] < df['low']]
        if len(invalid_high_low) > 0:
            errors.append(
                f"Found {len(invalid_high_low)} rows where High < Low "
                f"(indices: {invalid_high_low.index.tolist()[:5]})"
            )

        # Validate Close in [Low, High]
        invalid_close = df[(df['close'] < df['low']) | (df['close'] > df['high'])]
        if len(invalid_close) > 0:
            errors.append(
                f"Found {len(invalid_close)} rows where Close not in [Low, High] "
                f"(indices: {invalid_close.index.tolist()[:5]})"
            )

        # Validate Open in [Low, High]
        invalid_open = df[(df['open'] < df['low']) | (df['open'] > df['high'])]
        if len(invalid_open) > 0:
            errors.append(
                f"Found {len(invalid_open)} rows where Open not in [Low, High] "
                f"(indices: {invalid_open.index.tolist()[:5]})"
            )

        # Check for negative prices
        negative_prices = df[
            (df['open'] < 0) | (df['high'] < 0) | (df['low'] < 0) | (df['close'] < 0)
        ]
        if len(negative_prices) > 0:
            errors.append(
                f"Found {len(negative_prices)} rows with negative prices "
                f"(indices: {negative_prices.index.tolist()[:5]})"
            )

        # Check for negative volumes
        negative_volumes = df[df['volume'] < 0]
        if len(negative_volumes) > 0:
            errors.append(
                f"Found {len(negative_volumes)} rows with negative volumes "
                f"(indices: {negative_volumes.index.tolist()[:5]})"
            )

        # Check for zero prices (suspicious)
        zero_prices = df[
            (df['open'] == 0) | (df['high'] == 0) | (df['low'] == 0) | (df['close'] == 0)
        ]
        if len(zero_prices) > 0:
            logger.warning(
                f"Found {len(zero_prices)} rows with zero prices "
                f"(indices: {zero_prices.index.tolist()[:5]})"
            )

        # Check timestamp ordering (if timestamp column exists)
        if 'timestamp' in df.columns:
            if not df['timestamp'].is_monotonic_increasing:
                errors.append("Timestamps are not monotonically increasing")

        is_valid = len(errors) == 0
        
        if is_valid:
            logger.debug(f"OHLCV validation passed for {len(df)} rows")
        else:
            logger.error(f"OHLCV validation failed: {errors}")

        return is_valid, errors

    def check_completeness(self, df: pd.DataFrame) -> tuple[bool, dict[str, Any]]:
        """
        Check data completeness and reject if >5% missing.

        Args:
            df: DataFrame with OHLCV columns

        Returns:
            Tuple of (is_complete, statistics_dict)

        Example:
            >>> validator = DataValidator(max_missing_pct=5.0)
            >>> is_complete, stats = validator.check_completeness(df)
            >>> print(f"Missing: {stats['missing_pct']:.2f}%")
        """
        required_cols = ['open', 'high', 'low', 'close', 'volume']
        
        # Calculate missing data statistics
        total_values = len(df) * len(required_cols)
        missing_values = df[required_cols].isna().sum().sum()
        missing_pct = (missing_values / total_values) * 100

        # Per-column missing counts
        missing_per_col = df[required_cols].isna().sum().to_dict()

        # Consecutive missing values
        max_consecutive = {}
        for col in required_cols:
            max_consecutive[col] = self._max_consecutive_missing(df[col])

        stats = {
            "total_rows": len(df),
            "total_values": total_values,
            "missing_values": int(missing_values),
            "missing_pct": float(missing_pct),
            "missing_per_column": missing_per_col,
            "max_consecutive_missing": max_consecutive,
            "threshold_pct": self.max_missing_pct,
        }

        is_complete = missing_pct <= self.max_missing_pct

        if is_complete:
            logger.debug(
                f"Completeness check passed: {missing_pct:.2f}% missing "
                f"(threshold: {self.max_missing_pct}%)"
            )
        else:
            logger.error(
                f"Completeness check failed: {missing_pct:.2f}% missing "
                f"(threshold: {self.max_missing_pct}%)"
            )

        return is_complete, stats

    def detect_outliers(
        self,
        df: pd.DataFrame,
        method: str = "zscore",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        """
        Detect outliers in OHLCV data using statistical methods.

        Args:
            df: DataFrame with OHLCV columns
            method: Detection method ("zscore" or "iqr")

        Returns:
            Tuple of (outlier_dataframe, statistics_dict)

        Example:
            >>> validator = DataValidator(outlier_std_threshold=5.0)
            >>> outliers, stats = validator.detect_outliers(df, method="zscore")
            >>> print(f"Found {len(outliers)} outliers")
        """
        price_cols = ['open', 'high', 'low', 'close']
        
        if method == "zscore":
            outliers = self._detect_outliers_zscore(df, price_cols)
        elif method == "iqr":
            outliers = self._detect_outliers_iqr(df, price_cols)
        else:
            raise ValueError(f"Unknown outlier detection method: {method}")

        # Calculate statistics
        stats = {
            "method": method,
            "total_rows": len(df),
            "outlier_rows": len(outliers),
            "outlier_pct": (len(outliers) / len(df) * 100) if len(df) > 0 else 0.0,
            "threshold": self.outlier_std_threshold if method == "zscore" else "IQR",
        }

        if len(outliers) > 0:
            logger.warning(
                f"Detected {len(outliers)} outlier rows ({stats['outlier_pct']:.2f}%) "
                f"using {method} method"
            )
        else:
            logger.debug(f"No outliers detected using {method} method")

        return outliers, stats

    def _detect_outliers_zscore(
        self,
        df: pd.DataFrame,
        columns: list[str],
    ) -> pd.DataFrame:
        """
        Detect outliers using Z-score method.

        Flags rows where any price column has |z-score| > threshold.
        """
        # Calculate returns (percentage change)
        returns = df[columns].pct_change()

        # Calculate z-scores for returns
        z_scores = np.abs((returns - returns.mean()) / returns.std())

        # Flag rows with any z-score exceeding threshold
        outlier_mask = (z_scores > self.outlier_std_threshold).any(axis=1)

        return df[outlier_mask]

    def _detect_outliers_iqr(
        self,
        df: pd.DataFrame,
        columns: list[str],
    ) -> pd.DataFrame:
        """
        Detect outliers using Interquartile Range (IQR) method.

        Flags rows where any price column is outside [Q1 - 1.5*IQR, Q3 + 1.5*IQR].
        """
        # Calculate returns
        returns = df[columns].pct_change()

        outlier_mask = pd.Series(False, index=df.index)

        for col in columns:
            col_returns = returns[col].dropna()
            
            Q1 = col_returns.quantile(0.25)
            Q3 = col_returns.quantile(0.75)
            IQR = Q3 - Q1

            lower_bound = Q1 - 1.5 * IQR
            upper_bound = Q3 + 1.5 * IQR

            col_outliers = (returns[col] < lower_bound) | (returns[col] > upper_bound)
            outlier_mask = outlier_mask | col_outliers

        return df[outlier_mask]

    def _max_consecutive_missing(self, series: pd.Series) -> int:
        """
        Calculate maximum consecutive missing values in a series.

        Args:
            series: Pandas Series to check

        Returns:
            Maximum consecutive missing count
        """
        is_missing = series.isna()
        
        if not is_missing.any():
            return 0

        # Find consecutive groups
        groups = (is_missing != is_missing.shift()).cumsum()
        consecutive_counts = is_missing.groupby(groups).sum()
        
        return int(consecutive_counts.max())

    def validate_all(
        self,
        df: pd.DataFrame,
        check_outliers: bool = True,
    ) -> tuple[bool, dict[str, Any]]:
        """
        Run all validation checks on OHLCV data.

        Args:
            df: DataFrame with OHLCV columns
            check_outliers: Whether to check for outliers (default: True)

        Returns:
            Tuple of (is_valid, validation_report)

        Example:
            >>> validator = DataValidator()
            >>> is_valid, report = validator.validate_all(df)
            >>> if not is_valid:
            ...     print(f"Validation failed: {report['errors']}")
        """
        report = {
            "timestamp": datetime.now().isoformat(),
            "total_rows": len(df),
            "checks_passed": [],
            "checks_failed": [],
            "errors": [],
            "warnings": [],
        }

        # 1. OHLCV constraints validation
        ohlcv_valid, ohlcv_errors = self.validate_ohlcv(df)
        if ohlcv_valid:
            report["checks_passed"].append("ohlcv_constraints")
        else:
            report["checks_failed"].append("ohlcv_constraints")
            report["errors"].extend(ohlcv_errors)

        # 2. Completeness check
        is_complete, completeness_stats = self.check_completeness(df)
        report["completeness"] = completeness_stats
        if is_complete:
            report["checks_passed"].append("completeness")
        else:
            report["checks_failed"].append("completeness")
            report["errors"].append(
                f"Data completeness check failed: {completeness_stats['missing_pct']:.2f}% missing "
                f"(threshold: {self.max_missing_pct}%)"
            )

        # 3. Outlier detection (warning only, not a failure)
        if check_outliers:
            outliers, outlier_stats = self.detect_outliers(df, method="zscore")
            report["outliers"] = outlier_stats
            if len(outliers) > 0:
                report["warnings"].append(
                    f"Detected {len(outliers)} outlier rows ({outlier_stats['outlier_pct']:.2f}%)"
                )
            report["checks_passed"].append("outlier_detection")

        # Overall validation result
        is_valid = len(report["checks_failed"]) == 0

        if is_valid:
            logger.info(
                f"Data validation passed: {len(df)} rows, "
                f"{len(report['checks_passed'])} checks passed"
            )
        else:
            logger.error(
                f"Data validation failed: {len(report['checks_failed'])} checks failed, "
                f"{len(report['errors'])} errors"
            )

        return is_valid, report


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON INSTANCE
# ══════════════════════════════════════════════════════════════════════════════

_validator_instance: DataValidator | None = None


def get_data_validator(
    max_missing_pct: float = 5.0,
    outlier_std_threshold: float = 5.0,
) -> DataValidator:
    """
    Get the singleton DataValidator instance.

    Args:
        max_missing_pct: Maximum allowed missing data percentage
        outlier_std_threshold: Standard deviations for outlier detection

    Returns:
        DataValidator instance
    """
    global _validator_instance
    
    if _validator_instance is None:
        _validator_instance = DataValidator(
            max_missing_pct=max_missing_pct,
            outlier_std_threshold=outlier_std_threshold,
        )
    
    return _validator_instance
