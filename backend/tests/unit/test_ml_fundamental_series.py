"""
WS2a — gate tests for the point-in-time fundamental feature series.

Verifies:
  1. PIT correctness — a statement's values appear only at
     period_date + FUNDAMENTAL_REPORTING_LAG_DAYS: the day before, the daily
     frame still carries the previous period; mutating a *future* quarter
     never changes earlier series rows.
  2. roe / roce reconstructed formulas exact (net_profit/net_worth,
     op_profit/total_asset).
  3. pe/pb/ev_ebitda are absent from the v2 feature set.
  4. yoy with prior == 0 → NaN.
  5. merge_fundamentals_asof works on a tz-aware (UTC) daily frame.
"""

import math
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pandas as pd
import pytest

from app.ml.features.feature_pipeline import (
    compute_features_for_symbol,
    get_all_feature_names,
    zscore_feature_cols,
)
from app.ml.features.fundamental_features import (
    FUNDAMENTAL_FEATURE_NAMES,
    FUNDAMENTAL_FEATURE_NAMES_V2,
    FUNDAMENTAL_REPORTING_LAG_DAYS,
    compute_fundamental_features_series,
    get_fundamental_feature_names,
    merge_fundamentals_asof,
)

LAG = FUNDAMENTAL_REPORTING_LAG_DAYS
END = date(2026, 6, 30)


def _income(period: date, revenue: float, op: float, net: float) -> SimpleNamespace:
    return SimpleNamespace(period_date=period, revenue=revenue, op_profit=op, net_profit=net)


def _balance(period: date, nw: float, ta: float, tl: float) -> SimpleNamespace:
    return SimpleNamespace(period_date=period, net_worth=nw, total_asset=ta, total_liability=tl)


def _cashflow(period: date, ocf: float) -> SimpleNamespace:
    return SimpleNamespace(period_date=period, operating_cf=ocf)


def _holdings(period: date, promoters: float, fii: float) -> SimpleNamespace:
    return SimpleNamespace(period_date=period, promoters_pct=promoters, fii_pct=fii)


def _db_returning(is_rows, bs_rows, cf_rows, sh_rows) -> AsyncMock:
    """Mock session whose four bulk queries return the given row sets in order."""
    db = AsyncMock()
    results = []
    for rows in (is_rows, bs_rows, cf_rows, sh_rows):
        res = MagicMock()
        res.scalars.return_value.all.return_value = rows
        results.append(res)
    db.execute.side_effect = results
    return db


FY22, FY23 = date(2022, 3, 31), date(2023, 3, 31)

INCOME = [_income(FY22, 1000.0, 200.0, 100.0), _income(FY23, 1200.0, 300.0, 150.0)]
BALANCE = [_balance(FY22, 1200.0, 2000.0, 800.0), _balance(FY23, 1500.0, 2500.0, 1000.0)]
CASHFLOW = [_cashflow(FY22, 80.0), _cashflow(FY23, 120.0)]
HOLDINGS = [_holdings(FY22, 50.0, 10.0), _holdings(FY23, 52.0, 11.0)]


async def _standard_series() -> pd.DataFrame:
    db = _db_returning(INCOME, BALANCE, CASHFLOW, HOLDINGS)
    return await compute_fundamental_features_series("NSE_EQ|TEST", date(2020, 1, 1), END, db)


# ─── Feature-set contract ────────────────────────────────────────────────────

class TestFeatureSetContract:
    def test_v2_has_17_features_without_snapshot_ratios(self):
        assert len(FUNDAMENTAL_FEATURE_NAMES_V2) == 17
        for dropped in ("pe_ratio", "pb_ratio", "ev_ebitda"):
            assert dropped not in FUNDAMENTAL_FEATURE_NAMES_V2

    def test_legacy_default_is_byte_identical(self):
        assert get_fundamental_feature_names() == FUNDAMENTAL_FEATURE_NAMES
        assert len(get_fundamental_feature_names()) == 20

    def test_version_dispatch(self):
        assert get_fundamental_feature_names("2.0.0") == FUNDAMENTAL_FEATURE_NAMES_V2

    async def test_series_columns_are_effective_date_plus_v2(self):
        series = await _standard_series()
        assert list(series.columns) == ["effective_date", *FUNDAMENTAL_FEATURE_NAMES_V2]


# ─── PIT correctness ─────────────────────────────────────────────────────────

