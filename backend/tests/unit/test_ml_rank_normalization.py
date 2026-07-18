"""
WS2b — gate tests for cross-sectional rank normalization.

Verifies (pure core): hand-computed ranks, NaN → 0, all-equal → 0,
grid round-trip parity (``rank_transform_with_grid`` reproduces in-panel
ranks within tolerance), out-of-range clipping to ±1, missing grid → 0.
Plus: persistence upsert/load shape, the v2 batch integration, and the
end-to-end property that fundamentals keep non-zero variance in the final
training matrix when excluded from rolling z-score.
"""

from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.ml.features.cross_sectional_stats import (
    CrossStat,
    load_cross_sectional_stats,
    persist_cross_sectional_stats,
    rank_normalize_panel,
    rank_transform_with_grid,
)
from app.ml.features.feature_pipeline import compute_features_batch, normalize_features
from app.ml.features.fundamental_features import FUNDAMENTAL_FEATURE_NAMES_V2

D1 = date(2026, 7, 1)


def _frame(values: list[float], feature: str = "roe", n_days: int = 1) -> pd.DataFrame:
    """Single-symbol frame: one value per day for `feature`."""
    return pd.DataFrame({
        "timestamp": pd.to_datetime([f"2026-07-{d+1:02d}" for d in range(n_days)], utc=True),
        feature: values,
    })


def _panel(values_by_symbol: dict[str, float], feature: str = "roe") -> dict[str, pd.DataFrame]:
    return {sym: _frame([val], feature) for sym, val in values_by_symbol.items()}


# ─── Pure core: rank_normalize_panel ─────────────────────────────────────────

class TestRankNormalizePanel:
    def test_hand_computed_ranks(self):
        """4 symbols, values 10 < 20 < 30 < 40 → pct {.25,.5,.75,1} → 2p-1."""
        results = _panel({"A": 10.0, "B": 20.0, "C": 30.0, "D": 40.0})
        stats = rank_normalize_panel(results, ["roe"])

        assert results["A"]["roe"].iloc[0] == pytest.approx(-0.5)
        assert results["B"]["roe"].iloc[0] == pytest.approx(0.0)
        assert results["C"]["roe"].iloc[0] == pytest.approx(0.5)
        assert results["D"]["roe"].iloc[0] == pytest.approx(1.0)

        stat = stats[D1]["roe"]
        assert stat.n_obs == 4
        assert stat.median == pytest.approx(25.0)
        assert len(stat.quantiles) == 101
        assert stat.quantiles[0] == pytest.approx(10.0)
        assert stat.quantiles[-1] == pytest.approx(40.0)

    def test_nan_becomes_neutral_zero(self):
        results = _panel({"A": 10.0, "B": np.nan, "C": 30.0})
        stats = rank_normalize_panel(results, ["roe"])

        assert results["B"]["roe"].iloc[0] == 0.0
        assert stats[D1]["roe"].n_obs == 2  # NaN excluded from the grid

    def test_all_equal_cross_section_is_zero(self):
        results = _panel({"A": 5.0, "B": 5.0, "C": 5.0})
        stats = rank_normalize_panel(results, ["roe"])

        for sym in ("A", "B", "C"):
            assert results[sym]["roe"].iloc[0] == 0.0
        assert stats[D1]["roe"].degenerate

    def test_single_observation_is_zero(self):
        results = _panel({"A": 42.0})
        stats = rank_normalize_panel(results, ["roe"])

        assert results["A"]["roe"].iloc[0] == 0.0
        assert stats[D1]["roe"].degenerate

    def test_no_valid_observations_yields_no_stat(self):
        results = _panel({"A": np.nan, "B": np.nan})
        stats = rank_normalize_panel(results, ["roe"])
        assert stats == {}

    def test_dates_rank_independently(self):
        """A symbol strong on day 1 and weak on day 2 gets per-date ranks."""
        results = {
            "A": _frame([40.0, 1.0], n_days=2),
            "B": _frame([10.0, 2.0], n_days=2),
            "C": _frame([20.0, 3.0], n_days=2),
        }
        rank_normalize_panel(results, ["roe"])
        assert results["A"]["roe"].iloc[0] == pytest.approx(1.0)       # best of day 1
        assert results["A"]["roe"].iloc[1] == pytest.approx(-1.0 / 3)  # worst of day 2

    def test_missing_feature_column_treated_as_nan(self):
        results = {"A": _frame([10.0]), "B": _frame([20.0])}
        rank_normalize_panel(results, ["roe", "debt_ratio"])
        assert results["A"]["debt_ratio"].iloc[0] == 0.0


# ─── Pure core: rank_transform_with_grid ─────────────────────────────────────

