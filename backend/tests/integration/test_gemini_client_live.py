"""
Live integration tests for the Gemini-backed CortexIntelligenceClient.

Exercises the REAL Gemini API (no mocks) — text generation, schema-constrained
structured output with token accounting, embeddings, and health check.  Skipped
only when GEMINI_API_KEY is absent (e.g. offline CI); when a key is present these
run against the live model configured in settings (gemini-2.5-flash).

Quality standard: Billion-Dollar App — real provider, real responses, structural
assertions that tolerate the model's natural output variability.
"""
from __future__ import annotations

from typing import Literal

import pytest
import pytest_asyncio
from pydantic import BaseModel, Field

from app.ai.intelligence.llm_client import CortexIntelligenceClient, get_intelligence_client
from app.core.config import get_settings

# Run the whole module on one event loop so the live Gemini client's async
# transport is created and closed on the same loop.
#
# filterwarnings: the google-genai Client's __del__ schedules a redundant
# transport-aclose task at garbage-collection, which lands after the module loop
# has closed and surfaces as a PytestUnraisableExceptionWarning.  We close the
# transport explicitly in the fixture (and in the app lifespan), so this is a
# known third-party-SDK GC artifact, not a leak — filtered to keep the suite clean.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning"),
    pytest.mark.skipif(
        not get_settings().GEMINI_API_KEY,
        reason="GEMINI_API_KEY not set — live Gemini integration tests skipped",
    ),
]


class _Sentiment(BaseModel):
    """Minimal schema to exercise native structured output."""
    label: Literal["positive", "negative", "neutral"] = Field(
        description="Overall financial sentiment of the text"
    )
    confidence: float = Field(ge=0.0, le=1.0)


@pytest_asyncio.fixture(loop_scope="module", scope="module", autouse=True)
async def _gemini_client():
    """Initialize the Gemini client once for the module and close it at the end,
    on the module-scoped loop, so its async transport is cleanly released."""
    import app.ai.intelligence.llm_client as L
    await CortexIntelligenceClient.initialize()
    yield
    client, L._client = L._client, None
    if client is not None:
        await client.aclose()


async def test_generate_returns_text_and_usage():
    client = get_intelligence_client()
    out = await client.generate(
        prompt="Reply with exactly the word: pong",
        temperature=0.0,
        max_tokens=16,
    )
    assert out["provider"] == "gemini"
    assert out["content"].strip()                 # non-empty
    assert out["prompt_tokens"] > 0
    assert out["latency_ms"] >= 0


async def test_structured_output_validates_schema():
    client = get_intelligence_client()
    result = await client.generate_structured(
        prompt="Classify the sentiment: 'The company beat earnings and raised guidance.'",
        response_model=_Sentiment,
        max_tokens=128,
    )
    assert isinstance(result, _Sentiment)
    assert result.label == "positive"            # unambiguous case
    assert 0.0 <= result.confidence <= 1.0


async def test_structured_with_usage_reports_tokens():
    client = get_intelligence_client()
    result, usage = await client.generate_structured_with_usage(
        prompt="Classify the sentiment: 'Shares fell sharply after the profit warning.'",
        response_model=_Sentiment,
        max_tokens=128,
    )
    assert result.label == "negative"
    assert usage["provider"] == "gemini"
    assert usage["input_tokens"] and usage["input_tokens"] > 0
    assert usage["output_tokens"] and usage["output_tokens"] > 0


async def test_embed_dimension_and_normalization():
    client = get_intelligence_client()
    settings = get_settings()
    vectors = await client.embed(
        ["Reliance reports record quarterly profit on refining margins."],
        input_type="passage",
    )
    assert len(vectors) == 1
    vec = vectors[0]
    assert len(vec) == settings.GEMINI_EMBED_DIM
    # Sub-3072 dims are L2-normalized by the client.
    norm = sum(x * x for x in vec) ** 0.5
    assert abs(norm - 1.0) < 1e-3


async def test_query_and_passage_embeddings_are_comparable():
    client = get_intelligence_client()
    passage = (await client.embed(
        ["Tata Motors quarterly sales rose on strong SUV demand."], input_type="passage"
    ))[0]
    q = (await client.embed(["Tata Motors sales growth"], input_type="query"))[0]
    # Cosine similarity of related query/passage should be clearly positive.
    cos = sum(a * b for a, b in zip(passage, q))
    assert cos > 0.3


async def test_health_check_reports_healthy():
    client = get_intelligence_client()
    status = await client.health_check()
    assert status["provider"] == "gemini"
    assert status["gemini"] == "healthy"