class TestPointInTimeCorrectness:
    async def test_effective_date_is_period_plus_lag(self):
        series = await _standard_series()
        expected = pd.to_datetime([FY22, FY23]) + pd.Timedelta(days=LAG)
        assert list(series["effective_date"]) == list(expected)

    async def test_day_before_lag_carries_previous_period(self):
        """The daily frame flips to FY23 values exactly at period_date + lag."""
        series = await _standard_series()
        fy23_known = pd.Timestamp(FY23) + pd.Timedelta(days=LAG)
        daily = pd.DataFrame({
            "timestamp": [fy23_known - pd.Timedelta(days=1), fy23_known],
            "close": [1.0, 1.0],
        })
        merged = merge_fundamentals_asof(daily, series)

        # Day before: FY22 statement is the newest the market knows.
        assert merged["roe"].iloc[0] == pytest.approx(100.0 / 1200.0)
        # Flip day: FY23 becomes known.
        assert merged["roe"].iloc[1] == pytest.approx(150.0 / 1500.0)

    async def test_mutating_future_quarter_never_changes_earlier_rows(self):
        baseline = await _standard_series()

        fy24 = date(2024, 3, 31)
        db = _db_returning(
            INCOME + [_income(fy24, 99999.0, 88888.0, 77777.0)],
            BALANCE + [_balance(fy24, 9e6, 9e7, 9e5)],
            CASHFLOW + [_cashflow(fy24, 5e6)],
            HOLDINGS + [_holdings(fy24, 99.0, 0.5)],
        )
        extended = await compute_fundamental_features_series(
            "NSE_EQ|TEST", date(2020, 1, 1), END, db
        )

        pd.testing.assert_frame_equal(
            extended.iloc[: len(baseline)].reset_index(drop=True), baseline
        )

    async def test_rows_before_first_effective_date_stay_nan(self):
        series = await _standard_series()
        first_known = pd.Timestamp(FY22) + pd.Timedelta(days=LAG)
        daily = pd.DataFrame({
            "timestamp": [first_known - pd.Timedelta(days=200)],
            "close": [1.0],
        })
        merged = merge_fundamentals_asof(daily, series)
        assert merged[FUNDAMENTAL_FEATURE_NAMES_V2].isna().all().all()


# ─── Reconstructed ratios ────────────────────────────────────────────────────

class TestReconstructedRatios:
    async def test_roe_and_roce_formulas_exact(self):
        series = await _standard_series()
        # Row 0 = FY22, row 1 = FY23.
        assert series["roe"].iloc[0] == pytest.approx(100.0 / 1200.0)
        assert series["roe"].iloc[1] == pytest.approx(150.0 / 1500.0)
        assert series["roce"].iloc[0] == pytest.approx(200.0 / 2000.0)
        assert series["roce"].iloc[1] == pytest.approx(300.0 / 2500.0)

    async def test_non_positive_denominators_are_nan(self):
        db = _db_returning(
            [_income(FY22, 1000.0, 200.0, 100.0)],
            [_balance(FY22, -50.0, 0.0, 800.0)],  # negative equity, zero assets
            [], [],
        )
        series = await compute_fundamental_features_series(
            "NSE_EQ|TEST", date(2020, 1, 1), END, db
        )
        assert math.isnan(series["roe"].iloc[0])
        assert math.isnan(series["roce"].iloc[0])


# ─── Growth semantics ────────────────────────────────────────────────────────

class TestGrowthSemantics:
    async def test_yoy_with_zero_prior_is_nan(self):
        db = _db_returning(
            [_income(FY22, 0.0, 10.0, 0.0), _income(FY23, 500.0, 50.0, 25.0)],
            [], [], [],
        )
        series = await compute_fundamental_features_series(
            "NSE_EQ|TEST", date(2020, 1, 1), END, db
        )
        assert math.isnan(series["revenue_growth_yoy"].iloc[1])
        assert math.isnan(series["profit_growth_yoy"].iloc[1])

    async def test_yoy_and_cagr_match_legacy_values(self):
        series = await _standard_series()
        # First period has no prior.
        assert math.isnan(series["revenue_growth_yoy"].iloc[0])
        assert series["revenue_growth_yoy"].iloc[1] == pytest.approx(0.2)
        assert series["profit_growth_yoy"].iloc[1] == pytest.approx(0.5)
        # CAGR over 1 interval == growth rate.
        assert series["revenue_cagr"].iloc[1] == pytest.approx(0.2)
        assert series["operating_cf_growth"].iloc[1] == pytest.approx(0.5)
        assert series["promoter_holding_change"].iloc[1] == pytest.approx(2.0)


# ─── tz-aware merge ──────────────────────────────────────────────────────────

