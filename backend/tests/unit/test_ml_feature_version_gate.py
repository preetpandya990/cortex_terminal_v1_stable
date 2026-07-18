"""
WS2c — gate tests for version-gated inference parity.

Verifies:
  1. ``register_model`` persists ``feature_version`` (column + lineage) —
     the parameter was silently dropped before migration 0056.
  2. Ensemble feature-version resolution: NULL → "1.0.0", XGB/GRU mismatch →
     hard ``ModelLoadError`` (same spirit as the n_features check).
  3. v1 ensembles: legacy fundamentals hard-zeroed after manifest column
     selection and excluded from z-score — exact trained distribution
     regardless of what the feature store now contains.
  4. v2 ensembles: raw fundamentals mapped through persisted rank grids
     (newest grid <= row date), excluded from z-score; no grid → neutral 0
     with a loud warning.
"""

import numpy as np
import pandas as pd
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from app.ml.features.cross_sectional_stats import CrossStat
from app.ml.features.fundamental_features import FUNDAMENTAL_FEATURE_NAMES
from app.ml.inference.feature_loader import FeatureLoader
from app.ml.inference.registry_loader import (
    LoadedEnsemble,
    ModelLoadError,
    _resolve_ensemble_feature_version,
)
from app.ml.model_registry import ModelRegistry


def _meta(feature_version):
    m = MagicMock()
    m.feature_version = feature_version
    return m


# ─── register_model persistence ──────────────────────────────────────────────

class TestRegisterModelPersistsFeatureVersion:
    async def test_feature_version_lands_on_row_and_lineage(self, tmp_path):
        artifact = tmp_path / "model.json"
        artifact.write_text("{}")

        session = AsyncMock()
        session.add = MagicMock()  # sync method on AsyncSession
        registry = ModelRegistry(session=session, model_storage_path=tmp_path / "store")

        with patch.object(registry, "get_model", new=AsyncMock(return_value=None)):
            model = await registry.register_model(
                version="2.0.0_xgboost_test",
                model_type="xgboost",
                artifact_path=artifact,
                metrics={"accuracy": 0.65},
                metadata={"features": ["rsi_14"], "training_samples": 100},
                feature_version="2.0.0",
            )

        session.add.assert_called_once_with(model)
        assert model.feature_version == "2.0.0"
        assert model.lineage["feature_version"] == "2.0.0"


# ─── Ensemble version resolution ─────────────────────────────────────────────

class TestEnsembleVersionResolution:
    def test_null_reads_as_legacy(self):
        assert _resolve_ensemble_feature_version(_meta(None), None) == "1.0.0"
        assert _resolve_ensemble_feature_version(_meta(None), _meta(None)) == "1.0.0"

    def test_matching_versions_pass(self):
        assert _resolve_ensemble_feature_version(_meta("2.0.0"), _meta("2.0.0")) == "2.0.0"

    def test_xgb_only_mode_uses_xgb_version(self):
        assert _resolve_ensemble_feature_version(_meta("2.0.0"), None) == "2.0.0"

    def test_mismatch_is_hard_load_failure(self):
        with pytest.raises(ModelLoadError, match="Feature-version mismatch"):
            _resolve_ensemble_feature_version(_meta("2.0.0"), _meta(None))
        with pytest.raises(ModelLoadError, match="Feature-version mismatch"):
            _resolve_ensemble_feature_version(_meta("1.0.0"), _meta("2.0.0"))

    def test_loaded_ensemble_defaults_to_legacy(self):
        field = LoadedEnsemble.__dataclass_fields__["feature_version"]
        assert field.default == "1.0.0"


# ─── FeatureLoader gating ────────────────────────────────────────────────────

N_ROWS = 80
TECH_COLS = ["rsi_14", "macd_line"]
V1_FUND = ["pe_ratio", "roe"]          # both in the legacy 20-name list
V2_FUND = ["roe", "debt_ratio"]        # both in the v2 17-name list


