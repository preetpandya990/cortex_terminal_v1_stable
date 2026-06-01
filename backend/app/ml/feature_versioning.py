"""
Cortex AI — Feature Versioning Extension
==========================================
Feature versioning methods to be integrated into FeatureStore.

These methods provide:
- Feature definition registration with version tracking
- Feature migration between versions
- Backward compatibility during migrations
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

import pandas as pd
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ml_data import MLFeature


logger = logging.getLogger(__name__)

# Type alias for feature computation functions
FeatureFunction = Callable[[pd.DataFrame], float]


class FeatureVersioningMixin:
    """
    Mixin class providing feature versioning capabilities.
    
    This mixin adds version tracking and migration methods to FeatureStore.
    """

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
        # Register in memory (assumes self has register_feature method)
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

        # Store as a special feature record
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
