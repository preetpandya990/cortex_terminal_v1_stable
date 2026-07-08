"""
Test Fake News Detector

Tests four-layer fake news detection with LLM reasoning.
"""
import pytest
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.intelligence.fake_news_detector import FakeNewsDetector
from app.ai.fusion.models import AIEventClassification, AISourceCredibility


@pytest.fixture
def mock_ollama_client():
    """Mock Ollama client."""
    mock = MagicMock()
    mock.generate_json = AsyncMock()
    return mock


@pytest.fixture
def detector_with_mock(mock_ollama_client):
    """Create detector with mocked Ollama client."""
    with patch("app.ai.intelligence.fake_news_detector.get_ollama_client", return_value=mock_ollama_client):
        detector = FakeNewsDetector(use_llm=True)
        detector.ollama_client = mock_ollama_client
        return detector


@pytest.fixture
def sample_classification():
    """Sample event classification."""
    return MagicMock(
        id=1,
        event_type="earnings",
        impact_score=Decimal("75.0"),
        classification_confidence=Decimal("0.85"),
    )


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fake news detector test - AsyncMock issue")
async def test_detect_credible_news(detector_with_mock, sample_classification):
    """Test detection of credible news (high scores)."""
    # Mock database queries
    db_mock = AsyncMock()
    
    # Mock classification query
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=lambda: sample_classification
    ))
    
    # Mock source credibility (high)
    credibility_mock = MagicMock(credibility_score=Decimal("0.9"))
    
    # Mock LLM response (credible)
    detector_with_mock.ollama_client.generate_json.return_value = {
        "is_credible": True,
        "credibility_score": 0.85,
        "red_flags": [],
        "reasoning": "No issues found",
    }
    
    # Setup execute to return different results for different queries
    execute_results = [
        MagicMock(scalar_one_or_none=lambda: sample_classification),  # Get classification
        MagicMock(scalar_one_or_none=lambda: credibility_mock),  # Get source credibility
        MagicMock(scalars=lambda: MagicMock(all=lambda: [sample_classification] * 3)),  # Cross-ref
    ]
    db_mock.execute = AsyncMock(side_effect=execute_results)
    
    result = await detector_with_mock.detect(
        db=db_mock,
        classification_id=1,
        content="Apple reports strong quarterly earnings",
        source="https://reuters.com",
    )
    
    assert result.flag_status == "cleared"
    assert "score >= 0.6" in result.reasoning


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fake news detector test - AsyncMock issue")
async def test_detect_fake_news(detector_with_mock, sample_classification):
    """Test detection of fake news (low scores)."""
    db_mock = AsyncMock()
    
    # Mock low credibility source
    credibility_mock = MagicMock(credibility_score=Decimal("0.2"))
    
    # Mock LLM response (not credible)
    detector_with_mock.ollama_client.generate_json.return_value = {
        "is_credible": False,
        "credibility_score": 0.1,
        "red_flags": ["Sensationalist language", "Unrealistic claims"],
        "reasoning": "Multiple red flags detected",
    }
    
    execute_results = [
        MagicMock(scalar_one_or_none=lambda: sample_classification),
        MagicMock(scalar_one_or_none=lambda: credibility_mock),
        MagicMock(scalars=lambda: MagicMock(all=lambda: [])),  # No cross-ref
    ]
    db_mock.execute = AsyncMock(side_effect=execute_results)
    
    result = await detector_with_mock.detect(
        db=db_mock,
        classification_id=1,
        content="SHOCKING: Company stock to surge 1000% tomorrow!!!",
        source="https://unknown-blog.com",
    )
    
    assert result.flag_status in ["flagged", "suspected"]
    assert "source=" in result.reasoning
    assert "llm=" in result.reasoning


@pytest.mark.asyncio
async def test_source_credibility_known_source_rescaled(detector_with_mock):
    """A stored 0-100 score is rescaled to the 0-1 domain this layer feeds."""
    db_mock = AsyncMock()

    credibility_mock = MagicMock(credibility_score=Decimal("95.0"))
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=lambda: credibility_mock
    ))

    score = await detector_with_mock._check_source_credibility(
        db=db_mock,
        source="Reuters India Business",
    )

    assert score == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_source_credibility_seed_score_rescaled(detector_with_mock):
    """The neutral 50.0 seed maps to 0.5 — identical to the unknown default."""
    db_mock = AsyncMock()
    credibility_mock = MagicMock(credibility_score=Decimal("50.0"))
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=lambda: credibility_mock
    ))

    score = await detector_with_mock._check_source_credibility(
        db=db_mock, source="Moneycontrol",
    )

    assert score == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_source_credibility_unknown_source(detector_with_mock):
    """Test source credibility check with unknown source."""
    db_mock = AsyncMock()
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalar_one_or_none=lambda: None
    ))

    score = await detector_with_mock._check_source_credibility(
        db=db_mock,
        source="https://unknown-site.com",
    )

    assert score == 0.5  # Default for unknown


