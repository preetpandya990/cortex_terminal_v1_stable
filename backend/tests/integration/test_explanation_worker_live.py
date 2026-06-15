"""
Real-data integration test for the Gemini explanation worker.

Runs the full explanation pipeline against a real active TradeSuggestion in the
live database — real RAG retrieval, real Gemini structured generation, real
guardrails, real persistence, real audit row.  No mocks, no synthetic data.

Skips (rather than fails) when there is no suitable active suggestion, the
GEMINI_API_KEY is unset, or Gemini returns only transient capacity errors (503),
since those are environmental, not code, conditions.

Isolation: the conftest ``db_session`` runs in a transaction that is rolled back,
so the suggestion's explanation columns are not permanently modified.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.ai.intelligence import explanation_worker as W
from app.ai.intelligence.llm_client import CortexIntelligenceClient, LLMFallbackExhausted
from app.core.config import get_settings
from app.core.redis import close_redis, init_redis

pytestmark = [
    pytest.mark.integration,
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
    pytest.mark.skipif(
        not get_settings().GEMINI_API_KEY,
        reason="GEMINI_API_KEY not set — live explanation worker test skipped",
    ),
]

_FIVE_SECTIONS = (
    "### What the models saw",
    "### Technical picture",
    "### News context",
    "### What this suggests",
    "### Key risks",
)


@pytest.fixture
async def services():
    await init_redis()
    await CortexIntelligenceClient.initialize()
    yield
    import app.ai.intelligence.llm_client as L
    client, L._client = L._client, None
    if client is not None:
        await client.aclose()
    await close_redis()


async def test_explanation_generated_for_real_suggestion(db_session, services):
    row = (await db_session.execute(text(
        """SELECT suggestion_id, id FROM trade_suggestions
           WHERE status='active' AND ml_signal IS NOT NULL
           ORDER BY created_at DESC LIMIT 1"""
    ))).first()
    if not row:
        pytest.skip("No active suggestion with ml_signal in live data")
    sid, did = str(row[0]), row[1]

    # 503 high-demand is transient capacity — retry the whole pipeline a few times.
    for attempt in range(4):
        try:
            await W._generate_explanation(sid, did, db_session)
            break
        except (RuntimeError, LLMFallbackExhausted) as exc:
            if "503" in str(exc) or "UNAVAILABLE" in str(exc):
                await asyncio.sleep(4)
                continue
            raise
    else:
        pytest.skip("Gemini returned only transient 503 capacity errors")

    persisted = (await db_session.execute(text(
        """SELECT llm_summary, llm_explanation, explanation_model
           FROM trade_suggestions WHERE suggestion_id = :s"""
    ), {"s": sid})).first()
    summary, full, model = persisted

    # Summary present and plain-text (no markdown headers).
    assert summary and summary.strip()
    assert "###" not in summary
    # Full explanation has all five sections, in order, plus the regulatory disclaimer.
    assert full and all(h in full for h in _FIVE_SECTIONS)
    assert full.index("### What the models saw") < full.index("### Key risks")
    assert "does not constitute financial advice" in full   # injected disclaimer
    assert model.startswith("gemini/")

    # Governance: exactly the audit trail must record this inference.
    audit = (await db_session.execute(text(
        """SELECT invocation_type, model_provider, output_tokens
           FROM ai_llm_audit_log WHERE reference_id = :i AND invocation_type='explanation'
           ORDER BY created_at DESC LIMIT 1"""
    ), {"i": did})).first()
    assert audit is not None
    assert audit[1] == "gemini"
    assert audit[2] and audit[2] > 0
