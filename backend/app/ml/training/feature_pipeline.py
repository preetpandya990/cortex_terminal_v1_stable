"""
Cortex AI — Feature Engineering Pipeline
==========================================
Prepares training data by computing features and generating labels.

Pipeline stages:
1. Load OHLCV data from database
2. Compute 37 cross-sectional, scale-invariant features (single vectorised pass)
3. Generate binary UP/DOWN labels with ATR-adaptive dead zone
4. Create time-series sequences for GRU input
5. Train/test split with strict temporal ordering (no look-ahead)
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ml.features.ohlcv_features import (
    compute_cross_section_features,
    CROSS_SECTION_FEATURE_NAMES,
)
from app.ml.features.target_generator import ATR_MULTIPLIER_DEFAULT, create_target_variable
from app.ml.training.data_validator import get_data_validator


logger = logging.getLogger(__name__)


class FeaturePipeline:
    """
    Feature engineering pipeline for ML training.
    
    Orchestrates data loading, feature computation, sequence creation,
    and label generation for time-series prediction models.
    """

    def __init__(
        self,
        session: AsyncSession,
        sequence_length: int = 60,
        prediction_horizon: int = 5,
    ):
        """
        Initialize feature pipeline.

        Args:
            session: Database session for data loading
            sequence_length: Number of time steps in each sequence (default: 60)
            prediction_horizon: Days ahead to predict (default: 5)
        """
        self.session = session
        self.sequence_length = sequence_length
        self.prediction_horizon = prediction_horizon
        
        self.data_validator = get_data_validator()

    async def prepare_training_data(
        self,
        symbol: str,
        timeframe: str,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        validate: bool = True,
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """
        Prepare complete training dataset with features and labels.

        Args:
            symbol: Stock symbol to prepare data for
            timeframe: Timeframe (e.g., "1d", "1w")
            start_date: Start date for data (None = all available)
            end_date: End date for data (None = today)
            validate: Whether to validate data quality (default: True)

        Returns:
            Tuple of (feature_sequences, label_dict)
            - feature_sequences: shape (num_samples, sequence_length, num_features)
              num_features = 40 (CROSS_SECTION_FEATURE_NAMES)
            - label_dict: {
                "direction": (num_samples,),  # 1=UP, 0=DOWN  (dead-zone NaNs excluded)
              }

        Example:
            >>> pipeline = FeaturePipeline(session)
            >>> X, y = await pipeline.prepare_training_data("NSE_EQ|INE002A01018", "1d")
            >>> print(f"Features: {X.shape}, Direction labels: {y['direction'].shape}")
        """
        logger.info(
            f"Preparing training data for {symbol} [{timeframe}] "
            f"from {start_date} to {end_date}"
        )

        # 1. Load OHLCV data
        ohlcv_df = await self._load_ohlcv_data(
            symbol=symbol,
            timeframe=timeframe,
            start_date=start_date,
            end_date=end_date,
        )

        # 2. Validate data quality
        if validate:
            is_valid, report = self.data_validator.validate_all(ohlcv_df)
            if not is_valid:
                raise ValueError(
                    f"Data validation failed for {symbol}: {report['errors']}"
                )

        # 3. Compute features for all time steps
        features_df = self._compute_features_batch(ohlcv_df)

        # 4. Generate labels
        labels_df = self._generate_labels(ohlcv_df)

        # 5. Create sequences
        feature_sequences, label_dict = self.create_sequences(
            features_df=features_df,
            labels_df=labels_df,
        )

        logger.info(
            f"Prepared {len(feature_sequences)} training samples "
            f"with {features_df.shape[1]} features"
        )

        return feature_sequences, label_dict

    async def _load_ohlcv_data(
        self,
        symbol: str,
        timeframe: str,
        start_date: datetime | None,
        end_date: datetime | None,
    ) -> pd.DataFrame:
        """
        Load OHLCV data from database.

        Args:
            symbol: Stock symbol
            timeframe: Timeframe
            start_date: Start date filter
            end_date: End date filter

        Returns:
            DataFrame with OHLCV columns
        """
        # Production implementation: Query actual database
        from app.models.upstox_data import UpstoxOHLCV
        from sqlalchemy import and_, select
        
        logger.info(f"Loading OHLCV data for {symbol} [{timeframe}]")
        
        # Build query conditions
        conditions = [
            UpstoxOHLCV.instrument_key == symbol,
            UpstoxOHLCV.timeframe == timeframe.upper()
        ]
        
        if start_date:
            conditions.append(UpstoxOHLCV.timestamp >= start_date)
        if end_date:
            conditions.append(UpstoxOHLCV.timestamp <= end_date)
        
        # Execute query
        stmt = select(UpstoxOHLCV).where(and_(*conditions)).order_by(UpstoxOHLCV.timestamp)
        
        result = await self.session.execute(stmt)
        rows = result.scalars().all()
        
        if not rows:
            raise ValueError(f"No OHLCV data found for {symbol} [{timeframe}]")
        
        # Convert to DataFrame
        df = pd.DataFrame([{
            'timestamp': row.timestamp,
            'open': float(row.open),
            'high': float(row.high),
            'low': float(row.low),
            'close': float(row.close),
            'volume': int(row.volume)
        } for row in rows])
        
        # Ensure sorted by timestamp
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        # Validate OHLCV constraints
        df['high'] = df[['open', 'high', 'close']].max(axis=1)
        df['low'] = df[['open', 'low', 'close']].min(axis=1)
        
        logger.info(f"Loaded {len(df)} OHLCV records for {symbol}")
        
        return df

    def _compute_features_batch(self, ohlcv_df: pd.DataFrame) -> pd.DataFrame:
        """
        Compute 37 cross-sectional features for the entire OHLCV series in one
        vectorised pass — O(n) instead of the previous O(n²) expanding-window loop.

        Args:
            ohlcv_df: DataFrame with OHLCV columns and integer index (sorted by time).

        Returns:
            DataFrame indexed by timestamp, columns = CROSS_SECTION_FEATURE_NAMES.
            Rows with insufficient warm-up history (NaN from long-period indicators
            such as SMA_200) are dropped — callers should not assume a fixed offset.
        """
        features_df = compute_cross_section_features(ohlcv_df)

        # Attach timestamps as the index so create_sequences() can align with labels
        features_df.index = pd.Index(ohlcv_df['timestamp'].values, name='timestamp')

        # Drop warm-up rows (SMA_200 needs 200 bars, etc.)
        n_before = len(features_df)
        features_df = features_df.dropna()
        logger.debug(
            "Features: %d rows computed, %d warm-up rows dropped, %d features",
            n_before, n_before - len(features_df), len(CROSS_SECTION_FEATURE_NAMES),
        )

        return features_df

    def _generate_labels(self, ohlcv_df: pd.DataFrame) -> pd.DataFrame:
        """
        Generate binary UP/DOWN labels with a symmetric ATR-adaptive dead zone.

        Uses ``create_target_variable`` with the pipeline's prediction_horizon.
        Dead-zone rows (|return| ≤ threshold) are kept as NaN here; they are
        skipped during sequence creation so no information about them leaks into
        the training set.

        HOLD is not a training label — it is applied at inference time by
        thresholding the UP-class probability (Selective Classification).

        Args:
            ohlcv_df: DataFrame with OHLCV columns and integer index (sorted by time).

        Returns:
            DataFrame with a single 'direction' column (1.0=UP, 0.0=DOWN, NaN=dead zone),
            indexed by timestamp.
        """
        target = create_target_variable(
            ohlcv_df,
            atr_multiplier=ATR_MULTIPLIER_DEFAULT,
            horizon=self.prediction_horizon,
            price_col='close',
            use_atr_normalization=True,
        )

        labels_df = pd.DataFrame(
            {'direction': target.values},
            index=pd.Index(ohlcv_df['timestamp'].values, name='timestamp'),
        )

        n_valid = int(labels_df['direction'].notna().sum())
        logger.debug("Labels: %d total rows, %d labeled (%.1f%% dead zone)",
                     len(labels_df), n_valid,
                     100.0 * (1 - n_valid / max(len(labels_df), 1)))

        return labels_df

    def create_sequences(
        self,
        features_df: pd.DataFrame,
        labels_df: pd.DataFrame,
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        """
        Create fixed-length time-series sequences for GRU input.

        Sequence i uses features from rows [i : i+sequence_length] and the
        label at row i+sequence_length.  Sequences whose label is NaN (ATR
        dead-zone) are silently skipped — the feature history they contain is
        still valid and contributes to later sequences via the sliding window.

        Args:
            features_df: DataFrame indexed by timestamp (37 columns).
            labels_df:   DataFrame indexed by timestamp with 'direction' column.

        Returns:
            (X, label_dict) where:
            - X:            float32 array  (n_samples, sequence_length, n_features)
            - label_dict:   {'direction': int32 array (n_samples,)}  — 0=DOWN, 1=UP
        """
        common_ts = features_df.index.intersection(labels_df.index)
        features_aligned = features_df.loc[common_ts]
        labels_aligned = labels_df.loc[common_ts]

        feat_arr = features_aligned.values.astype(np.float32)
        dir_arr = labels_aligned['direction'].values  # float64 with possible NaN

        X_list: list[np.ndarray] = []
        y_list: list[int] = []

        for i in range(len(feat_arr) - self.sequence_length):
            label = dir_arr[i + self.sequence_length]
            if np.isnan(label):
                continue  # dead-zone sample — skip label, keep history intact
            X_list.append(feat_arr[i : i + self.sequence_length])
            y_list.append(int(label))

        X = np.array(X_list, dtype=np.float32)
        y_direction = np.array(y_list, dtype=np.int32)

        logger.debug(
            "Sequences: %d created  (shape %s)  |  skipped %d dead-zone labels",
            len(X), X.shape if len(X) else "(0,)",
            (len(feat_arr) - self.sequence_length) - len(X),
        )

        return X, {'direction': y_direction}

    def train_test_split(
        self,
        X: np.ndarray,
        y: dict[str, np.ndarray],
        train_ratio: float = 0.8,
    ) -> tuple[np.ndarray, np.ndarray, dict[str, np.ndarray], dict[str, np.ndarray]]:
        """
        Split data into train and test sets with temporal ordering.

        IMPORTANT: Uses temporal split (not random) to prevent data leakage.
        Train set contains earlier data, test set contains later data.

        Args:
            X: Feature sequences (num_samples, sequence_length, num_features)
            y: Label dictionary
            train_ratio: Ratio of data for training (default: 0.8)

        Returns:
            Tuple of (X_train, X_test, y_train, y_test)

        Example:
            >>> X_train, X_test, y_train, y_test = pipeline.train_test_split(X, y)
            >>> print(f"Train: {len(X_train)}, Test: {len(X_test)}")
        """
        split_idx = int(len(X) * train_ratio)
        
        X_train = X[:split_idx]
        X_test = X[split_idx:]
        
        y_train = {key: values[:split_idx] for key, values in y.items()}
        y_test = {key: values[split_idx:] for key, values in y.items()}
        
        logger.info(
            f"Train/test split: {len(X_train)} train samples, "
            f"{len(X_test)} test samples (ratio: {train_ratio:.2f})"
        )
        
        return X_train, X_test, y_train, y_test

    def get_class_weights(self, y_direction: np.ndarray) -> dict[int, float]:
        """
        Compute balanced class weights for binary UP/DOWN labels.

        Uses sklearn-style balanced weighting:
            weight_c = n_samples / (n_classes × n_samples_c)

        Args:
            y_direction: int32 array of binary labels (0=DOWN, 1=UP).

        Returns:
            {0: weight_down, 1: weight_up}
        """
        unique, counts = np.unique(y_direction, return_counts=True)
        total = len(y_direction)
        n_classes = len(unique)

        weights = {int(cls): total / (n_classes * count)
                   for cls, count in zip(unique, counts)}

        logger.info("Class distribution: %s  →  weights: %s",
                    dict(zip(unique.tolist(), counts.tolist())), weights)

        return weights


# ══════════════════════════════════════════════════════════════════════════════
# FACTORY FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def get_feature_pipeline(
    session: AsyncSession,
    sequence_length: int = 60,
    prediction_horizon: int = 5,
) -> FeaturePipeline:
    """
    Factory function to create FeaturePipeline instance.

    Args:
        session: Database session
        sequence_length: Number of time steps in each sequence
        prediction_horizon: Days ahead to predict

    Returns:
        Configured FeaturePipeline instance
    """
    return FeaturePipeline(
        session=session,
        sequence_length=sequence_length,
        prediction_horizon=prediction_horizon,
    )
