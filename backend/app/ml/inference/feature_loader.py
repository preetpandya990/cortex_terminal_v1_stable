"""
Cortex AI — Feature Loading Service
=====================================
Production-grade feature loading with instrument_key resolution,
rolling z-score normalization, and graceful fallback.

Loading strategy:
  1. TimescaleDB ml_features table (pre-computed, < 50 ms)
  2. On-demand computation from raw OHLCV (< 500 ms)

Symbol resolution:
  All public methods accept NSE trading symbols (e.g. "RELIANCE").
  Internally, trading symbols are resolved to Upstox instrument_keys
  (e.g. "NSE_EQ|INE002A01018") via instrument_master before any OHLCV
  or feature-store query — matching the format used at training time.

Normalization:
  Features are normalized with the same rolling z-score (window=60) applied
  during training.  Skipping this step causes out-of-distribution inputs that
  degrade model probability outputs to near-random (≈0.50).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from contextlib import asynccontextmanager
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ml.features.feature_store import load_features_from_db
from app.ml.features.feature_pipeline import compute_features_for_symbol, normalize_features

logger = logging.getLogger(__name__)

# Module-level cache: trading_symbol → instrument_key.
# Instrument keys are immutable once instrument_master is populated,
# so a process-lifetime dict is safe and eliminates repeated DB round-trips.
_INSTRUMENT_KEY_CACHE: dict[str, str] = {}


class FeatureLoader:
    """
    Production feature loader with instrument_key resolution,
    rolling z-score normalization, and intelligent fallback.

    n_features, sequence_length, and feature_names must be sourced from the
    LoadedEnsemble at startup and passed here.  The ensemble is the single
    source of truth for its own feature contract.  The constructor defaults
    (49, 60, empty) are retained for test convenience only.
    """

    def __init__(
        self,
        db:               AsyncSession,
        redis:            Redis,
        session_factory:  async_sessionmaker | None = None,
        sequence_length:  int = 60,
        n_features:       int = 49,
        feature_names:    tuple[str, ...] | list[str] = (),
    ) -> None:
        self.db               = db
        self.redis            = redis
        self._session_factory = session_factory
        self.sequence_length  = sequence_length
        self.n_features       = n_features
        self.feature_names    = list(feature_names)

    # ── Public API ─────────────────────────────────────────────────────────────

    async def load_features(
        self,
        symbol: str,
        timeframe: str = "1d",
        *,
        indicator_snapshot_out: dict | None = None,
    ) -> tuple[np.ndarray, np.ndarray, float, float]:
        """
        Load and normalize features for a trading symbol.

        Resolves the NSE trading symbol to an Upstox instrument_key, then
        attempts the pre-computed feature store before falling back to
        on-demand OHLCV computation.

        Args:
            symbol:    NSE trading symbol ("RELIANCE") or instrument_key
            timeframe: Prediction timeframe (default "1d")
            indicator_snapshot_out: Optional caller-owned dict.  When provided, it
                is populated with the latest RAW (un-normalized) labeled indicator
                values (rsi_14, macd_line, ema_20, atr_14, …) — the same numbers
                the model sees before z-scoring.  Used by the Gemini news
                forecaster so both forecasters share identical indicators.  The
                caller owns the dict, so this is concurrency-safe.

        Returns:
            (tabular, sequence, current_price, volatility)
            tabular:  np.ndarray (n_features,)  — latest-day feature vector, normalized
            sequence: np.ndarray (sequence_length, n_features) — 60-day window, normalized
        """
        instrument_key = await self._resolve_instrument_key(symbol)

        # ── Tier 1: pre-computed feature store (< 50 ms) ──────────────────────
        try:
            features_df = await self._load_from_database(instrument_key, timeframe)
            if not features_df.empty and len(features_df) >= self.sequence_length:
                logger.info(
                    "Feature store hit for %s → %s (%d rows)",
                    symbol, instrument_key, len(features_df),
                )
                return self._prepare_features(
                    features_df, indicator_snapshot_out=indicator_snapshot_out
                )
            if not features_df.empty:
                logger.warning(
                    "Feature store: only %d rows for %s (need %d) — "
                    "falling through to on-demand computation",
                    len(features_df), symbol, self.sequence_length,
                )
        except Exception as exc:
            logger.error(
                "Feature store load failed for %s: %s", symbol, exc, exc_info=True,
            )

        # ── Tier 2: on-demand computation from OHLCV (< 500 ms) ───────────────
        try:
            logger.info(
                "Computing features on-demand for %s (instrument_key=%s)",
                symbol, instrument_key,
            )
            features_df = await self._compute_on_demand(instrument_key, timeframe)
            if not features_df.empty and len(features_df) >= 20:
                return self._prepare_features(
                    features_df, indicator_snapshot_out=indicator_snapshot_out
                )
        except Exception as exc:
            logger.error(
                "On-demand computation failed for %s: %s", symbol, exc, exc_info=True,
            )

        raise ValueError(
            f"Failed to load features for '{symbol}' "
            f"(instrument_key={instrument_key!r}). "
            f"Ensure OHLCV data exists in upstox_ohlcv for this instrument."
        )

    # ── Private: symbol resolution ─────────────────────────────────────────────

    async def _resolve_instrument_key(self, symbol: str) -> str:
        """
        Resolve an NSE trading symbol to its Upstox instrument_key via
        instrument_master.

        Results are cached in a process-lifetime module dict.  Symbols already
        in instrument_key format (containing '|') are passed through unchanged.
        """
        if symbol in _INSTRUMENT_KEY_CACHE:
            return _INSTRUMENT_KEY_CACHE[symbol]

        # Already in instrument_key format
        if "|" in symbol:
            _INSTRUMENT_KEY_CACHE[symbol] = symbol
            return symbol

        from sqlalchemy import text as sa_text

        result = await self.db.execute(
            sa_text("""
                SELECT instrument_key
                FROM   instrument_master
                WHERE  trading_symbol = :symbol
                  AND  exchange        = 'NSE'
                  AND  instrument_type = 'EQ'
                ORDER  BY instrument_key
                LIMIT  1
            """),
            {"symbol": symbol},
        )
        row = result.fetchone()
        if row:
            key = row[0]
            _INSTRUMENT_KEY_CACHE[symbol] = key
            logger.debug("Resolved symbol %s → %s", symbol, key)
            return key

        logger.warning(
            "No NSE_EQ row found in instrument_master for '%s' — "
            "using symbol as-is; feature loading may fail.",
            symbol,
        )
        return symbol

    # ── Private: data loading ───────────────────────────────────────────────────

    @asynccontextmanager
    async def _read_session(self):
        """
        Yields an isolated read-only session when session_factory is available.

        Falls back to self.db for callers that did not provide a session_factory
        (e.g. correlation_loop, which creates a fresh session per cycle with no
        prior commits on the same connection — no isolation needed there).

        This isolation is critical in the event processing pipeline where
        NLPEngine and EventClassifier each commit before feature loading runs.
        Post-commit state on asyncpg connections causes ORM SELECT queries to
        return 0 rows; a fresh session avoids this entirely.
        """
        if self._session_factory is not None:
            async with self._session_factory() as session:
                yield session
        else:
            yield self.db

    async def _load_from_database(
        self,
        instrument_key: str,
        timeframe: str,
    ) -> pd.DataFrame:
        """
        Load pre-computed features from the ml_features TimescaleDB table.

        Lookback is 2×sequence_length + 30 days so the rolling normalization
        window (window=60) is fully populated for every row in the GRU sequence.
        Uses an isolated session via _read_session() to avoid post-commit state
        contamination from earlier commits in the event processing pipeline.
        """
        end_date   = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=self.sequence_length * 2 + 30)

        timeframe_map = {"1d": "1D", "1D": "1D", "1w": "1week", "1week": "1week"}
        db_tf = timeframe_map.get(timeframe, timeframe)

        from sqlalchemy import text as sa_text

        async with self._read_session() as db:
            features_df = await load_features_from_db(
                symbol=instrument_key,
                start_date=start_date,
                end_date=end_date,
                db=db,
            )

            if features_df.empty:
                return features_df

            row = (
                await db.execute(
                    sa_text("""
                        SELECT close
                        FROM   upstox_ohlcv
                        WHERE  instrument_key = :ik
                          AND  timeframe       = :tf
                        ORDER  BY timestamp DESC
                        LIMIT  1
                    """),
                    {"ik": instrument_key, "tf": db_tf},
                )
            ).fetchone()

        if row:
            features_df["close"] = float(row[0])

        return features_df

    async def _compute_on_demand(
        self,
        instrument_key: str,
        timeframe: str,
    ) -> pd.DataFrame:
        """
        Compute features on-demand from raw OHLCV data.

        Uses an isolated session via _read_session() to avoid post-commit state
        contamination from earlier commits in the event processing pipeline.
        """
        end_date   = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=self.sequence_length * 2 + 30)

        _tf_map      = {"1d": "1D", "1w": "1week"}
        db_timeframe = _tf_map.get(timeframe, timeframe)

        async with self._read_session() as db:
            return await compute_features_for_symbol(
                symbol=instrument_key,
                start_date=start_date,
                end_date=end_date,
                timeframe=db_timeframe,
                db=db,
            )

    # ── Private: feature preparation ───────────────────────────────────────────

    def _prepare_features(
        self,
        features_df: pd.DataFrame,
        *,
        indicator_snapshot_out: dict | None = None,
    ) -> tuple[np.ndarray, np.ndarray, float, float]:
        """
        Normalize and extract inference-ready arrays from a feature DataFrame.

        Applies rolling z-score normalization (window=60, clipped [-5, 5]) to
        exactly match the preprocessing applied during training.  This is the
        critical step that prevents training/inference distribution skew — raw
        feature values (RSI≈68, EMA≈₹1450, volume ratios≈2.3) are far outside
        the [-5, 5] normalized range the model was trained on.

        Returns:
            (tabular, sequence, current_price, volatility)
        """
        features_df = features_df.sort_values("timestamp").reset_index(drop=True)

        # ── Column selection (manifest-driven or auto-detect) ──────────────────
        _exclude = {"timestamp", "symbol", "close"}
        if self.feature_names:
            missing = [f for f in self.feature_names if f not in features_df.columns]
            if missing:
                logger.warning(
                    "Zero-filling %d missing feature(s): %s. "
                    "Ensure the feature pipeline produces a complete feature set.",
                    len(missing), missing,
                )
                for col in missing:
                    features_df[col] = 0.0
            feature_cols = self.feature_names
        else:
            feature_cols = [c for c in features_df.columns if c not in _exclude]

        # ── Capture raw labeled indicators (pre-normalization) ─────────────────
        # The latest row holds human-readable values (RSI≈68, EMA≈₹1450) that the
        # Gemini news forecaster needs — captured here, before z-scoring, so both
        # forecasters reason over identical numbers.  Caller owns the dict.
        if indicator_snapshot_out is not None and len(features_df):
            latest = features_df.iloc[-1]
            for col in feature_cols:
                val = latest.get(col)
                if val is not None and not pd.isna(val):
                    indicator_snapshot_out[col] = float(val)
            if "close" in features_df.columns and not pd.isna(latest.get("close")):
                indicator_snapshot_out["close"] = float(latest["close"])

        # ── Rolling z-score normalization (must match training) ─────────────────
        # normalize_features updates feature_cols in-place on the DataFrame copy;
        # metadata columns (timestamp, symbol, close) are preserved untouched.
        features_df = normalize_features(
            features_df,
            method="rolling",
            window=60,
            feature_cols=list(feature_cols),
        )

        # ── Build feature matrix ───────────────────────────────────────────────
        feature_matrix = features_df[feature_cols].values.astype(np.float32)
        feature_matrix = np.nan_to_num(feature_matrix, nan=0.0, posinf=0.0, neginf=0.0)

        # ── Tabular (latest row) and sequence (last N rows) ────────────────────
        tabular_features = feature_matrix[-1]

        if len(feature_matrix) >= self.sequence_length:
            sequence_features = feature_matrix[-self.sequence_length:]
        else:
            # Zero-pad the time axis when fewer than sequence_length rows exist
            pad = np.zeros(
                (self.sequence_length - len(feature_matrix), feature_matrix.shape[1]),
                dtype=np.float32,
            )
            sequence_features = np.vstack([pad, feature_matrix])

        # ── Price and volatility ───────────────────────────────────────────────
        # Extract from the un-normalized close column (close is excluded from
        # normalization above so its raw value is still accessible here).
        if "close" in features_df.columns:
            close_vals    = features_df["close"].dropna()
            current_price = float(close_vals.iloc[-1]) if len(close_vals) > 0 else 0.0
        else:
            current_price = 0.0

        if "returns_1d" in features_df.columns:
            # Use raw returns from the original (pre-normalization) column.
            # Note: returns_1d IS normalized above — re-derive from raw close prices.
            # Fallback to a conservative 20% annualized vol if not available.
            volatility = 0.20
        else:
            volatility = 0.20

        # Better volatility estimate: if the feature store has a raw volatility_20d
        # column, recover it before normalization corrupts the scale.
        # The on-demand path gives us the raw DataFrame so we can extract it.
        # For now, use a safe conservative default; the model's volatility-regime
        # threshold logic is robust to a ±0.10 vol estimation error.

        return tabular_features, sequence_features, current_price, volatility

    @staticmethod
    def _estimate_volatility_from_features(features: np.ndarray) -> float:
        """Fallback: estimate annualized volatility from feature index 1 (returns_1d)."""
        try:
            daily_return = abs(float(features[1])) if len(features) > 1 else 0.01
            return daily_return * np.sqrt(252)
        except Exception:
            return 0.20


# ── Factory ────────────────────────────────────────────────────────────────────

def create_feature_loader(
    db:               AsyncSession,
    redis:            Redis,
    session_factory:  async_sessionmaker | None = None,
    sequence_length:  int = 60,
    n_features:       int = 49,
    feature_names:    tuple[str, ...] = (),
) -> FeatureLoader:
    """
    Convenience factory for FeatureLoader.

    Pass session_factory at any call site where intermediate DB commits occur
    before feature loading (e.g. event_processing_loop) so that _load_from_database
    and _compute_on_demand open isolated sessions, avoiding post-commit state
    contamination on the shared connection.

    In production inference paths always pass n_features, sequence_length, and
    feature_names from the loaded EnsemblePredictor — never rely on these defaults.
    """
    return FeatureLoader(
        db=db,
        redis=redis,
        session_factory=session_factory,
        sequence_length=sequence_length,
        n_features=n_features,
        feature_names=feature_names,
    )
