"""
Tests for app.workers.registry
================================
Validates that build_task_registry() produces exactly 13 callable factories
matching TASK_NAMES, and that the key migrated tasks are present.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.workers.registry import TASK_NAMES, build_task_registry
from app.workers.supervisor import TaskState, create_task_states


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
    def test_has_13_tasks(self):
        assert len(TASK_NAMES) == 13

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
                     "fundamentals_refresh"):
            assert name in TASK_NAMES


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
        # Patch all imported loop modules to avoid pulling in real dependencies
        with (
            patch("app.ai.ingestion.rss_fetcher.rss_ingestion_loop", return_value=None),
            patch("app.ai.intelligence.event_processor.event_processing_loop", return_value=None),
            patch("app.ai.safety.safety_trigger_engine.safety_monitoring_loop", return_value=None),
            patch("app.ai.strategy.regime_detector.regime_detection_loop", return_value=None),
            patch("app.ml.monitoring.drift_scheduler.drift_detection_loop", return_value=None),
            patch("app.services.data_ingestion_worker.data_ingestion_loop", return_value=None),
            patch("app.services.fundamentals_refresh.FundamentalsRefreshScheduler"),
            patch("app.services.paper_trading.pnl_worker.run_pnl_worker", return_value=None),
            patch("app.services.strategy_engine.sl_tp_worker.run_sl_tp_worker", return_value=None),
            patch("app.worker.heartbeat_loop", return_value=None),
            patch("app.worker.cache_invalidation_loop", return_value=None),
            patch("app.worker.expiry_loop", return_value=None),
            patch("app.worker.correlation_loop", return_value=None),
        ):
            return build_task_registry(
                session_factory=session_factory,
                redis_client=redis_client,
                ml_components=ml_components,
                upstox_client=upstox_client,
                shutdown=shutdown_event,
                task_states=task_states,
            )

    def test_builds_all_13_tasks(
        self, mock_session_factory, mock_redis_client, mock_ml_components,
        mock_upstox_client, shutdown_event, task_states,
    ):
        registry = self._build(
            mock_session_factory, mock_redis_client, mock_ml_components,
            mock_upstox_client, shutdown_event, task_states,
        )
        assert len(registry) == 13
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
