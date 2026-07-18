"""
Regression tests: training symbol selection is STOCK-only (2026-07-17).

The selectors ranked purely by liquidity from upstox_ohlcv with no
instrument_master join, so highly liquid ETFs sailed into the training
universe — 229 ETF_FUND + 14 UNCLASSIFIED landed in a live 2,245-symbol
run before the gate existed. Both selection tracks must semi-join the
active-STOCK universe, and UNCLASSIFIED must stay excluded (fail-closed).
"""

from app.ml.features.symbol_selector import _tradeable_stock_subquery
from app.services.instrument_classifier import AssetClass


class TestTradeableStockSubquery:
    def test_filters_active_stock_only(self):
        sql = str(_tradeable_stock_subquery().compile(compile_kwargs={"literal_binds": True}))
        assert "instrument_master" in sql
        assert "is_active IS true" in sql or "is_active = true" in sql.lower()
        assert f"'{AssetClass.STOCK.value}'" in sql

    def test_unclassified_and_funds_are_not_whitelisted(self):
        """Fail-closed: only the STOCK literal may appear as an allowed class."""
        sql = str(_tradeable_stock_subquery().compile(compile_kwargs={"literal_binds": True}))
        assert AssetClass.ETF_FUND.value not in sql
        assert AssetClass.UNCLASSIFIED.value not in sql


class TestSelectionQueriesCarryTheGate:
    def _compiled(self, fn_source: str) -> None:
        pass

    def test_both_tracks_semi_join_the_stock_universe(self):
        """Source-level guard: every aggregate selection over upstox_ohlcv in
        symbol_selector must reference the tradeable-stock subquery."""
        import inspect
        import app.ml.features.symbol_selector as mod

        for fn_name in ("get_top_liquid_symbols", "get_recently_listed_symbols"):
            source = inspect.getsource(getattr(mod, fn_name))
            assert "_tradeable_stock_subquery" in source, (
                f"{fn_name} no longer applies the STOCK-only gate — "
                f"ETFs will re-enter the training universe"
            )
