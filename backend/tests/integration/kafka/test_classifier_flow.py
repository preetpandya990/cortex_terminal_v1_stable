"""
Deferred-classification queue flow against the real broker, using a REAL
nlp_result_id + content from the dev DB's ai_event_classifications rows.

The Gemini boundary (_gemini_classify) is faked per the owner directive.
The persist boundary is ALSO faked here — a synthetic Gemini result must
never overwrite a real dev-DB classification row; the real upsert path
(UNIQUE(nlp_result_id) in-place upgrade) is covered by the unit tests and
migration 0049.

Covers: commit-only-after-successful-persist, failure → no commit →
redelivery on the next dispatch, and malformed-payload skip.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.ai.intelligence.event_classifier import EventClassifier
from app.core.config import get_settings
from app.core.kafka import KafkaGroups, KafkaTopics, pending_count, publish
from tests.integration.kafka.conftest import fast_forward

pytestmark = pytest.mark.integration

_TOPIC = KafkaTopics.CLASSIFIER_PENDING
_GROUP = KafkaGroups.CLASSIFIER_FLUSH

_GEMINI_OK = {
    "event_type": "regulatory",
    "impact_score": 80.0,
    "confidence": 0.9,
    "affected_symbols": [],
    "reasoning": "integration test",
    "decay_hours": 24,
    "decay_slow_hours": 168,
}

_GEMINI_FAIL = dict(_GEMINI_OK, confidence=0.0)  # failure sentinel


def _db_factory_mock():
    db = AsyncMock()
    db.execute.return_value = MagicMock(scalar_one_or_none=MagicMock(return_value=None))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx), db


@pytest.fixture
async def real_pending_payload():
    """A deferred-classification payload built from a REAL dev-DB row."""
    engine = create_async_engine(
        str(get_settings().DATABASE_URL), poolclass=NullPool, echo=False
    )
    try:
        async with engine.connect() as conn:
            row = (await conn.execute(text(
                "SELECT c.nlp_result_id, p.translated_content "
                "FROM ai_event_classifications c "
                "JOIN ai_nlp_results n ON n.id = c.nlp_result_id "
                "JOIN ai_processed_events p ON p.id = n.processed_event_id "
                "WHERE c.nlp_result_id IS NOT NULL LIMIT 1"
            ))).first()
    finally:
        await engine.dispose()
    if row is None:
        pytest.skip("dev DB has no classified events to build a payload from")
    return {
        "content_hash": "integration",
        "content": (row.translated_content or "integration test content")[:1500],
        "entities": {"companies": []},
        "nlp_result_id": row.nlp_result_id,
        "enqueued_at": "2026-07-01T00:00:00+00:00",
    }


@pytest.fixture
async def clean_group(kafka):
    await fast_forward(_TOPIC, _GROUP)
    yield


@pytest.mark.asyncio
async def test_commit_only_after_successful_persist(
    redis, clean_group, real_pending_payload
):
    classifier = EventClassifier(use_llm=False)
    await publish(_TOPIC, real_pending_payload,
                  key=str(real_pending_payload["nlp_result_id"]))
    db_factory, db = _db_factory_mock()

    with patch.object(classifier, "_gemini_classify", AsyncMock(return_value=_GEMINI_OK)):
        with patch.object(classifier, "_extract_symbols_from_content", AsyncMock(return_value=[])):
            with patch(
                "app.ai.intelligence.event_classifier.normalize_and_validate_symbols",
                AsyncMock(return_value=[]),
            ):
                with patch.object(
                    classifier, "_persist_classification", AsyncMock()
                ) as persist:
                    result = await classifier.flush_pending_classifications(db_factory)

    assert result == {"dispatched": 1, "calls_made": 1}
    persist.assert_awaited_once()
    assert persist.await_args.args[1] == real_pending_payload["nlp_result_id"]
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_gemini_failure_leaves_item_pending_then_redelivers(
    redis, clean_group, real_pending_payload
):
    classifier = EventClassifier(use_llm=False)
    await publish(_TOPIC, real_pending_payload,
                  key=str(real_pending_payload["nlp_result_id"]))
    db_factory, _ = _db_factory_mock()

    with patch.object(classifier, "_gemini_classify", AsyncMock(return_value=_GEMINI_FAIL)):
        with pytest.raises(RuntimeError, match="stopped early"):
            await classifier.flush_pending_classifications(db_factory)

    # NOT committed — the old LPOP path would have lost this item.
    assert await pending_count(_TOPIC, _GROUP) == 1

    # The next dispatch (Gemini recovered) redelivers and drains it.
    with patch.object(classifier, "_gemini_classify", AsyncMock(return_value=_GEMINI_OK)):
        with patch.object(classifier, "_extract_symbols_from_content", AsyncMock(return_value=[])):
            with patch(
                "app.ai.intelligence.event_classifier.normalize_and_validate_symbols",
                AsyncMock(return_value=[]),
            ):
                with patch.object(classifier, "_persist_classification", AsyncMock()):
                    result = await classifier.flush_pending_classifications(db_factory)

    assert result == {"dispatched": 1, "calls_made": 1}
    assert await pending_count(_TOPIC, _GROUP) == 0


@pytest.mark.asyncio
async def test_malformed_payload_committed_past(redis, clean_group):
    classifier = EventClassifier(use_llm=False)
    await publish(_TOPIC, {"garbage": True}, key="malformed")
    db_factory, _ = _db_factory_mock()

    with patch.object(classifier, "_gemini_classify", AsyncMock()) as fake_gemini:
        result = await classifier.flush_pending_classifications(db_factory)

    fake_gemini.assert_not_awaited()
    assert result == {"dispatched": 0, "calls_made": 0}
    assert await pending_count(_TOPIC, _GROUP) == 0
