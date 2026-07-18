"""
WS4 — tests for scripts/purge_test_model_rows.py.

Verifies dry-run vs execute semantics, the protected-model-type guard, and
the sanity cap. DB access is mocked per the ML-unit convention.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.purge_test_model_rows import (
    DEFAULT_SANITY_CAP,
    PurgeAborted,
    assert_candidates_safe,
    run,
)

RUN_MODULE = "scripts.purge_test_model_rows"


def _model(id_: int, name: str, model_type: str = "lstm") -> MagicMock:
    m = MagicMock()
    m.id = id_
    m.model_name = name
    m.model_type = model_type
    m.deployment_state = "retired"
    m.model_version = "1.0.0"
    return m


def _test_rows(n: int = 5) -> list[MagicMock]:
    return [_model(i, f"test_drift_model_{1776181766 + i}") for i in range(1, n + 1)]


def _patched_session() -> tuple[AsyncMock, MagicMock]:
    """AsyncMock session wrapped in a context-manager factory."""
    session = AsyncMock()
    factory = MagicMock()
    factory.return_value.__aenter__ = AsyncMock(return_value=session)
    factory.return_value.__aexit__ = AsyncMock(return_value=False)
    return session, factory


# ─── Safety guards (pure) ────────────────────────────────────────────────────

class TestSafetyGuards:
    def test_protected_model_type_aborts(self):
        candidates = _test_rows(2) + [_model(6, "cortex_xgboost_1d", "xgboost")]
        with pytest.raises(PurgeAborted, match="protected"):
            assert_candidates_safe(candidates, DEFAULT_SANITY_CAP)

    def test_gru_is_also_protected(self):
        with pytest.raises(PurgeAborted, match="protected"):
            assert_candidates_safe([_model(7, "cortex_gru_1d", "gru")], DEFAULT_SANITY_CAP)

    def test_sanity_cap_aborts_mass_delete(self):
        with pytest.raises(PurgeAborted, match="sanity cap"):
            assert_candidates_safe(_test_rows(21), cap=20)

    def test_normal_selection_passes(self):
        assert_candidates_safe(_test_rows(5), DEFAULT_SANITY_CAP)  # no raise


# ─── run(): dry-run vs execute ───────────────────────────────────────────────

class TestRun:
    async def test_dry_run_lists_but_never_deletes(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
            patch(f"{RUN_MODULE}.count_linked_drift_reports", new_callable=AsyncMock) as counts,
            patch(f"{RUN_MODULE}.purge", new_callable=AsyncMock) as purge_fn,
        ):
            find.return_value = _test_rows(5)
            counts.return_value = {i: 341 for i in range(1, 6)}

            exit_code = await run(dry_run=True)

        assert exit_code == 0
        purge_fn.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_execute_deletes_and_commits(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
            patch(f"{RUN_MODULE}.count_linked_drift_reports", new_callable=AsyncMock) as counts,
            patch(f"{RUN_MODULE}.purge", new_callable=AsyncMock) as purge_fn,
        ):
            find.return_value = _test_rows(5)
            counts.return_value = {i: 341 for i in range(1, 6)}
            purge_fn.return_value = (1705, 5)

            exit_code = await run(dry_run=False)

        assert exit_code == 0
        purge_fn.assert_awaited_once_with(session, [1, 2, 3, 4, 5])
        session.commit.assert_awaited_once()

    async def test_empty_table_is_success_noop(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
        ):
            find.return_value = []
            exit_code = await run(dry_run=False)

        assert exit_code == 0
        session.commit.assert_not_awaited()

    async def test_protected_match_aborts_with_error_exit_and_no_write(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
            patch(f"{RUN_MODULE}.purge", new_callable=AsyncMock) as purge_fn,
        ):
            find.return_value = [_model(6, "cortex_xgboost_1d", "xgboost")]
            exit_code = await run(dry_run=False)

        assert exit_code == 2
        purge_fn.assert_not_awaited()
        session.commit.assert_not_awaited()

    async def test_cap_exceeded_aborts_even_on_execute(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
            patch(f"{RUN_MODULE}.purge", new_callable=AsyncMock) as purge_fn,
        ):
            find.return_value = _test_rows(25)
            exit_code = await run(dry_run=False, cap=20)

        assert exit_code == 2
        purge_fn.assert_not_awaited()

    async def test_unexpected_error_returns_error_exit(self):
        session, factory = _patched_session()
        with (
            patch(f"{RUN_MODULE}.AsyncSessionLocal", factory),
            patch(f"{RUN_MODULE}.find_purge_candidates", new_callable=AsyncMock) as find,
        ):
            find.side_effect = RuntimeError("db unreachable")
            exit_code = await run(dry_run=True)

        assert exit_code == 2
