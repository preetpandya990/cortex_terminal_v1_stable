"""
Tests for app.workers.registry
================================
Validates that build_task_registry() produces exactly 18 callable factories
matching TASK_NAMES, and that the key migrated tasks are present.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.registry import TASK_EXPECTED_INTERVAL_SECONDS, TASK_NAMES, build_task_registry
from app.workers.supervisor import TaskState, create_task_states

# Every loop/class currently imported by build_task_registry() — kept in one
# place so both _build() and the on_cycle wiring-proof tests patch the exact
# same set without duplicating the list.
_LOOP_PATCH_TARGETS = (
    "app.ai.ingestion.rss_fetcher.rss_ingestion_loop",
    "app.ai.intelligence.event_processor.event_processing_loop",
    "app.ai.safety.safety_trigger_engine.safety_monitoring_loop",
    "app.ai.strategy.regime_detector.regime_detection_loop",
    "app.ml.monitoring.drift_scheduler.drift_detection_loop",
    "app.services.data_ingestion_worker.data_ingestion_loop",
    "app.services.fundamentals_refresh.FundamentalsRefreshScheduler",
    "app.services.paper_trading.pnl_worker.run_pnl_worker",
    "app.services.strategy_engine.sl_tp_worker.run_sl_tp_worker",
    "app.worker.heartbeat_loop",
    "app.worker.cache_invalidation_loop",
    "app.worker.expiry_loop",
    "app.worker.correlation_loop",
    "app.worker.feature_refresh_loop",
    "app.workers.rag_cleanup.rag_cleanup_loop",
    "app.workers.watchlist_context_scheduler.WatchlistContextScheduler",
    "app.ai.fusion.forecast_batch_worker.forecast_batch_loop",
    "app.workers.ai_processing_safety_net.AIProcessingSafetyNet",
)


def _patch_all_loops(stack: ExitStack) -> dict:
    """Patch every loop/class in _LOOP_PATCH_TARGETS; return name -> Mock."""
    mocks = {}
    for target in _LOOP_PATCH_TARGETS:
        kwargs = {} if target.endswith(("Scheduler", "SafetyNet")) else {"return_value": None}
        mocks[target] = stack.enter_context(patch(target, **kwargs))
    return mocks


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_session_factory():
    return MagicMock()


@pytest.fixture
def mock_redis_client():
    client = MagicMock()
    client._redis = MagicMock()
    return client


@pytest.fixture
def mock_ml_components():
    return {}


@pytest.fixture
def mock_upstox_client():
    return MagicMock()


@pytest.fixture
def shutdown_event():
    return asyncio.Event()


@pytest.fixture
def task_states():
    return create_task_states(list(TASK_NAMES))


# ── TASK_NAMES ─────────────────────────────────────────────────────────────────

class TestTaskNames:
    def test_has_18_tasks(self):
        assert len(TASK_NAMES) == 18

    def test_is_tuple(self):
        assert isinstance(TASK_NAMES, tuple)

    def test_no_duplicates(self):
        assert len(set(TASK_NAMES)) == len(TASK_NAMES)

    def test_contains_migrated_tasks(self):
        assert "pnl_worker" in TASK_NAMES
        assert "sl_tp_worker" in TASK_NAMES

    def test_contains_native_loops(self):
        for name in ("heartbeat", "cache_invalidation", "suggestion_expiry", "correlation_engine"):
            assert name in TASK_NAMES

    def test_contains_imported_loops(self):
        for name in ("rss_ingestion", "event_processing", "regime_detection",
                     "drift_detection", "safety_monitoring", "data_ingestion",
                     "fundamentals_refresh", "feature_refresh", "rag_cleanup",
                     "watchlist_scheduler", "forecast_batch", "ai_processing_safety_net"):
            assert name in TASK_NAMES


# ── TASK_EXPECTED_INTERVAL_SECONDS ──────────────────────────────────────────────

class TestTaskExpectedIntervalSeconds:
    def test_keys_match_task_names_exactly(self):
        assert set(TASK_EXPECTED_INTERVAL_SECONDS.keys()) == set(TASK_NAMES)

    def test_event_driven_tasks_map_to_none(self):
        for name in ("cache_invalidation", "pnl_worker", "sl_tp_worker"):
            assert TASK_EXPECTED_INTERVAL_SECONDS[name] is None

    def test_periodic_tasks_have_positive_intervals(self):
        for name, interval in TASK_EXPECTED_INTERVAL_SECONDS.items():
            if name in ("cache_invalidation", "pnl_worker", "sl_tp_worker"):
                continue
            assert isinstance(interval, int) and interval > 0, name


# ── build_task_registry() ──────────────────────────────────────────────────────

class TestBuildTaskRegistry:
    def _build(
        self,
        session_factory,
        redis_client,
        ml_components,
        upstox_client,
        shutdown_event,
        task_states,
    ) -> dict:
        with ExitStack() as stack:
            _patch_all_loops(stack)
            return build_task_registry(
                session_factory=session_factory,
                redis_client=redis_client,
                ml_components=ml_components,
                upstox_client=upstox_client,
                shutdown=shutdown_event,
                task_states=task_states,
            )

    def test_builds_all_18_tasks(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        assert len(registry) == 18
        assert set(registry.keys()) == set(TASK_NAMES)

    def test_factories_are_callable(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        for name, factory in registry.items():
            assert callable(factory), f"Factory for '{name}' is not callable"

    def test_pnl_worker_in_registry(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        assert "pnl_worker" in registry

    def test_sl_tp_worker_in_registry(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        assert "sl_tp_worker" in registry

    def test_registry_task_names_integrity(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        """Registry keys must match TASK_NAMES exactly — no extras or missing."""
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        assert set(registry.keys()) == set(TASK_NAMES)
        assert len(registry) == len(TASK_NAMES)


# ── on_cycle wiring proof ────────────────────────────────────────────────────────
# Only 2 representative tests (not 18) — one per wiring pattern (see plan
# pure-brewing-backus.md §9): proves the two mechanics work without
# re-verifying all 18 call sites individually, matching the existing coverage
# boundary (nothing tests loop bodies directly, only the wiring).

class TestOnCycleWiring:
    @pytest.mark.asyncio
    async def test_pattern_a_heartbeat_receives_bound_record_cycle(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        """Pattern A: func kwarg alongside pause/trigger (app.worker.* loops)."""
        with ExitStack() as stack:
            mocks = _patch_all_loops(stack)
            registry = build_task_registry(
                session_factory=mock_session_factory,
                redis_client=mock_redis_client,
                ml_components=mock_ml_components,
                upstox_client=mock_upstox_client,
                shutdown=shutdown_event,
                task_states=task_states,
            )
            # Loop factories are patched with AsyncMock (patch() auto-detects
            # the original is a coroutine function) — must await, not just
            # call, or the returned coroutine is left dangling.
            await registry["heartbeat"]()
            _, kwargs = mocks["app.worker.heartbeat_loop"].call_args

        assert kwargs["on_cycle"] == task_states["heartbeat"].record_cycle

    @pytest.mark.asyncio
    async def test_pattern_b_rss_ingestion_receives_bound_record_cycle(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        """Pattern B: keyword-only default-None param on plain imported functions."""
        with ExitStack() as stack:
            mocks = _patch_all_loops(stack)
            registry = build_task_registry(
                session_factory=mock_session_factory,
                redis_client=mock_redis_client,
                ml_components=mock_ml_components,
                upstox_client=mock_upstox_client,
                shutdown=shutdown_event,
                task_states=task_states,
            )
            await registry["rss_ingestion"]()
            _, kwargs = mocks["app.ai.ingestion.rss_fetcher.rss_ingestion_loop"].call_args

        assert kwargs["on_cycle"] == task_states["rss_ingestion"].record_cycle
