"""
Feature Validation Module

Validates computed features for data quality, correctness, and ML readiness.
Production-grade validation with comprehensive checks.

Author: Cortex AI Team
Date: 2026-04-12
"""

import pandas as pd
import numpy as np
from typing import Tuple, List, Dict, Optional
import logging

logger = logging.getLogger(__name__)


def validate_features(
    df: pd.DataFrame,
    feature_cols: Optional[List[str]] = None,
    strict: bool = True
) -> Tuple[bool, List[str]]:
    """
    Comprehensive feature validation
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns to validate (if None, auto-detect)
        strict: If True, fail on warnings; if False, only fail on errors
        
    Returns:
        Tuple of (is_valid, list of error/warning messages)
    """
    errors = []
    warnings = []
    
    # Auto-detect feature columns if not provided
    if feature_cols is None:
        exclude_cols = ['timestamp', 'symbol', 'target', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    logger.info(f"Validating {len(feature_cols)} features across {len(df)} rows")
    
    # Check 1: Missing columns
    missing_cols = [col for col in feature_cols if col not in df.columns]
    if missing_cols:
        errors.append(f"Missing feature columns: {missing_cols}")
    
    # Check 2: NaN values
    nan_check = check_missing_values(df, feature_cols)
    if nan_check['has_nans']:
        if nan_check['max_nan_pct'] > 50:
            errors.append(f"Excessive NaN values: {nan_check['columns_with_nans']}")
        else:
            warnings.append(f"Some NaN values present: {nan_check['columns_with_nans']}")
    
    # Check 3: Infinite values
    inf_check = check_infinite_values(df, feature_cols)
    if inf_check['has_infs']:
        errors.append(f"Infinite values found: {inf_check['columns_with_infs']}")
    
    # Check 4: Constant features
    const_check = check_constant_features(df, feature_cols)
    if const_check['has_constants']:
        warnings.append(f"Constant features (no variance): {const_check['constant_columns']}")
    
    # Check 5: Feature ranges
    range_check = check_feature_ranges(df, feature_cols)
    if range_check['has_outliers']:
        warnings.append(f"Features with extreme values: {range_check['outlier_columns']}")
    
    # Check 6: Data types
    dtype_check = check_data_types(df, feature_cols)
    if dtype_check['has_issues']:
        errors.append(f"Invalid data types: {dtype_check['invalid_columns']}")
    
    # Combine errors and warnings
    all_issues = errors + (warnings if strict else [])
    is_valid = len(errors) == 0 and (not strict or len(warnings) == 0)
    
    if is_valid:
        logger.info("✓ All feature validations passed")
    else:
        logger.error(f"✗ Feature validation failed: {len(errors)} errors, {len(warnings)} warnings")
    
    return is_valid, all_issues


def check_missing_values(
    df: pd.DataFrame,
    feature_cols: List[str]
) -> Dict[str, any]:
    """
    Check for missing (NaN) values in features
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns
        
    Returns:
        Dict with NaN statistics
    """
    nan_counts = {}
    nan_pcts = {}
    
    for col in feature_cols:
        if col in df.columns:
            nan_count = df[col].isna().sum()
            if nan_count > 0:
                nan_counts[col] = int(nan_count)
                nan_pcts[col] = float(nan_count / len(df) * 100)
    
    result = {
        'has_nans': len(nan_counts) > 0,
        'columns_with_nans': list(nan_counts.keys()),
        'nan_counts': nan_counts,
        'nan_percentages': nan_pcts,
        'max_nan_pct': max(nan_pcts.values()) if nan_pcts else 0
    }
    
    if result['has_nans']:
        logger.warning(f"Found NaN values in {len(nan_counts)} columns")
    
    return result


def check_infinite_values(
    df: pd.DataFrame,
    feature_cols: List[str]
) -> Dict[str, any]:
    """
    Check for infinite values in features
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns
        
    Returns:
        Dict with infinity statistics
    """
    inf_counts = {}
    
    for col in feature_cols:
        if col in df.columns:
            inf_count = df[col].isin([np.inf, -np.inf]).sum()
            if inf_count > 0:
                inf_counts[col] = int(inf_count)
    
    result = {
        'has_infs': len(inf_counts) > 0,
        'columns_with_infs': list(inf_counts.keys()),
        'inf_counts': inf_counts
    }
    
    if result['has_infs']:
        logger.error(f"Found infinite values in {len(inf_counts)} columns")
    
    return result


def check_constant_features(
    df: pd.DataFrame,
    feature_cols: List[str],
    std_threshold: float = 1e-6
) -> Dict[str, any]:
    """
    Check for constant (zero variance) features
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns
        std_threshold: Minimum standard deviation threshold
        
    Returns:
        Dict with constant feature information
    """
    constant_cols = []
    std_values = {}
    
    for col in feature_cols:
        if col in df.columns:
            std = df[col].std()
            if pd.notna(std) and std < std_threshold:
                constant_cols.append(col)
                std_values[col] = float(std)
    
    result = {
        'has_constants': len(constant_cols) > 0,
        'constant_columns': constant_cols,
        'std_values': std_values
    }
    
    if result['has_constants']:
        logger.warning(f"Found {len(constant_cols)} constant features")
    
    return result


def check_feature_ranges(
    df: pd.DataFrame,
    feature_cols: List[str],
    outlier_threshold: float = 10.0
) -> Dict[str, any]:
    """
    Check feature value ranges for extreme outliers
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns
        outlier_threshold: Threshold for extreme values (after normalization)
        
    Returns:
        Dict with range statistics
    """
    outlier_cols = []
    ranges = {}
    
    for col in feature_cols:
        if col in df.columns:
            min_val = df[col].min()
            max_val = df[col].max()
            
            # Check for extreme values (assuming normalized features)
            if abs(min_val) > outlier_threshold or abs(max_val) > outlier_threshold:
                outlier_cols.append(col)
                ranges[col] = {'min': float(min_val), 'max': float(max_val)}
    
    result = {
        'has_outliers': len(outlier_cols) > 0,
        'outlier_columns': outlier_cols,
        'ranges': ranges
    }
    
    if result['has_outliers']:
        logger.warning(f"Found extreme values in {len(outlier_cols)} columns")
    
    return result


def check_data_types(
    df: pd.DataFrame,
    feature_cols: List[str]
) -> Dict[str, any]:
    """
    Check that all features have numeric data types
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns
        
    Returns:
        Dict with data type information
    """
    invalid_cols = []
    dtypes = {}
    
    for col in feature_cols:
        if col in df.columns:
            dtype = df[col].dtype
            if not np.issubdtype(dtype, np.number):
                invalid_cols.append(col)
                dtypes[col] = str(dtype)
    
    result = {
        'has_issues': len(invalid_cols) > 0,
        'invalid_columns': invalid_cols,
        'dtypes': dtypes
    }
    
    if result['has_issues']:
        logger.error(f"Found non-numeric columns: {invalid_cols}")
    
    return result


def validate_feature_count(
    df: pd.DataFrame,
    expected_count: int,
    feature_cols: Optional[List[str]] = None
) -> Tuple[bool, str]:
    """
    Validate that DataFrame has expected number of features
    
    Args:
        df: DataFrame with features
        expected_count: Expected number of features
        feature_cols: List of feature columns (if None, auto-detect)
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if feature_cols is None:
        exclude_cols = ['timestamp', 'symbol', 'target', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    actual_count = len(feature_cols)
    
    if actual_count != expected_count:
        error = f"Feature count mismatch: expected {expected_count}, got {actual_count}"
        logger.error(error)
        return False, error
    
    logger.info(f"✓ Feature count validated: {actual_count} features")
    return True, ""


def validate_sample_count(
    df: pd.DataFrame,
    min_samples: int = 100
) -> Tuple[bool, str]:
    """
    Validate that DataFrame has sufficient samples
    
    Args:
        df: DataFrame with features
        min_samples: Minimum required samples
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    actual_samples = len(df)
    
    if actual_samples < min_samples:
        error = f"Insufficient samples: expected >={min_samples}, got {actual_samples}"
        logger.error(error)
        return False, error
    
    logger.info(f"✓ Sample count validated: {actual_samples} samples")
    return True, ""


def get_feature_statistics(
    df: pd.DataFrame,
    feature_cols: Optional[List[str]] = None
) -> pd.DataFrame:
    """
    Get comprehensive statistics for all features
    
    Args:
        df: DataFrame with features
        feature_cols: List of feature columns (if None, auto-detect)
        
    Returns:
        DataFrame with statistics for each feature
    """
    if feature_cols is None:
        exclude_cols = ['timestamp', 'symbol', 'target', 'open', 'high', 'low', 'close', 'volume']
        feature_cols = [col for col in df.columns if col not in exclude_cols]
    
    stats = []
    
    for col in feature_cols:
        if col in df.columns:
            stats.append({
                'feature': col,
                'count': int(df[col].count()),
                'mean': float(df[col].mean()),
                'std': float(df[col].std()),
                'min': float(df[col].min()),
                'max': float(df[col].max()),
                'nan_count': int(df[col].isna().sum()),
                'nan_pct': float(df[col].isna().sum() / len(df) * 100)
            })
    
    stats_df = pd.DataFrame(stats)
    
    logger.info(f"Generated statistics for {len(stats_df)} features")
    
    return stats_df
