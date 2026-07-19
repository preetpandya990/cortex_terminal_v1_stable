"""
Unit tests for the Portfolio Insight stats service (B4):
``app.services.paper_trading.portfolio_insight_service``.

Pure statistic helpers are tested with hand-verified numbers; the async
orchestrator is tested with its three I/O helpers monkeypatched, so no DB is
touched. Correlation/σ use small deterministic returns frames.

Marked ``unit``: pure math + monkeypatched I/O, no real DB/network.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import numpy as np
import pandas as pd
import pytest

from app.services.paper_trading import portfolio_insight_service as svc
from app.services.paper_trading.portfolio_insight_service import _PositionView

pytestmark = pytest.mark.unit


def _v(symbol, key, side, qty, avg_cost, stop, last):
    mv = last * qty
    return _PositionView(
        symbol=symbol, instrument_key=key, side=side, quantity=qty,
        avg_cost=avg_cost, stop_loss=stop, last_price=last,
        market_value=mv, signed_exposure=(mv if side == "LONG" else -mv),
    )


# 3-position book used across tests: PV = 5000.
def _book():
    return [
        _v("RELIANCE", "NSE_EQ|A", "LONG", 10, 100.0, 95.0, 110.0),   # mv 1100, CaR 50
        _v("ONGC", "NSE_EQ|B", "LONG", 5, 200.0, None, 210.0),        # mv 1050, no stop
        _v("INFY", "NSE_EQ|C", "SHORT", 4, 150.0, 160.0, 145.0),      # mv 580, CaR 40, short
    ]


PV = 5000.0
_SECTORS = {"NSE_EQ|A": "Energy", "NSE_EQ|B": "Energy", "NSE_EQ|C": "Unclassified"}


# ──────────────────────────────────────────────────────────────────────────────
# Capital at risk
# ──────────────────────────────────────────────────────────────────────────────

class TestCapitalAtRisk:
    def test_sums_stopped_only(self):
        car = svc._capital_at_risk(_book(), PV)
        assert car.capital_at_risk == pytest.approx(90.0)     # 50 (RELIANCE) + 40 (INFY)
        assert car.capital_at_risk_pct == pytest.approx(1.8)
        assert car.positions_with_stop == 2
        assert car.positions_without_stop == 1

    def test_zero_pv_guard(self):
        car = svc._capital_at_risk(_book(), 0.0)
        assert car.capital_at_risk_pct == 0.0
        assert car.capital_at_risk == pytest.approx(90.0)

    def test_all_unstopped(self):
        views = [_v("X", "K", "LONG", 1, 100.0, None, 100.0)]
        car = svc._capital_at_risk(views, PV)
        assert car.capital_at_risk == 0.0
        assert car.positions_without_stop == 1


# ──────────────────────────────────────────────────────────────────────────────
# Single-name concentration (max weight of PV, HHI/effective on invested weights)
# ──────────────────────────────────────────────────────────────────────────────

class TestSingleName:
    def test_weights_and_hhi(self):
        sn = svc._single_name_concentration(_book(), PV)
        assert sn.max_weight_symbol == "RELIANCE"
        assert sn.max_weight_pct == pytest.approx(22.0)        # 1100/5000
        # invested weights: 1100/2730, 1050/2730, 580/2730 → HHI ≈ 0.3554
        assert sn.hhi == pytest.approx(0.3554, abs=1e-3)
        assert sn.effective_positions == pytest.approx(1.0 / 0.3554, abs=0.02)
        assert [h.symbol for h in sn.top_holdings] == ["RELIANCE", "ONGC", "INFY"]

    def test_single_position_hhi_is_one(self):
        sn = svc._single_name_concentration([_v("X", "K", "LONG", 1, 100.0, 90.0, 100.0)], PV)
        assert sn.hhi == pytest.approx(1.0)
        assert sn.effective_positions == pytest.approx(1.0)

    def test_zero_pv_guard(self):
        sn = svc._single_name_concentration(_book(), 0.0)
        assert sn.max_weight_pct == 0.0
        # HHI uses invested weights, independent of PV — still valid.
        assert sn.hhi > 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Sector concentration
# ──────────────────────────────────────────────────────────────────────────────

class TestSector:
    def test_grouping_and_unclassified(self):
        sc = svc._sector_concentration(_book(), _SECTORS, PV)
        assert sc.max_sector == "Energy"
        assert sc.max_sector_weight_pct == pytest.approx(43.0)   # (1100+1050)/5000
        assert sc.unclassified_weight_pct == pytest.approx(11.6)  # 580/5000
        assert sc.breakdown[0].sector == "Energy"

    def test_all_unclassified_has_no_top_sector(self):
        sectors = {k: "Unclassified" for k in _SECTORS}
        sc = svc._sector_concentration(_book(), sectors, PV)
        assert sc.max_sector is None
        assert sc.max_sector_weight_pct == 0.0
        assert sc.unclassified_weight_pct == pytest.approx(54.6)  # (1100+1050+580)/5000


# ──────────────────────────────────────────────────────────────────────────────
# Correlation + σ
# ──────────────────────────────────────────────────────────────────────────────

def _returns_frame(n=60, seed=0):
    idx = pd.date_range("2026-01-01", periods=n, freq="D")
    rng = np.random.default_rng(seed)
    base = rng.normal(0, 0.01, n)
    return pd.DataFrame(
        {
            "NSE_EQ|A": base + rng.normal(0, 0.001, n),   # A,B share `base` → high corr
            "NSE_EQ|B": base + rng.normal(0, 0.001, n),
            "NSE_EQ|C": rng.normal(0, 0.01, n),           # independent
        },
        index=idx,
    )


class TestCorrelation:
    def test_max_pair_and_avg(self):
        corr = svc._correlation_stat(_book(), _returns_frame())
        assert corr.max_pair == ["RELIANCE", "ONGC"]        # the correlated pair
        assert corr.max_pair_correlation > 0.9
        assert corr.covered_positions == 3
        assert corr.excluded_positions == 0
        assert -1.0 <= corr.avg_pairwise_correlation <= 1.0

    def test_short_history_excluded(self):
        df = _returns_frame(n=60)
        df.loc[df.index[:50], "NSE_EQ|C"] = np.nan   # C left with 10 obs < 30
        corr = svc._correlation_stat(_book(), df)
        assert corr.covered_positions == 2
        assert corr.excluded_positions == 1

    def test_none_frame(self):
        corr = svc._correlation_stat(_book(), None)
        assert corr.max_pair_correlation is None
        assert corr.covered_positions == 0
        assert corr.excluded_positions == 3

    def test_daily_sigma_excludes_short_history(self):
        df = _returns_frame(n=60)
        df.loc[df.index[:50], "NSE_EQ|C"] = np.nan
        sig = svc._daily_sigma(df)
        assert set(sig) == {"NSE_EQ|A", "NSE_EQ|B"}
        assert all(v > 0 for v in sig.values())


# ──────────────────────────────────────────────────────────────────────────────
# Stress scan
# ──────────────────────────────────────────────────────────────────────────────

class TestStress:
    def test_scenarios(self):
        sig = {"NSE_EQ|A": 0.01, "NSE_EQ|B": 0.01, "NSE_EQ|C": 0.01}
        stress = svc._stress_scan(_book(), _SECTORS, sig, PV)
        by_key = {s.key: s for s in stress.scenarios}

        # index: -0.05 * (1100 + 1050 - 580) = -78.5 → -1.57%
        assert by_key["index_down"].delta_pct == pytest.approx(-1.57, abs=1e-2)
        # sector Energy: -0.10 * (1100 + 1050) = -215 → -4.30%
        assert by_key["sector_down"].delta_pct == pytest.approx(-4.30, abs=1e-2)
        assert "Energy" in by_key["sector_down"].label
        # vol: -(2*0.01*1100 + 2*0.01*1050 + 2*0.01*580) = -(22+21+11.6) = -54.6 → -1.092%
        assert by_key["vol_double"].delta_pct == pytest.approx(-1.092, abs=1e-2)

    def test_vol_excludes_missing_sigma(self):
        sig = {"NSE_EQ|A": 0.01}   # B, C missing
        stress = svc._stress_scan(_book(), _SECTORS, sig, PV)
        vol = next(s for s in stress.scenarios if s.key == "vol_double")
        assert "2 excluded" in vol.detail

    def test_no_classified_sector_skips_sector_scenario(self):
        sectors = {k: "Unclassified" for k in _SECTORS}
        stress = svc._stress_scan(_book(), sectors, {}, PV)
        assert not any(s.key == "sector_down" for s in stress.scenarios)

    def test_net_short_gains_on_index_down(self):
        views = [_v("X", "K", "SHORT", 10, 100.0, 110.0, 100.0)]   # signed -1000
        stress = svc._stress_scan(views, {"K": "Unclassified"}, {}, PV)
        idx = next(s for s in stress.scenarios if s.key == "index_down")
        assert idx.delta_pct > 0   # short profits when the market falls


# ──────────────────────────────────────────────────────────────────────────────
# Orchestrator (I/O helpers monkeypatched) + empty
# ──────────────────────────────────────────────────────────────────────────────

class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_full_stitch(self, monkeypatch):
        async def fake_views(session, pid):
            return _book()

        async def fake_sectors(session, views):
            return _SECTORS

        async def fake_returns(session, keys):
            return _returns_frame()

        monkeypatch.setattr(svc, "_load_position_views", fake_views)
        monkeypatch.setattr(svc, "_resolve_sectors", fake_sectors)
        monkeypatch.setattr(svc, "_load_returns_matrix", fake_returns)

        portfolio = SimpleNamespace(id=uuid4(), current_cash=2270.0)  # PV = 2270 + 2730 = 5000
        stats = await svc.compute_portfolio_insight_stats(object(), portfolio)

        assert stats.open_position_count == 3
        assert stats.portfolio_value == pytest.approx(5000.0)
        assert stats.capital_at_risk.capital_at_risk == pytest.approx(90.0)
        assert stats.single_name.max_weight_symbol == "RELIANCE"
        assert stats.sector.max_sector == "Energy"
        assert stats.correlation.max_pair == ["RELIANCE", "ONGC"]
        assert len(stats.stress.scenarios) == 3
        # honest-gap notes surfaced (unstopped ONGC + Unclassified INFY)
        assert any("no stop-loss" in n for n in stats.notes)
        assert any("Unclassified" in n for n in stats.notes)

    @pytest.mark.asyncio
    async def test_empty_portfolio(self, monkeypatch):
        async def fake_views(session, pid):
            return []

        monkeypatch.setattr(svc, "_load_position_views", fake_views)
        portfolio = SimpleNamespace(id=uuid4(), current_cash=100000.0)
        stats = await svc.compute_portfolio_insight_stats(object(), portfolio)

        assert stats.open_position_count == 0
        assert stats.portfolio_value == pytest.approx(100000.0)
        assert stats.capital_at_risk.capital_at_risk == 0.0
        assert stats.stress.scenarios == []
        assert stats.notes == ["No open positions."]