class TestTimezoneAwareMerge:
    async def test_tz_aware_utc_frame_merges(self):
        """OHLCV timestamps are tz-aware UTC; merge_asof must not raise."""
        series = await _standard_series()
        fy23_known = pd.Timestamp(FY23, tz="UTC") + pd.Timedelta(days=LAG)
        daily = pd.DataFrame({
            "timestamp": pd.to_datetime(
                [fy23_known - pd.Timedelta(days=1), fy23_known, fy23_known + pd.Timedelta(days=30)]
            ),
            "close": [1.0, 2.0, 3.0],
        })
        merged = merge_fundamentals_asof(daily, series)

        assert len(merged) == 3
        assert merged["roe"].iloc[0] == pytest.approx(100.0 / 1200.0)
        assert merged["roe"].iloc[1] == pytest.approx(150.0 / 1500.0)
        assert merged["roe"].iloc[2] == pytest.approx(150.0 / 1500.0)
        # Original columns intact, helper keys gone.
        assert "close" in merged.columns
        assert "_asof_key" not in merged.columns
        assert "effective_date" not in merged.columns

    async def test_empty_series_yields_all_nan_columns(self):
        db = _db_returning([], [], [], [])
        series = await compute_fundamental_features_series(
            "NSE_EQ|TEST", date(2020, 1, 1), END, db
        )
        assert series.empty

        daily = pd.DataFrame({
            "timestamp": pd.to_datetime(["2026-01-01"], utc=True),
            "close": [1.0],
        })
        merged = merge_fundamentals_asof(daily, series)
        assert merged[FUNDAMENTAL_FEATURE_NAMES_V2].isna().all().all()


# ─── Commit 8: version wiring through the pipeline ───────────────────────────

class TestFeatureSetVersionWiring:
    def test_all_feature_name_counts(self):
        assert len(get_all_feature_names()) == 69                              # v1 default
        assert len(get_all_feature_names(feature_set_version="2.0.0")) == 66
        assert len(get_all_feature_names(include_fundamentals=False)) == 49

    def test_zscore_cols_exclude_v2_fundamentals_only(self):
        v2_names = get_all_feature_names(feature_set_version="2.0.0")
        cols = zscore_feature_cols(v2_names, "2.0.0")
        assert len(cols) == 49
        assert not set(cols) & set(FUNDAMENTAL_FEATURE_NAMES_V2)
        # v1: identity — nothing excluded.
        v1_names = get_all_feature_names()
        assert zscore_feature_cols(v1_names, "1.0.0") == v1_names

    async def test_compute_features_for_symbol_v2_merges_pit_series(self):
        """End-to-end through the symbol pipeline: v2 rows carry PIT
        fundamentals, and the NaN-fill never touches fundamental columns."""
        fy23_known = pd.Timestamp(FY23, tz="UTC") + pd.Timedelta(days=LAG)
        n_days = 10
        ts = pd.date_range(fy23_known - pd.Timedelta(days=5), periods=n_days, tz="UTC")
        ohlcv_rows = [
            SimpleNamespace(
                timestamp=t, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000
            )
            for t in ts
        ]

        db = AsyncMock()
        results = []
        for rows in (ohlcv_rows, INCOME, BALANCE, CASHFLOW, HOLDINGS):
            res = MagicMock()
            res.scalars.return_value.all.return_value = rows
            results.append(res)
        db.execute.side_effect = results

        features_df = await compute_features_for_symbol(
            "NSE_EQ|TEST",
            (fy23_known - pd.Timedelta(days=400)).to_pydatetime(),
            (fy23_known + pd.Timedelta(days=5)).to_pydatetime(),
            "1D", db,
            include_sentiment=False,
            include_fundamentals=True,
            feature_set_version="2.0.0",
        )

        assert len(features_df) == n_days
        # pe/pb/ev never appear under v2.
        for dropped in ("pe_ratio", "pb_ratio", "ev_ebitda"):
            assert dropped not in features_df.columns
        # Before the FY23 effective date: FY22 roe; on/after: FY23 roe —
        # and the non-fundamental NaN-fill left these NaN-capable columns alone.
        pre = features_df[features_df["timestamp"] < fy23_known]
        post = features_df[features_df["timestamp"] >= fy23_known]
        assert pre["roe"].to_numpy() == pytest.approx(100.0 / 1200.0)
        assert post["roe"].to_numpy() == pytest.approx(150.0 / 1500.0)

    async def test_compute_features_for_symbol_v1_unchanged(self):
        """Default path still runs the legacy broadcast (key-ratios query)."""
        ts = pd.date_range("2026-06-01", periods=5, tz="UTC")
        ohlcv_rows = [
            SimpleNamespace(
                timestamp=t, open=100.0, high=101.0, low=99.0, close=100.5, volume=1000
            )
            for t in ts
        ]
        db = AsyncMock()
        ohlcv_res = MagicMock()
        ohlcv_res.scalars.return_value.all.return_value = ohlcv_rows
        legacy_res = MagicMock()
        legacy_res.scalar_one_or_none.return_value = None      # key ratios
        legacy_res.scalars.return_value.all.return_value = []  # statements
        db.execute.side_effect = [ohlcv_res] + [legacy_res] * 5

        features_df = await compute_features_for_symbol(
            "NSE_EQ|TEST", datetime(2026, 5, 1), datetime(2026, 6, 6), "1D", db,
            include_sentiment=False,
            include_fundamentals=True,
        )
        # Legacy 20-name broadcast contract intact (columns exist, incl. pe).
        assert "pe_ratio" in features_df.columns
        assert set(FUNDAMENTAL_FEATURE_NAMES) <= set(features_df.columns)
