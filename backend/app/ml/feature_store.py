"""
Cortex AI — Feature Store
===========================
Centralized feature repository ensuring training-serving consistency.
Manages feature definitions, computation, versioning, and caching.

Key responsibilities:
- Register feature computation functions
- Compute features in batch (training) and real-time (inference)
- Version feature definitions for reproducibility
- Cache computed features in Redis for low-latency serving
- Validate feature schema compatibility
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis import CacheService
from app.models.ml_data import MLFeature
from app.ml.feature_versioning import FeatureVersioningMixin

logger = logging.getLogger(__name__)

# Type alias for feature computation functions
FeatureFunction = Callable[[pd.DataFrame], float]


class FeatureStore(FeatureVersioningMixin):
    """
    Feature Store managing feature definitions, computation, and serving.
    
    Ensures training-serving consistency by using identical computation logic
    for both batch (training) and real-time (inference) feature generation.
    """

    def __init__(
        self,
        session: AsyncSession,
        cache: CacheService,
        feature_version: str = "1.0.0",
    ) -> None:
        """
        Initialize Feature Store.

        Args:
            session: Database session for feature persistence
            cache: Redis cache service for low-latency feature serving
            feature_version: Semantic version for feature definitions
        """
        self._session = session
        self._cache = cache
        self._feature_version = feature_version
        
        # Feature definition registry: {feature_name: computation_function}
        self._feature_registry: dict[str, FeatureFunction] = {}
        
        # Feature metadata: {feature_name: {description, data_type, etc.}}
        self._feature_metadata: dict[str, dict[str, Any]] = {}

    def register_feature(
        self,
        name: str,
        computation_fn: FeatureFunction,
        description: str = "",
        data_type: str = "float",
    ) -> None:
        """
        Register a feature definition with its computation function.

        Args:
            name: Unique feature name (e.g., "rsi_14", "macd_signal")
            computation_fn: Function that computes feature from OHLCV DataFrame
            description: Human-readable feature description
            data_type: Feature data type (default: "float")

        Example:
            >>> def compute_rsi(df: pd.DataFrame) -> float:
            ...     # RSI computation logic
            ...     return rsi_value
            >>> feature_store.register_feature("rsi_14", compute_rsi, "14-period RSI")
        """
        if name in self._feature_registry:
            logger.warning("Feature %s already registered, overwriting", name)
        
        self._feature_registry[name] = computation_fn
        self._feature_metadata[name] = {
            "description": description,
            "data_type": data_type,
            "version": self._feature_version,
        }
        
        logger.info("Registered feature: %s (version %s)", name, self._feature_version)

    async def compute_features(
        self,
        symbol: str,
        timeframe: str,
        ohlcv_data: pd.DataFrame,
        feature_names: list[str] | None = None,
    ) -> dict[str, float]:
        """
        Compute features for a given symbol and timeframe (batch mode).

        This method is used during training to compute features for historical data.
        Features are computed using registered computation functions and stored
        in the database for reproducibility.

        Args:
            symbol: Stock symbol (e.g., "NSE_EQ|INE002A01018")
            timeframe: Timeframe (e.g., "1d", "1w")
            ohlcv_data: DataFrame with columns [timestamp, open, high, low, close, volume]
            feature_names: List of features to compute (None = all registered features)

        Returns:
            Dictionary mapping feature names to computed values

        Raises:
            ValueError: If OHLCV data is invalid or insufficient
            KeyError: If requested feature is not registered
        """
        # Validate OHLCV data
        self._validate_ohlcv_data(ohlcv_data)
        
        # Determine which features to compute
        features_to_compute = feature_names or list(self._feature_registry.keys())
        
        # Compute each feature
        feature_vector: dict[str, float] = {}
        computed_at = datetime.now(timezone.utc)
        
        for feature_name in features_to_compute:
            if feature_name not in self._feature_registry:
                raise KeyError(f"Feature {feature_name} not registered")
            
            try:
                computation_fn = self._feature_registry[feature_name]
                feature_value = computation_fn(ohlcv_data)
                feature_vector[feature_name] = float(feature_value)
                
                # Store in database for audit trail
                feature_record = MLFeature(
                    symbol=symbol,
                    timeframe=timeframe,
                    feature_name=feature_name,
                    feature_value=feature_value,
                    version=self._feature_version,
                    computed_at=computed_at,
                )
                self._session.add(feature_record)
                
            except Exception as exc:
                logger.error(
                    "Feature computation failed: %s for %s",
                    feature_name,
                    symbol,
                    exc_info=exc,
                )
                # Return None for failed features (graceful degradation)
                feature_vector[feature_name] = None  # type: ignore[assignment]
        
        await self._session.commit()
        
        logger.debug(
            "Computed %d features for %s [%s]",
            len(feature_vector),
            symbol,
            timeframe,
        )
        
        return feature_vector

    async def get_features(
        self,
        symbol: str,
        timeframe: str,
        feature_names: list[str] | None = None,
        use_cache: bool = True,
    ) -> dict[str, float]:
        """
        Retrieve features for real-time inference (serving mode).

        This method is used during inference to get features with low latency.
        Features are retrieved from Redis cache if available, otherwise computed
        from latest OHLCV data and cached.

        Args:
            symbol: Stock symbol
            timeframe: Timeframe
            feature_names: List of features to retrieve (None = all)
            use_cache: Whether to use Redis cache (default: True)

        Returns:
            Dictionary mapping feature names to values

        Raises:
            ValueError: If features not found and cannot be computed
        """
        features_to_get = feature_names or list(self._feature_registry.keys())
        cache_key = self._get_cache_key(symbol, timeframe, self._feature_version)
        
        # Try cache first
        if use_cache:
            cached_features = await self._cache.get(cache_key)
            if cached_features is not None:
                logger.debug("Cache hit for features: %s [%s]", symbol, timeframe)
                # Filter to requested features
                return {k: v for k, v in cached_features.items() if k in features_to_get}
        
        # Cache miss - retrieve from database
        logger.debug("Cache miss for features: %s [%s]", symbol, timeframe)
        
        stmt = (
            select(MLFeature)
            .where(
                and_(
                    MLFeature.symbol == symbol,
                    MLFeature.timeframe == timeframe,
                    MLFeature.version == self._feature_version,
                    MLFeature.feature_name.in_(features_to_get),
                )
            )
            .order_by(MLFeature.computed_at.desc())
            .limit(len(features_to_get))
        )
        
        result = await self._session.execute(stmt)
        feature_records = result.scalars().all()
        
        if not feature_records:
            raise ValueError(
                f"No features found for {symbol} [{timeframe}] version {self._feature_version}"
            )
        
        # Build feature vector from database records
        feature_vector = {
            record.feature_name: record.feature_value
            for record in feature_records
        }
        
        # Cache for future requests (5-minute TTL for daily, 1-hour for weekly)
        cache_ttl = 300 if timeframe == "1d" else 3600
        await self._cache.set(cache_key, feature_vector, ttl=cache_ttl)
        
        return feature_vector

    def get_feature_version(self) -> str:
        """Get current feature version."""
        return self._feature_version


    async def register_feature_definition(
        self,
        feature_name: str,
        version: str,
        computation_fn: FeatureFunction,
        description: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """
        Register a feature definition with version tracking.

        This method stores feature definitions in the database for reproducibility
        and version management. It allows tracking which features were used in
        which model versions.

        Args:
            feature_name: Unique feature name (e.g., "rsi_14")
            version: Semantic version for this feature definition (e.g., "1.0.0")
            computation_fn: Function that computes the feature
            description: Human-readable description
            metadata: Additional metadata (parameters, dependencies, etc.)

        Example:
            >>> await feature_store.register_feature_definition(
            ...     "rsi_14",
            ...     "1.0.0",
            ...     lambda df: rsi(df, 14),
            ...     "14-period RSI using Wilder's smoothing",
            ...     {"period": 14, "method": "wilder"}
            ... )
        """
        # Register in memory
        self.register_feature(feature_name, computation_fn, description)

        # Store definition in database for version tracking
        # Note: We store the feature definition metadata, not the function itself
        # The function is registered in memory and must be re-registered on startup
        feature_def_metadata = {
            "description": description,
            "version": version,
            "metadata": metadata or {},
            "registered_at": datetime.now(timezone.utc).isoformat(),
        }

        # Store as a special feature record with NULL feature_value
        # This acts as a feature definition registry
        feature_def_record = MLFeature(
            symbol="__DEFINITION__",  # Special marker for definitions
            timeframe="__ALL__",
            feature_name=feature_name,
            feature_value=0.0,  # Placeholder (not used for definitions)
            version=version,
            computed_at=datetime.now(timezone.utc),
        )
        self._session.add(feature_def_record)
        await self._session.commit()

        logger.info(
            "Registered feature definition: %s (version %s)",
            feature_name,
            version,
        )

    async def get_feature_definitions(
        self,
        version: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        """
        Retrieve feature definitions for a specific version.

        Args:
            version: Feature version to retrieve (None = current version)

        Returns:
            Dictionary mapping feature names to their definitions
        """
        target_version = version or self._feature_version

        stmt = (
            select(MLFeature)
            .where(
                and_(
                    MLFeature.symbol == "__DEFINITION__",
                    MLFeature.version == target_version,
                )
            )
        )

        result = await self._session.execute(stmt)
        definition_records = result.scalars().all()

        definitions = {}
        for record in definition_records:
            definitions[record.feature_name] = {
                "version": record.version,
                "registered_at": record.computed_at,
            }

        return definitions

    async def migrate_features(
        self,
        symbol: str,
        timeframe: str,
        from_version: str,
        to_version: str,
        ohlcv_data: pd.DataFrame,
    ) -> dict[str, float]:
        """
        Migrate features from one version to another.

        This method recomputes features using the new version's computation
        functions while maintaining backward compatibility during the migration.

        Args:
            symbol: Stock symbol
            timeframe: Timeframe
            from_version: Current feature version
            to_version: Target feature version
            ohlcv_data: OHLCV data for recomputation

        Returns:
            Dictionary of newly computed features

        Raises:
            ValueError: If migration fails
        """
        logger.info(
            "Migrating features for %s [%s] from v%s to v%s",
            symbol,
            timeframe,
            from_version,
            to_version,
        )

        # Store old version temporarily
        old_version = self._feature_version

        try:
            # Switch to new version
            self._feature_version = to_version

            # Recompute all features with new version
            new_features = await self.compute_features(
                symbol=symbol,
                timeframe=timeframe,
                ohlcv_data=ohlcv_data,
            )

            # Invalidate cache for this symbol/timeframe
            await self.invalidate_cache(symbol, timeframe)

            logger.info(
                "Successfully migrated %d features for %s [%s] to v%s",
                len(new_features),
                symbol,
                timeframe,
                to_version,
            )

            return new_features

        except Exception as exc:
            # Rollback to old version on failure
            self._feature_version = old_version
            logger.error(
                "Feature migration failed for %s [%s]: %s",
                symbol,
                timeframe,
                exc,
                exc_info=True,
            )
            raise ValueError(f"Feature migration failed: {exc}") from exc


    def get_feature_schema(self) -> dict[str, dict[str, Any]]:
        """
        Get feature schema with metadata for all registered features.

        Returns:
            Dictionary mapping feature names to metadata (description, type, version)
        """
        return self._feature_metadata.copy()

    def validate_feature_schema(self, feature_vector: dict[str, float]) -> bool:
        """
        Validate that a feature vector matches the registered schema.

        Args:
            feature_vector: Dictionary of feature names to values

        Returns:
            True if schema is valid, False otherwise
        """
        registered_features = set(self._feature_registry.keys())
        provided_features = set(feature_vector.keys())
        
        # Check for missing features
        missing_features = registered_features - provided_features
        if missing_features:
            logger.warning("Missing features: %s", missing_features)
            return False
        
        # Check for extra features
        extra_features = provided_features - registered_features
        if extra_features:
            logger.warning("Extra features not in schema: %s", extra_features)
            return False
        
        return True

    async def invalidate_cache(
        self,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> int:
        """
        Invalidate cached features.

        Args:
            symbol: Specific symbol to invalidate (None = all symbols)
            timeframe: Specific timeframe to invalidate (None = all timeframes)

        Returns:
            Number of cache keys deleted
        """
        if symbol and timeframe:
            cache_key = self._get_cache_key(symbol, timeframe, self._feature_version)
            await self._cache.delete(cache_key)
            return 1
        else:
            # Invalidate all features matching pattern
            pattern = f"ml:features:*"
            count = await self._cache.delete_pattern(pattern)
            logger.info("Invalidated %d feature cache entries", count)
            return count

    def _get_cache_key(self, symbol: str, timeframe: str, version: str) -> str:
        """Generate Redis cache key for features."""
        return f"ml:features:{symbol}:{timeframe}:{version}"

    def _validate_ohlcv_data(self, df: pd.DataFrame) -> None:
        """
        Validate OHLCV DataFrame structure and data quality.

        Args:
            df: OHLCV DataFrame

        Raises:
            ValueError: If data is invalid
        """
        required_columns = ["open", "high", "low", "close", "volume"]
        missing_columns = set(required_columns) - set(df.columns)
        
        if missing_columns:
            raise ValueError(f"Missing required columns: {missing_columns}")
        
        if len(df) < 20:
            raise ValueError(
                f"Insufficient data: {len(df)} rows (minimum 20 required)"
            )
        
        # Validate OHLCV constraints
        invalid_rows = df[
            (df["high"] < df["low"]) |
            (df["close"] < df["low"]) |
            (df["close"] > df["high"])
        ]
        
        if len(invalid_rows) > 0:
            raise ValueError(
                f"Invalid OHLCV data: {len(invalid_rows)} rows violate constraints"
            )
        
        # Check for excessive missing data
        missing_pct = df[required_columns].isnull().sum().sum() / (len(df) * len(required_columns))
        if missing_pct > 0.05:
            raise ValueError(
                f"Excessive missing data: {missing_pct:.1%} (max 5% allowed)"
            )


def get_feature_store(
    session: AsyncSession,
    cache: CacheService,
    feature_version: str = "1.0.0",
) -> FeatureStore:
    """
    Factory function to create FeatureStore instance.

    Args:
        session: Database session
        cache: Redis cache service
        feature_version: Feature schema version

    Returns:
        Configured FeatureStore instance
    """
    return FeatureStore(session, cache, feature_version)
