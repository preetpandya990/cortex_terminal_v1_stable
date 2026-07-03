"""
Tests for the demand-driven dispatch additions to NLPEngine.

Covers:
  - flush_pending_sentiment() drains the queue fully and reports correct
    dispatched/calls_made counts.
  - pending_sentiment_count() reflects live queue depth.
  - SENTIMENT_AUTO_FLUSH=False prevents the size-trigger from firing in
    _analyze_with_audit_meta(), while the item is still enqueued and its
    caller still resolves once an explicit dispatch drains the queue.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.intelligence.nlp_engine import (
    NLPEngine,
    SentimentOutput,
    _SentimentBatchItem,
    _SentimentBatchOutput,
)


@pytest.fixture(autouse=True)
async def _reset_engine_state():
    """Ensure NLPEngine's class-level queue/flusher never leaks across tests."""
    NLPEngine._initialized = False
    yield
    if NLPEngine._initialized:
        await NLPEngine.aclose()


@pytest.fixture
def mock_cache():
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=None)
    return cache


@pytest.fixture
def mock_llm_client():
    """3-item batch path uses _SentimentBatchOutput, not the single-item SentimentOutput."""
    client = MagicMock()
    client.model_id = "gemini/gemini-2.5-flash"
    client.generate_structured = AsyncMock(
        return_value=_SentimentBatchOutput(
            results=[
                _SentimentBatchItem(index=i, label="positive", score=0.5, confidence=0.8)
                for i in range(3)
            ]
        )
    )
    return client


@pytest.mark.asyncio
async def test_pending_sentiment_count_reflects_queue_depth(mock_cache):
    with patch("app.core.redis.get_cache_service", return_value=mock_cache):
        await NLPEngine.initialize()
        engine = NLPEngine()

        assert await NLPEngine.pending_sentiment_count() == 0

        # Enqueue without triggering the flusher — hold a settings override so
        # the size trigger never fires mid-test.
        mock_settings = MagicMock(SENTIMENT_AUTO_FLUSH=False, SENTIMENT_BATCH_SIZE=15, SENTIMENT_BATCH_WINDOW_SECS=60.0)
        with patch("app.core.config.get_settings", return_value=mock_settings):
            task = asyncio.ensure_future(engine.analyze_sentiment("Reliance beats Q4 estimates"))
            await asyncio.sleep(0.05)  # let the coroutine reach the enqueue + await point

            assert await NLPEngine.pending_sentiment_count() == 1

            # Drain explicitly so the pending future resolves and the test exits cleanly.
            with patch(
                "app.ai.intelligence.llm_client.get_intelligence_client",
                return_value=MagicMock(
                    model_id="gemini/gemini-2.5-flash",
                    generate_structured=AsyncMock(
                        return_value=SentimentOutput(label="neutral", score=0.0, confidence=0.5, reasoning="test")
                    ),
                ),
            ):
                await NLPEngine.flush_pending_sentiment()

            await task


@pytest.mark.asyncio
async def test_flush_pending_sentiment_drains_fully(mock_cache, mock_llm_client):
    with patch("app.core.redis.get_cache_service", return_value=mock_cache):
        await NLPEngine.initialize()
        engine = NLPEngine()

        mock_settings = MagicMock(SENTIMENT_AUTO_FLUSH=False, SENTIMENT_BATCH_SIZE=15, SENTIMENT_BATCH_WINDOW_SECS=60.0)
        with patch("app.core.config.get_settings", return_value=mock_settings):
            tasks = [
                asyncio.ensure_future(engine.analyze_sentiment(f"Article {i} about markets"))
                for i in range(3)
            ]
            await asyncio.sleep(0.05)
            assert await NLPEngine.pending_sentiment_count() == 3

            with patch(
                "app.ai.intelligence.llm_client.get_intelligence_client",
                return_value=mock_llm_client,
            ):
                result = await NLPEngine.flush_pending_sentiment()

            assert result["dispatched"] == 3
            assert result["calls_made"] >= 1
            assert await NLPEngine.pending_sentiment_count() == 0

            results = await asyncio.gather(*tasks)
            assert all(r["label"] in ("positive", "negative", "neutral") for r in results)


@pytest.mark.asyncio
async def test_auto_flush_false_does_not_set_trigger(mock_cache):
    """
    With SENTIMENT_AUTO_FLUSH=False, reaching the batch-size threshold must
    NOT set the flush trigger — only an explicit dispatch drains the queue.
    """
    with patch("app.core.redis.get_cache_service", return_value=mock_cache):
        await NLPEngine.initialize()
        engine = NLPEngine()

        mock_settings = MagicMock(SENTIMENT_AUTO_FLUSH=False, SENTIMENT_BATCH_SIZE=1, SENTIMENT_BATCH_WINDOW_SECS=60.0)
        with patch("app.core.config.get_settings", return_value=mock_settings):
            task = asyncio.ensure_future(engine.analyze_sentiment("Single article"))
            await asyncio.sleep(0.05)

            # Batch size is 1, so a flag-on flusher would have fired by now.
            assert not NLPEngine._flush_trigger.is_set()
            assert await NLPEngine.pending_sentiment_count() == 1

            with patch(
                "app.ai.intelligence.llm_client.get_intelligence_client",
                return_value=MagicMock(
                    model_id="gemini/gemini-2.5-flash",
                    generate_structured=AsyncMock(
                        return_value=SentimentOutput(label="neutral", score=0.0, confidence=0.5, reasoning="test")
                    ),
                ),
            ):
                await NLPEngine.flush_pending_sentiment()
            await task