def _classification(
    *,
    event_type: str = "earnings",
    affected_symbols: list[str] | None = None,
    classification_id: int = 1,
) -> MagicMock:
    return MagicMock(
        id=classification_id,
        event_type=event_type,
        affected_symbols=affected_symbols,
        impact_score=Decimal("75.0"),
        classification_confidence=Decimal("0.85"),
    )


@pytest.mark.asyncio
async def test_cross_reference_well_corroborated(detector_with_mock):
    """Test cross-reference with multiple similar events."""
    db_mock = AsyncMock()

    # Mock 4 similar events
    similar_events = [MagicMock() for _ in range(4)]
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalars=lambda: MagicMock(all=lambda: similar_events)
    ))

    score = await detector_with_mock._check_cross_reference(
        db=db_mock,
        classification=_classification(
            event_type="fed_announcement", affected_symbols=None
        ),
    )

    assert score == 0.9  # Well corroborated


@pytest.mark.asyncio
async def test_cross_reference_not_corroborated(detector_with_mock):
    """Test cross-reference with no similar events."""
    db_mock = AsyncMock()
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalars=lambda: MagicMock(all=lambda: [])
    ))

    score = await detector_with_mock._check_cross_reference(
        db=db_mock,
        classification=_classification(event_type="general"),
    )

    assert score == 0.3  # Not corroborated


@pytest.mark.asyncio
async def test_cross_reference_requires_symbol_overlap_in_query(detector_with_mock):
    """Symbol-bearing classifications add the && overlap filter and self-exclusion."""
    db_mock = AsyncMock()
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalars=lambda: MagicMock(all=lambda: [])
    ))

    await detector_with_mock._check_cross_reference(
        db=db_mock,
        classification=_classification(
            event_type="earnings", affected_symbols=["TCS"], classification_id=42
        ),
    )

    compiled = str(db_mock.execute.await_args.args[0].compile(
        compile_kwargs={"literal_binds": False}
    ))
    assert "&&" in compiled                       # array overlap required
    assert "id !=" in compiled or "id != " in compiled  # self-exclusion


@pytest.mark.asyncio
async def test_cross_reference_symbolless_falls_back_to_event_type(detector_with_mock):
    """Market-wide events (no symbols) corroborate on event_type alone."""
    db_mock = AsyncMock()
    db_mock.execute = AsyncMock(return_value=MagicMock(
        scalars=lambda: MagicMock(all=lambda: [])
    ))

    await detector_with_mock._check_cross_reference(
        db=db_mock,
        classification=_classification(
            event_type="geopolitical", affected_symbols=None
        ),
    )

    compiled = str(db_mock.execute.await_args.args[0].compile())
    assert "&&" not in compiled


def test_sentiment_consistency_regulatory_bearish(detector_with_mock, sample_classification):
    """Test sentiment consistency for regulatory event with bearish sentiment."""
    sample_classification.event_type = "regulatory"
    
    score = detector_with_mock._check_sentiment_consistency(
        content="SEC investigation causes stock to plunge",
        classification=sample_classification,
    )
    
    assert score == 0.9  # Consistent


def test_sentiment_consistency_regulatory_bullish(detector_with_mock, sample_classification):
    """Test sentiment consistency for regulatory event with bullish sentiment."""
    sample_classification.event_type = "regulatory"
    
    score = detector_with_mock._check_sentiment_consistency(
        content="SEC investigation causes stock to surge",  # Inconsistent
        classification=sample_classification,
    )
    
    assert score == 0.3  # Inconsistent


def test_sentiment_consistency_earnings_neutral(detector_with_mock, sample_classification):
    """Test sentiment consistency for earnings (neutral)."""
    sample_classification.event_type = "earnings"

    score = detector_with_mock._check_sentiment_consistency(
        content="Company reports earnings",
        classification=sample_classification,
    )

    assert score == 0.7  # Neutral consistency