class TestRankTransformWithGrid:
    def _stat(self, values: list[float]) -> CrossStat:
        arr = np.asarray(values, dtype=float)
        return CrossStat(
            quantiles=tuple(np.quantile(arr, np.linspace(0, 1, 101))),
            median=float(np.median(arr)),
            n_obs=len(arr),
        )

    def test_grid_round_trip_parity_with_panel_ranks(self):
        """The persisted grid must reproduce in-panel ranks within ~1/n."""
        rng = np.random.default_rng(42)
        values = rng.normal(0.15, 0.08, size=500)
        symbols = {f"S{i}": float(v) for i, v in enumerate(values)}

        results = _panel(symbols)
        stats = rank_normalize_panel(results, ["roe"])
        stat = stats[D1]["roe"]

        # Tolerance = one grid cell on the rank axis (101 points → 0.02) plus
        # the 1/n rank discretization — the grid cannot be more precise than
        # its own resolution.
        tol = 2.0 / 100 + 2.0 / len(symbols)
        for i, (sym, raw) in enumerate(symbols.items()):
            if i % 25:  # sample every 25th for speed
                continue
            in_panel = results[sym]["roe"].iloc[0]
            via_grid = rank_transform_with_grid(raw, stat)
            assert via_grid == pytest.approx(in_panel, abs=tol)

    def test_out_of_range_clips_to_plus_minus_one(self):
        stat = self._stat([1.0, 2.0, 3.0, 4.0, 5.0])
        assert rank_transform_with_grid(999.0, stat) == 1.0
        assert rank_transform_with_grid(-999.0, stat) == -1.0

    def test_missing_grid_is_neutral_zero(self):
        assert rank_transform_with_grid(0.5, None) == 0.0

    def test_nan_raw_is_neutral_zero(self):
        stat = self._stat([1.0, 2.0, 3.0])
        assert rank_transform_with_grid(float("nan"), stat) == 0.0

    def test_degenerate_grid_is_neutral_zero(self):
        stat = self._stat([7.0, 7.0, 7.0])
        assert stat.degenerate
        assert rank_transform_with_grid(7.0, stat) == 0.0

    def test_median_value_maps_near_zero(self):
        stat = self._stat(list(np.arange(1.0, 102.0)))  # 1..101, median 51
        assert rank_transform_with_grid(51.0, stat) == pytest.approx(0.0, abs=0.02)


# ─── Persistence ─────────────────────────────────────────────────────────────

class TestPersistence:
    async def test_persist_upserts_all_rows_in_chunks(self):
        session = AsyncMock()
        stats = {
            D1: {
                "roe": CrossStat(quantiles=tuple([1.0] * 100 + [2.0]), median=1.5, n_obs=10),
                "debt_ratio": CrossStat(quantiles=tuple(np.linspace(0, 1, 101)), median=0.5, n_obs=10),
            },
            date(2026, 7, 2): {
                "roe": CrossStat(quantiles=tuple(np.linspace(0, 1, 101)), median=0.5, n_obs=12),
            },
        }
        written = await persist_cross_sectional_stats(session, stats, "2.0.0")

        assert written == 3
        session.execute.assert_awaited_once()  # 3 rows < chunk size → 1 statement
        stmt = session.execute.await_args.args[0]
        assert stmt._post_values_clause is not None  # ON CONFLICT present

    async def test_load_rebuilds_in_memory_shape(self):
        session = AsyncMock()
        result = MagicMock()
        result.all.return_value = [
            (D1, "roe", [float(q) for q in np.linspace(0, 1, 101)], 0.5, 20),
        ]
        session.execute.return_value = result

        stats = await load_cross_sectional_stats(
            session, ["roe"], D1, D1, "2.0.0"
        )
        stat = stats[D1]["roe"]
        assert isinstance(stat, CrossStat)
        assert stat.n_obs == 20
        assert len(stat.quantiles) == 101


# ─── Batch integration + end-to-end variance ─────────────────────────────────

class TestBatchIntegration:
    async def _run_batch(self, version: str) -> dict[str, pd.DataFrame]:
        """3 symbols with PIT-varying roe; other v2 features absent (NaN)."""
        def df_for(mult: float) -> pd.DataFrame:
            frame = _frame([0.10 * mult, 0.12 * mult, 0.14 * mult], n_days=3)
            frame["symbol"] = f"S{mult}"
            return frame

        frames = {f"S{m}": df_for(m) for m in (1.0, 2.0, 3.0)}

        async def fake_compute(symbol, *args, **kwargs):
            return frames[symbol]

        with patch(
            "app.ml.features.feature_pipeline.compute_features_for_symbol",
            new=AsyncMock(side_effect=fake_compute),
        ):
            return await compute_features_batch(
                list(frames), datetime(2026, 7, 1), datetime(2026, 7, 3), "1D",
                AsyncMock(),
                include_sentiment=False,
                include_fundamentals=True,
                feature_set_version=version,
            )

    async def test_v2_rank_normalizes_in_place(self):
        results = await self._run_batch("2.0.0")
        # 3 symbols, distinct values each day → ranks {-1/3, 1/3, 1}.
        assert results["S1.0"]["roe"].iloc[0] == pytest.approx(-1.0 / 3)
        assert results["S3.0"]["roe"].iloc[0] == pytest.approx(1.0)
        # Absent v2 features were rank-passed to neutral 0, not median-imputed.
        assert (results["S1.0"]["debt_ratio"] == 0.0).all()

    async def test_v1_keeps_legacy_median_imputation(self):
        results = await self._run_batch("1.0.0")
        # Legacy path: raw values untouched (no ranking).
        assert results["S1.0"]["roe"].iloc[0] == pytest.approx(0.10)

    def test_fundamentals_keep_variance_through_zscore_exclusion(self):
        """End-to-end: ranked fundamentals excluded from rolling z-score
        arrive in the final matrix with non-zero variance — the v1 bug
        (constant → z-score 0) cannot recur."""
        rng = np.random.default_rng(7)
        n = 120
        df = pd.DataFrame({
            "timestamp": pd.date_range("2026-01-01", periods=n, tz="UTC"),
            "some_technical": rng.normal(size=n),
            "roe": np.clip(np.linspace(-1, 1, n) + rng.normal(0, 0.05, n), -1, 1),
        })
        out = normalize_features(df, method="rolling", window=60, feature_cols=["some_technical"])

        assert float(out["roe"].std()) > 0.1          # survived untouched
        pd.testing.assert_series_equal(out["roe"], df["roe"], check_names=False)
