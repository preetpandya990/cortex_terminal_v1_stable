"""
Walk-Forward Validation Module
===============================
.. deprecated::
    :class:`WalkForwardSplitter` is a decorative stub — it produced date-range
    splits that were never wired into model training or evaluation, so it has
    no effect on any trained model.  It is retained here only for any external
    callers that import the :class:`Split` dataclass.

    **Use** :class:`app.ml.evaluation.cpcv.PanelPurgedCPCV` instead.
    ``PanelPurgedCPCV`` implements López de Prado's Combinatorial Purged CV:
    it operates on the global unique-timestamp axis of the panel, enforces a
    label-horizon purge + embargo, and yields φ coherent OOS backtest paths
    for Deflated-Sharpe / PBO estimation.  It replaced
    ``WalkForwardSplitter`` as part of Workstream A2.
"""

import warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Optional
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class Split:
    """Single walk-forward split"""
    train_start: datetime
    train_end: datetime
    val_start: datetime
    val_end: datetime
    test_start: datetime
    test_end: datetime
    split_id: int


class WalkForwardSplitter:
    """Expanding-window walk-forward splitter — **DEPRECATED**.

    This class is retained for backward compatibility only.  It was never
    connected to the production training pipeline (splits were computed but
    not used for model selection or evaluation).

    Use :class:`app.ml.evaluation.cpcv.PanelPurgedCPCV` for all new code.
    ``PanelPurgedCPCV`` provides Combinatorial Purged CV with label-horizon
    purge, embargo, and φ OOS backtest paths — the correct primitive for
    leakage-free financial ML evaluation.
    """

    def __init__(
        self,
        initial_train_days: int = 730,
        validation_days: int = 90,
        test_days: int = 30,
        step_days: int = 30
    ):
        """
        Initialize walk-forward splitter
        
        Args:
            initial_train_days: Initial training window (default 2 years)
            validation_days: Validation window (default 3 months)
            test_days: Test window (default 1 month)
            step_days: Step size between splits (default 1 month)
        """
        warnings.warn(
            "WalkForwardSplitter is deprecated and has no effect on trained "
            "models. Use app.ml.evaluation.cpcv.PanelPurgedCPCV instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.initial_train_days = initial_train_days
        self.validation_days = validation_days
        self.test_days = test_days
        self.step_days = step_days

        logger.info(
            f"WalkForwardSplitter initialized: "
            f"train={initial_train_days}d, val={validation_days}d, "
            f"test={test_days}d, step={step_days}d"
        )
    
    def create_splits(
        self,
        data: pd.DataFrame,
        date_column: str = 'timestamp',
        n_splits: Optional[int] = None
    ) -> List[Split]:
        """
        Create walk-forward splits from data
        
        Args:
            data: DataFrame with time series data
            date_column: Name of date column
            n_splits: Number of splits (None = maximum possible)
            
        Returns:
            List of Split objects
        """
        if date_column not in data.columns:
            raise ValueError(f"Column '{date_column}' not found in data")
        
        # Ensure sorted by date
        data = data.sort_values(date_column).reset_index(drop=True)
        
        # Get date range
        min_date = data[date_column].min()
        max_date = data[date_column].max()
        
        logger.info(f"Data range: {min_date} to {max_date}")
        
        # Calculate splits
        splits = []
        split_id = 0
        
        # First split starts with initial training window
        current_train_end = min_date + timedelta(days=self.initial_train_days)
        
        while True:
            # Validation period
            val_start = current_train_end + timedelta(days=1)
            val_end = val_start + timedelta(days=self.validation_days - 1)
            
            # Test period
            test_start = val_end + timedelta(days=1)
            test_end = test_start + timedelta(days=self.test_days - 1)
            
            # Check if we have enough data
            if test_end > max_date:
                break
            
            # Create split
            split = Split(
                train_start=min_date,
                train_end=current_train_end,
                val_start=val_start,
                val_end=val_end,
                test_start=test_start,
                test_end=test_end,
                split_id=split_id
            )
            
            splits.append(split)
            split_id += 1
            
            # Check if we've reached desired number of splits
            if n_splits and len(splits) >= n_splits:
                break
            
            # Move forward by step size (expanding window)
            current_train_end += timedelta(days=self.step_days)
        
        logger.info(f"Created {len(splits)} walk-forward splits")
        
        return splits
    
    def get_split_data(
        self,
        data: pd.DataFrame,
        split: Split,
        date_column: str = 'timestamp'
    ) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """
        Get train, validation, and test data for a split
        
        Args:
            data: Full dataset
            split: Split object
            date_column: Name of date column
            
        Returns:
            (train_data, val_data, test_data)
        """
        train_data = data[
            (data[date_column] >= split.train_start) &
            (data[date_column] <= split.train_end)
        ].copy()
        
        val_data = data[
            (data[date_column] >= split.val_start) &
            (data[date_column] <= split.val_end)
        ].copy()
        
        test_data = data[
            (data[date_column] >= split.test_start) &
            (data[date_column] <= split.test_end)
        ].copy()
        
        logger.info(
            f"Split {split.split_id}: "
            f"train={len(train_data)}, val={len(val_data)}, test={len(test_data)}"
        )
        
        return train_data, val_data, test_data


def create_walk_forward_splits(
    data: pd.DataFrame,
    date_column: str = 'timestamp',
    initial_train_days: int = 730,
    validation_days: int = 90,
    test_days: int = 30,
    step_days: int = 30,
    n_splits: Optional[int] = None
) -> List[Split]:
    """
    Convenience function to create walk-forward splits
    
    Args:
        data: DataFrame with time series data
        date_column: Name of date column
        initial_train_days: Initial training window
        validation_days: Validation window
        test_days: Test window
        step_days: Step size between splits
        n_splits: Number of splits (None = maximum)
        
    Returns:
        List of Split objects
    """
    splitter = WalkForwardSplitter(
        initial_train_days=initial_train_days,
        validation_days=validation_days,
        test_days=test_days,
        step_days=step_days
    )
    
    return splitter.create_splits(data, date_column, n_splits)


def validate_split(split: Split) -> bool:
    """
    Validate that a split has proper date ordering
    
    Args:
        split: Split object to validate
        
    Returns:
        True if valid, False otherwise
    """
    checks = [
        split.train_start < split.train_end,
        split.train_end < split.val_start,
        split.val_start <= split.val_end,
        split.val_end < split.test_start,
        split.test_start <= split.test_end
    ]
    
    if not all(checks):
        logger.error(f"Invalid split {split.split_id}: date ordering violated")
        return False
    
    return True


def print_split_summary(splits: List[Split]) -> None:
    """
    Print summary of walk-forward splits
    
    Args:
        splits: List of Split objects
    """
    print(f"\n{'='*80}")
    print(f"Walk-Forward Validation Summary: {len(splits)} splits")
    print(f"{'='*80}\n")
    
    for split in splits:
        train_days = (split.train_end - split.train_start).days
        val_days = (split.val_end - split.val_start).days + 1
        test_days = (split.test_end - split.test_start).days + 1
        
        print(f"Split {split.split_id}:")
        print(f"  Train: {split.train_start.date()} to {split.train_end.date()} ({train_days} days)")
        print(f"  Val:   {split.val_start.date()} to {split.val_end.date()} ({val_days} days)")
        print(f"  Test:  {split.test_start.date()} to {split.test_end.date()} ({test_days} days)")
        print()
    
    print(f"{'='*80}\n")
