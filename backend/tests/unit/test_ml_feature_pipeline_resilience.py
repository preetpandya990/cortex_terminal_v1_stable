"""
WS1 — Feature pipeline connection-fault resilience (gate test).

Covers the 2026-07-15 scheduled-retrain crash mode: an asyncpg
``ConnectionDoesNotExistError`` mid-query poisoned the single logical session
and cascaded ``PendingRollbackError`` into every subsequent symbol.

Verifies:
  1. Legacy single-session path rolls back after a per-symbol failure and the
     remaining symbols survive.
  2. Factory path retries the failed symbol exactly once on a fresh session
     (asserted via factory call count); a second failure skips the symbol.
  3. Non-transient errors never trigger a retry.
  4. ``sentiment_features`` rolls back and re-raises transient connection
     errors instead of swallowing them into empty defaults.
  5. ``is_transient_connection_error`` classification, including wrapped
     driver exceptions.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from asyncpg.exceptions import ConnectionDoesNotExistError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.ml.features.db_errors import is_transient_connection_error
from app.ml.features.feature_pipeline import compute_features_batch
from app.ml.features.sentiment_features import SentimentFeatureExtractor

START = datetime(2026, 1, 1)
END = datetime(2026, 6, 1)
SYMBOLS = ["SYM_A", "SYM_B", "SYM_C", "SYM_D"]

COMPUTE_ONE = "app.ml.features.feature_pipeline.compute_features_for_symbol"


def _df() -> pd.DataFrame:
    return pd.DataFrame({"close": [1.0, 2.0]})


def _transient() -> ConnectionDoesNotExistError:
    return ConnectionDoesNotExistError("connection was closed in the middle of operation")


async def _run_batch(db=None, session_factory=None, chunk_size: int = 50):
    return await compute_features_batch(
        SYMBOLS, START, END, "1D", db,
        include_sentiment=False,
        include_fundamentals=False,
        session_factory=session_factory,
        chunk_size=chunk_size,
    )


# ─── is_transient_connection_error classification ────────────────────────────

class TestTransientClassification:
    def test_asyncpg_connection_does_not_exist(self):
        assert is_transient_connection_error(_transient()) is True

    def test_sqlalchemy_operational_error(self):
        exc = OperationalError("SELECT 1", {}, Exception("server closed the connection"))
        assert is_transient_connection_error(exc) is True

    def test_sqlalchemy_interface_error(self):
        exc = InterfaceError("SELECT 1", {}, Exception("connection is closed"))
        assert is_transient_connection_error(exc) is True

    def test_dbapi_error_connection_invalidated(self):
        exc = DBAPIError("SELECT 1", {}, Exception("boom"), connection_invalidated=True)
        assert is_transient_connection_error(exc) is True

    def test_dbapi_error_not_invalidated_is_not_transient(self):
        exc = DBAPIError("SELECT 1", {}, Exception("syntax error"))
        assert is_transient_connection_error(exc) is False

    def test_wrapped_asyncpg_error_via_orig_chain(self):
        # SQLAlchemy surfaces the driver error as .orig on the wrapper.
        exc = DBAPIError("SELECT 1", {}, _transient())
        assert is_transient_connection_error(exc) is True

    def test_wrapped_asyncpg_error_via_cause_chain(self):
        try:
            try:
                raise _transient()
            except ConnectionDoesNotExistError as inner:
                raise RuntimeError("sentiment query failed") from inner
        except RuntimeError as outer:
            assert is_transient_connection_error(outer) is True

    def test_plain_exceptions_are_not_transient(self):
        assert is_transient_connection_error(ValueError("bad input")) is False
        assert is_transient_connection_error(KeyError("missing")) is False


# ─── Legacy single-session path ──────────────────────────────────────────────

class TestLegacyPathRollback:
    async def test_failed_symbol_rolls_back_and_others_survive(self):
        db = AsyncMock()
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            compute.side_effect = [_df(), _transient(), _df(), _df()]
            results = await _run_batch(db=db)

        db.rollback.assert_awaited_once()
        assert set(results) == {"SYM_A", "SYM_C", "SYM_D"}

    async def test_rollback_failure_does_not_abort_the_batch(self):
        # Rollback on a dead connection can itself raise — the loop must survive.
        db = AsyncMock()
        db.rollback.side_effect = InterfaceError("ROLLBACK", {}, Exception("dead"))
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            compute.side_effect = [_df(), _transient(), _df(), _df()]
            results = await _run_batch(db=db)

        assert set(results) == {"SYM_A", "SYM_C", "SYM_D"}

    async def test_requires_db_or_factory(self):
        with pytest.raises(ValueError, match="db.*session_factory"):
            await _run_batch(db=None, session_factory=None)


# ─── Factory (session-per-chunk) path ────────────────────────────────────────

def _factory() -> MagicMock:
    return MagicMock(side_effect=lambda: AsyncMock())


class TestFactoryPathRetry:
    async def test_transient_error_retries_once_on_fresh_session(self):
        factory = _factory()
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            # SYM_B fails transiently, then succeeds on the retry.
            compute.side_effect = [_df(), _transient(), _df(), _df(), _df()]
            results = await _run_batch(session_factory=factory)

        # 1 chunk session + 1 fresh retry session.
        assert factory.call_count == 2
        assert set(results) == set(SYMBOLS)
        # Retry targeted the failed symbol: call 2 and 3 are both SYM_B.
        assert compute.await_args_list[1].args[0] == "SYM_B"
        assert compute.await_args_list[2].args[0] == "SYM_B"

    async def test_second_transient_failure_skips_symbol(self):
        factory = _factory()
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            compute.side_effect = [_df(), _transient(), _transient(), _df(), _df()]
            results = await _run_batch(session_factory=factory)

        # Chunk session + retry session + post-retry-failure replacement.
        assert factory.call_count == 3
        assert set(results) == {"SYM_A", "SYM_C", "SYM_D"}

    async def test_non_transient_error_never_retries(self):
        factory = _factory()
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            compute.side_effect = [_df(), ValueError("bad data"), _df(), _df()]
            results = await _run_batch(session_factory=factory)

        assert factory.call_count == 1  # no fresh session was ever needed
        assert compute.await_count == 4  # one attempt per symbol, no retry
        assert set(results) == {"SYM_A", "SYM_C", "SYM_D"}

    async def test_chunking_opens_one_session_per_chunk_and_closes_all(self):
        sessions: list[AsyncMock] = []

        def make_session() -> AsyncMock:
            s = AsyncMock()
            sessions.append(s)
            return s

        factory = MagicMock(side_effect=make_session)
        with patch(COMPUTE_ONE, new_callable=AsyncMock) as compute:
            compute.return_value = _df()
            results = await _run_batch(session_factory=factory, chunk_size=2)

        assert factory.call_count == 2  # 4 symbols / chunk_size 2
        assert set(results) == set(SYMBOLS)
        for s in sessions:
            s.close.assert_awaited()


# ─── Sentiment features: rollback + re-raise transients ─────────────────────

class TestSentimentTransientPropagation:
    async def test_fetch_rolls_back_and_reraises_transient(self):
        db = AsyncMock()
        db.execute.side_effect = _transient()
        extractor = SentimentFeatureExtractor()

        with pytest.raises(ConnectionDoesNotExistError):
            await extractor._fetch_sentiment_data("SYM_A", START, END, db)
        db.rollback.assert_awaited()

    async def test_extract_features_propagates_transient(self):
        db = AsyncMock()
        db.execute.side_effect = _transient()
        extractor = SentimentFeatureExtractor()

        with pytest.raises(ConnectionDoesNotExistError):
            await extractor.extract_features("SYM_A", START, END, db)

    async def test_non_transient_error_still_returns_neutral_defaults(self):
        db = AsyncMock()
        db.execute.side_effect = ValueError("mapper misconfigured")
        extractor = SentimentFeatureExtractor()

        features = await extractor.extract_features("SYM_A", START, END, db)

        db.rollback.assert_awaited()
        assert not features.empty
        assert (features.astype(float) == 0.0).all().all()
