"""
Demand registry tests
=====================
WS6: the in-demand symbol set (watchlist ∪ active suggestions, all identity
forms), its lazy Redis rebuild, and the fail-open policy.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.demand_registry import (
    DEMAND_FRESH_KEY,
    DEMAND_SET_KEY,
    is_in_demand,
    load_in_demand_symbols,
    refresh_demand_set,
)

pytestmark = pytest.mark.unit


def _db_returning(rows: list[tuple]) -> AsyncMock:
    result = MagicMock()
    result.all.return_value = rows
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _redis(fresh: bool, member: bool = False) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value="1" if fresh else None)
    redis.sismember = AsyncMock(return_value=member)
    pipe = MagicMock()
    pipe.delete = MagicMock()
    pipe.sadd = MagicMock()
    pipe.expire = MagicMock()
    pipe.set = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=pipe)
    return redis


class TestLoadInDemandSymbols:
    @pytest.mark.asyncio
    async def test_union_of_all_identity_forms(self):
        db = _db_returning([
            ("NSE_EQ|INE002A01018", "RELIANCE"),   # watchlist row
            ("NSE_EQ|INE009A01021", "INFY"),        # active suggestion row
            ("NSE_EQ|INE009A01021", None),          # suggestion.symbol form
        ])
        symbols = await load_in_demand_symbols(db)
        assert symbols == {
            "NSE_EQ|INE002A01018", "RELIANCE",
            "NSE_EQ|INE009A01021", "INFY",
        }

    @pytest.mark.asyncio
    async def test_nulls_and_empties_filtered(self):
        db = _db_returning([(None, None), ("", ""), ("TCS", None)])
        assert await load_in_demand_symbols(db) == {"TCS"}


class TestRefreshDemandSet:
    @pytest.mark.asyncio
    async def test_populates_set_then_arms_freshness_key(self):
        db = _db_returning([("RELIANCE", None)])
        redis = _redis(fresh=False)

        symbols = await refresh_demand_set(redis, db)

        assert symbols == {"RELIANCE"}
        pipe = redis.pipeline.return_value
        pipe.delete.assert_called_once_with(DEMAND_SET_KEY)
        pipe.sadd.assert_called_once_with(DEMAND_SET_KEY, "RELIANCE")
        pipe.set.assert_called_once()
        assert pipe.set.call_args.args[0] == DEMAND_FRESH_KEY
        pipe.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_demand_set_still_arms_freshness(self):
        """No watchers + no active suggestions → empty set, no sadd, but the
        freshness key still arms (otherwise every check re-queries the DB)."""
        db = _db_returning([])
        redis = _redis(fresh=False)

        symbols = await refresh_demand_set(redis, db)

        assert symbols == set()
        pipe = redis.pipeline.return_value
        pipe.sadd.assert_not_called()
        pipe.set.assert_called_once()


class TestIsInDemand:
    @pytest.mark.asyncio
    async def test_fresh_set_serves_without_rebuild(self):
        db = _db_returning([])
        redis = _redis(fresh=True, member=True)

        assert await is_in_demand(redis, db, "RELIANCE") is True
        db.execute.assert_not_awaited()  # no rebuild while fresh
        redis.sismember.assert_awaited_once_with(DEMAND_SET_KEY, "RELIANCE")

    @pytest.mark.asyncio
    async def test_stale_set_triggers_rebuild(self):
        db = _db_returning([("RELIANCE", None)])
        redis = _redis(fresh=False, member=True)

        assert await is_in_demand(redis, db, "RELIANCE") is True
        db.execute.assert_awaited_once()  # rebuild happened

    @pytest.mark.asyncio
    async def test_non_member_is_not_in_demand(self):
        redis = _redis(fresh=True, member=False)
        assert await is_in_demand(redis, _db_returning([]), "UNWATCHED") is False

    @pytest.mark.asyncio
    async def test_empty_symbol_never_in_demand(self):
        redis = _redis(fresh=True, member=True)
        assert await is_in_demand(redis, _db_returning([]), "") is False
        redis.sismember.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fails_open_on_redis_error(self):
        """Infrastructure failure must degrade to pre-gating behavior
        (forecast allowed), never silently starve watched symbols."""
        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        assert await is_in_demand(redis, _db_returning([]), "RELIANCE") is True

    @pytest.mark.asyncio
    async def test_fails_open_on_db_error_during_rebuild(self):
        redis = _redis(fresh=False)
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=RuntimeError("db down"))

        assert await is_in_demand(redis, db, "RELIANCE") is True