def _loader(feature_version: str, feature_names: list[str]) -> FeatureLoader:
    return FeatureLoader(
        db=AsyncMock(),
        redis=MagicMock(),
        sequence_length=10,
        n_features=len(feature_names),
        feature_names=feature_names,
        feature_version=feature_version,
    )


def _frame(cols: dict[str, np.ndarray]) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-04-01", periods=N_ROWS, tz="UTC"),
        "close": rng.uniform(100, 110, N_ROWS),
        **cols,
    })


class TestV1HardZero:
    def test_varying_store_values_are_forced_to_trained_zero(self):
        """The crucial case: the store now holds PIT-varying fundamentals a
        v1 model has never seen — they must come out exactly 0."""
        rng = np.random.default_rng(4)
        names = TECH_COLS + V1_FUND
        df = _frame({
            "rsi_14":    rng.uniform(20, 80, N_ROWS),
            "macd_line": rng.normal(0, 2, N_ROWS),
            "pe_ratio":  rng.uniform(5, 60, N_ROWS),   # varying!
            "roe":       rng.uniform(0.02, 0.3, N_ROWS),
        })
        loader = _loader("1.0.0", names)
        tabular, sequence, price, _vol = loader._prepare_features(df)

        pe_idx, roe_idx = names.index("pe_ratio"), names.index("roe")
        assert tabular[pe_idx] == 0.0 and tabular[roe_idx] == 0.0
        assert (sequence[:, pe_idx] == 0.0).all()
        assert (sequence[:, roe_idx] == 0.0).all()
        # Technicals still z-scored (non-degenerate).
        assert float(np.std(sequence[:, names.index("rsi_14")])) > 0.0
        assert price > 0

    def test_snapshot_captures_raw_values_before_zeroing(self):
        names = TECH_COLS + V1_FUND
        rng = np.random.default_rng(5)
        df = _frame({
            "rsi_14":    rng.uniform(20, 80, N_ROWS),
            "macd_line": rng.normal(0, 2, N_ROWS),
            "pe_ratio":  np.full(N_ROWS, 23.5),
            "roe":       np.full(N_ROWS, 0.18),
        })
        snapshot: dict = {}
        _loader("1.0.0", names)._prepare_features(df, indicator_snapshot_out=snapshot)
        assert snapshot["pe_ratio"] == pytest.approx(23.5)


