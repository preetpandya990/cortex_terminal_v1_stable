"""
Credibility scorer tests
========================
WS3: the scorer goes from orphaned (zero call sites) to load-bearing — it now
feeds fake-news layer 1, sentiment source weights, and RAG ranking. These
tests pin the concurrency-safety mechanics (row lock on update, conflict-safe
create) and the batch loader's cache behavior.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.intelligence import credibility_scorer as cs
from app.ai.intelligence.credibility_scorer import (
    CredibilityScorer,
    load_credibility_scores,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clear_score_cache():
    cs._score_cache.clear()
    yield
    cs._score_cache.clear()


class TestGetOrCreateSource:
    @pytest.mark.asyncio
    async def test_create_uses_conflict_safe_insert_with_type_seed(self):
        insert_result = MagicMock(rowcount=1)
        select_result = MagicMock()
        select_result.scalar_one.return_value = MagicMock(source_name="NSE Corporate Filings")
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[insert_result, select_result])

        source = await CredibilityScorer().get_or_create_source(
            db, "NSE Corporate Filings", "exchange"
        )

        assert source.source_name == "NSE Corporate Filings"
        insert_stmt = db.execute.await_args_list[0].args[0]
        compiled = str(
            insert_stmt.compile(
                dialect=__import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
                ).dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "ON CONFLICT (source_name) DO NOTHING" in compiled
        assert "80.0" in compiled  # exchange seed score

    @pytest.mark.asyncio
    async def test_news_type_seeds_neutral(self):
        insert_result = MagicMock(rowcount=1)
        select_result = MagicMock()
        select_result.scalar_one.return_value = MagicMock()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[insert_result, select_result])

        await CredibilityScorer().get_or_create_source(db, "Moneycontrol", "news")

        compiled = str(
            db.execute.await_args_list[0].args[0].compile(
                dialect=__import__(
                    "sqlalchemy.dialects.postgresql", fromlist=["dialect"]
                ).dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert "50.0" in compiled


class TestUpdateCredibility:
    def _db_with_source(self, source) -> AsyncMock:
        result = MagicMock()
        result.scalar_one_or_none.return_value = source
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_select_takes_row_lock(self):
        source = MagicMock(total_events=0, confirmed_events=0, contradicted_events=0)
        db = self._db_with_source(source)

        await CredibilityScorer().update_credibility(db, "Moneycontrol", confirmed=True)

        compiled = str(db.execute.await_args.args[0].compile())
        assert "FOR UPDATE" in compiled

    @pytest.mark.asyncio
    async def test_confirmed_outcome_arithmetic(self):
        source = MagicMock(total_events=3, confirmed_events=2, contradicted_events=1)
        db = self._db_with_source(source)

        await CredibilityScorer().update_credibility(db, "Moneycontrol", confirmed=True)

        assert source.total_events == 4
        assert source.confirmed_events == 3
        assert source.contradicted_events == 1
        assert source.credibility_score == Decimal("75.0")
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_contradicted_outcome_arithmetic(self):
        source = MagicMock(total_events=1, confirmed_events=1, contradicted_events=0)
        db = self._db_with_source(source)

        await CredibilityScorer().update_credibility(db, "Moneycontrol", confirmed=False)

        assert source.total_events == 2
        assert source.contradicted_events == 1
        assert source.credibility_score == Decimal("50.0")

    @pytest.mark.asyncio
    async def test_unknown_source_skipped_never_created(self):
        db = self._db_with_source(None)

        result = await CredibilityScorer().update_credibility(
            db, "https://arbitrary-api-input.example", confirmed=True
        )

        assert result is None
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_update_invalidates_score_cache(self):
        cs._score_cache["Moneycontrol"] = (50.0, float("inf"))
        source = MagicMock(total_events=0, confirmed_events=0, contradicted_events=0)
        db = self._db_with_source(source)

        await CredibilityScorer().update_credibility(db, "Moneycontrol", confirmed=True)

        assert "Moneycontrol" not in cs._score_cache


class TestLoadCredibilityScores:
    def _db_returning(self, rows: list[tuple[str, Decimal]]) -> AsyncMock:
        result = MagicMock()
        result.all.return_value = rows
        db = AsyncMock()
        db.execute = AsyncMock(return_value=result)
        return db

    @pytest.mark.asyncio
    async def test_batch_load_and_cache(self):
        db = self._db_returning([("A", Decimal("80.0")), ("B", Decimal("40.0"))])

        scores = await load_credibility_scores(db, ["A", "B", "A"])
        assert scores == {"A": 80.0, "B": 40.0}
        assert db.execute.await_count == 1

        # Second call: fully served from the in-process cache, no query.
        scores2 = await load_credibility_scores(db, ["A", "B"])
        assert scores2 == scores
        assert db.execute.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_names_absent_and_not_negative_cached(self):
        db = self._db_returning([])

        scores = await load_credibility_scores(db, ["Unknown"])
        assert scores == {}

        # Unknown name queried again next call (no negative caching) so a
        # newly registered source is picked up promptly.
        await load_credibility_scores(db, ["Unknown"])
        assert db.execute.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_input_no_query(self):
        db = self._db_returning([])
        assert await load_credibility_scores(db, []) == {}
        db.execute.assert_not_awaited()