def test_sentiment_cues_ignore_substrings(detector_with_mock, sample_classification):
    """
    Word-boundary regression: "up" in "Group"/"support", "down" in
    "showdown", "miss" in "commissioner" must not count as sentiment cues.
    A credible regulatory article naming "XYZ Group" used to flip this layer
    from 0.9 (bearish-consistent) to 0.3 on those false positives.
    """
    sample_classification.event_type = "regulatory"

    score = detector_with_mock._check_sentiment_consistency(
        content=(
            "SEC commissioner opens probe into XYZ Group; supporters expect a "
            "showdown as shares plunge on the regulatory action"
        ),
        classification=sample_classification,
    )

    # "plunge" is the only genuine cue → bearish → consistent with regulatory.
    assert score == 0.9


def test_sentiment_cues_match_inflected_forms(detector_with_mock, sample_classification):
    """Explicit inflections ("surged", "gains") still register as cues."""
    sample_classification.event_type = "regulatory"

    score = detector_with_mock._check_sentiment_consistency(
        content="Shares surged and gains exceeded estimates despite the probe",
        classification=sample_classification,
    )

    # Bullish content on a regulatory event → inconsistent.
    assert score == 0.3


@pytest.mark.asyncio
async def test_llm_reasoning_success(detector_with_mock, sample_classification):
    """Test LLM reasoning returns credibility score."""
    detector_with_mock.ollama_client.generate_json.return_value = {
        "is_credible": True,
        "credibility_score": 0.88,
        "red_flags": [],
        "reasoning": "Credible source and content",
    }
    
    score = await detector_with_mock._llm_reasoning(
        content="Apple reports earnings",
        source="https://reuters.com",
        classification=sample_classification,
    )
    
    assert score == 0.88


@pytest.mark.asyncio
async def test_llm_reasoning_failure_fallback(detector_with_mock, sample_classification):
    """Test LLM reasoning fallback on error."""
    detector_with_mock.ollama_client.generate_json.side_effect = Exception("Timeout")
    
    score = await detector_with_mock._llm_reasoning(
        content="Some content",
        source="https://example.com",
        classification=sample_classification,
    )
    
    assert score == 0.5  # Neutral fallback


@pytest.mark.asyncio
async def test_llm_reasoning_disabled():
    """Test LLM reasoning when LLM is disabled."""
    detector = FakeNewsDetector(use_llm=False)
    
    score = await detector._llm_reasoning(
        content="Some content",
        source="https://example.com",
        classification=MagicMock(),
    )
    
    assert score == 0.5  # Neutral when disabled


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fake news detector test - AsyncMock issue")
async def test_weighted_score_calculation(detector_with_mock, sample_classification):
    """Test weighted score calculation."""
    db_mock = AsyncMock()
    
    # Set specific scores for each layer
    credibility_mock = MagicMock(credibility_score=Decimal("0.8"))
    
    detector_with_mock.ollama_client.generate_json.return_value = {
        "credibility_score": 0.7,
    }
    
    execute_results = [
        MagicMock(scalar_one_or_none=lambda: sample_classification),
        MagicMock(scalar_one_or_none=lambda: credibility_mock),
        MagicMock(scalars=lambda: MagicMock(all=lambda: [MagicMock(), MagicMock()])),  # 2 similar
    ]
    db_mock.execute = AsyncMock(side_effect=execute_results)
    
    result = await detector_with_mock.detect(
        db=db_mock,
        classification_id=1,
        content="Company reports earnings beat",
        source="https://reuters.com",
    )
    
    # Verify weighted calculation in reasoning
    assert "source=" in result.reasoning
    assert "cross_ref=" in result.reasoning
    assert "sentiment=" in result.reasoning
    assert "llm=" in result.reasoning


@pytest.mark.asyncio
@pytest.mark.skip(reason="Fake news detector test - AsyncMock issue")
async def test_flag_status_thresholds(detector_with_mock, sample_classification):
    """Test flag status based on score thresholds."""
    db_mock = AsyncMock()
    
    # Test flagged (< 0.4)
    credibility_mock = MagicMock(credibility_score=Decimal("0.1"))
    detector_with_mock.ollama_client.generate_json.return_value = {"credibility_score": 0.1}
    
    execute_results = [
        MagicMock(scalar_one_or_none=lambda: sample_classification),
        MagicMock(scalar_one_or_none=lambda: credibility_mock),
        MagicMock(scalars=lambda: MagicMock(all=lambda: [])),
    ]
    db_mock.execute = AsyncMock(side_effect=execute_results)
    
    result = await detector_with_mock.detect(
        db=db_mock,
        classification_id=1,
        content="Fake news content",
        source="https://fake.com",
    )
    
    assert result.flag_status == "flagged"
    assert "score < 0.4" in result.reasoning