class TestV2RankTransform:
    def _grid(self) -> CrossStat:
        # Uniform raw values 0..1 → rank == 2*raw - 1.
        return CrossStat(
            quantiles=tuple(np.linspace(0.0, 1.0, 101)), median=0.5, n_obs=500,
        )

    async def test_raw_values_map_through_newest_grid(self):
        names = TECH_COLS + V2_FUND
        loader = _loader("2.0.0", names)
        rng = np.random.default_rng(6)
        df = _frame({
            "rsi_14":     rng.uniform(20, 80, N_ROWS),
            "macd_line":  rng.normal(0, 2, N_ROWS),
            "roe":        np.full(N_ROWS, 0.75),
            "debt_ratio": np.full(N_ROWS, 0.25),
        })
        grids = {date(2026, 3, 31): {"roe": self._grid(), "debt_ratio": self._grid()}}
        with patch(
            "app.ml.inference.feature_loader.load_cross_sectional_stats",
            new=AsyncMock(return_value=grids),
        ):
            out = await loader._apply_rank_transform(df)

        assert out["roe"].to_numpy() == pytest.approx(0.5)          # 2*0.75-1
        assert out["debt_ratio"].to_numpy() == pytest.approx(-0.5)  # 2*0.25-1

    async def test_rows_before_first_grid_are_neutral(self):
        names = V2_FUND
        loader = _loader("2.0.0", names)
        df = _frame({"roe": np.full(N_ROWS, 0.9), "debt_ratio": np.full(N_ROWS, 0.9)})
        # Grid published mid-window: rows before it get 0, after get rank.
        mid = date(2026, 5, 1)
        grids = {mid: {"roe": self._grid(), "debt_ratio": self._grid()}}
        with patch(
            "app.ml.inference.feature_loader.load_cross_sectional_stats",
            new=AsyncMock(return_value=grids),
        ):
            out = await loader._apply_rank_transform(df)

        ts = pd.to_datetime(out["timestamp"]).dt.tz_localize(None).dt.date
        before, after = out[ts < mid], out[ts >= mid]
        assert (before["roe"] == 0.0).all()
        assert after["roe"].to_numpy() == pytest.approx(0.8)  # 2*0.9-1

    async def test_no_grids_at_all_degrades_to_neutral_with_warning(self):
        loader = _loader("2.0.0", V2_FUND)
        df = _frame({"roe": np.full(N_ROWS, 0.9), "debt_ratio": np.full(N_ROWS, 0.9)})
        # Patch the module logger directly — caplog is unreliable here because
        # other tests in the suite install JSON logging with propagate=False.
        with (
            patch(
                "app.ml.inference.feature_loader.load_cross_sectional_stats",
                new=AsyncMock(return_value={}),
            ),
            patch("app.ml.inference.feature_loader.logger") as mock_logger,
        ):
            out = await loader._apply_rank_transform(df)

        assert (out["roe"] == 0.0).all()
        warned = " ".join(str(c.args[0]) for c in mock_logger.warning.call_args_list)
        assert "NO cross-sectional rank grids" in warned

    def test_prepare_features_leaves_rank_values_unscaled(self):
        """Rank-normalized fundamentals must pass through z-score untouched."""
        names = TECH_COLS + V2_FUND
        rng = np.random.default_rng(7)
        ranks = np.clip(np.linspace(-1, 1, N_ROWS) + rng.normal(0, 0.02, N_ROWS), -1, 1)
        df = _frame({
            "rsi_14":     rng.uniform(20, 80, N_ROWS),
            "macd_line":  rng.normal(0, 2, N_ROWS),
            "roe":        ranks,
            "debt_ratio": ranks[::-1].copy(),
        })
        loader = _loader("2.0.0", names)
        tabular, sequence, _price, _vol = loader._prepare_features(df)

        roe_idx = names.index("roe")
        assert tabular[roe_idx] == pytest.approx(ranks[-1], abs=1e-6)
        assert sequence[:, roe_idx] == pytest.approx(ranks[-10:], abs=1e-6)

    async def test_load_features_invokes_rank_prestep_for_v2(self):
        """Tier-1 path calls _apply_rank_transform before _prepare_features."""
        names = V2_FUND
        loader = _loader("2.0.0", names)
        df = _frame({"roe": np.full(N_ROWS, 0.5), "debt_ratio": np.full(N_ROWS, 0.5)})

        with (
            patch.object(loader, "_resolve_instrument_key", new=AsyncMock(return_value="NSE_EQ|X")),
            patch.object(loader, "_load_from_database", new=AsyncMock(return_value=df)),
            patch.object(loader, "_apply_rank_transform", new=AsyncMock(return_value=df)) as rank,
            patch.object(loader, "_prepare_features", return_value=(1, 2, 3, 4)),
        ):
            result = await loader.load_features("X")

        rank.assert_awaited_once()
        assert result == (1, 2, 3, 4)

    async def test_load_features_skips_rank_prestep_for_v1(self):
        loader = _loader("1.0.0", V1_FUND)
        df = _frame({"pe_ratio": np.zeros(N_ROWS), "roe": np.zeros(N_ROWS)})

        with (
            patch.object(loader, "_resolve_instrument_key", new=AsyncMock(return_value="NSE_EQ|X")),
            patch.object(loader, "_load_from_database", new=AsyncMock(return_value=df)),
            patch.object(loader, "_apply_rank_transform", new=AsyncMock()) as rank,
            patch.object(loader, "_prepare_features", return_value=(1, 2, 3, 4)),
        ):
            await loader.load_features("X")

        rank.assert_not_awaited()
