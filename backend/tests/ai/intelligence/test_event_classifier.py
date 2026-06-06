"""
Tests for EventClassifier — rule-based path and content-level symbol extraction.

LLM-dependent tests (Ollama / GPT-4o paths) are exercised via the classify()
integration path and are guarded by AsyncMock for unit testing.

Decay half-lives are taken from the canonical _DECAY_HALF_LIVES table
(updated in migration 0015).  Tests that previously asserted stale values
have been corrected to match:
  earnings:        fast_hl=12   (was 48)
  fed_announcement: fast_hl=8   (was 72)
  geopolitical:    fast_hl=8    (was 72)
  general:         impact=45.0  (was 50.0)
"""
import pytest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from app.ai.intelligence.event_classifier import (
    EventClassifier,
    _ALLCAPS_RE,
    _CORP_NAME_RE,
    _NON_TICKER_WORDS,
    _ClassificationSchema,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_llm_client():
    """Mock CortexIntelligenceClient for LLM-dependent tests."""
    mock = MagicMock()
    mock.generate_structured = AsyncMock()
    mock.generate_json = AsyncMock()
    return mock


@pytest.fixture
def classifier_with_mock(mock_llm_client):
    """Create classifier with mocked LLM client."""
    with patch(
        "app.ai.intelligence.event_classifier.get_ollama_client",
        return_value=mock_llm_client,
    ):
        classifier = EventClassifier(use_llm=True)
        classifier.ollama_client = mock_llm_client
        return classifier


# ── LLM path tests (generate_structured) ──────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_with_llm_success(classifier_with_mock):
    """LLM path: successful structured classification with symbols."""
    classifier_with_mock.ollama_client.generate_structured.return_value = (
        _ClassificationSchema(
            event_type="earnings",
            impact_score=75.0,
            confidence=0.85,
            affected_symbols=["TCS", "INFY"],
            reasoning="Tata Consultancy quarterly earnings beat estimates.",
            decay_hours=12,
            decay_slow_hours=72,
        )
    )

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()
    db_mock.refresh = AsyncMock()

    # Patch content extraction so no DB calls are needed
    with patch.object(
        classifier_with_mock,
        "_extract_symbols_from_content",
        return_value=[],
    ):
        with patch(
            "app.ai.intelligence.event_classifier.normalize_and_validate_symbols",
            return_value=["TCS", "INFY"],
        ):
            result = await classifier_with_mock.classify(
                db=db_mock,
                nlp_result_id=1,
                content="TCS and Infosys announce record Q4 earnings",
                entities={"companies": ["TCS", "Infosys"], "people": [], "locations": []},
            )

    classifier_with_mock.ollama_client.generate_structured.assert_called_once()
    assert result.event_type == "earnings"
    assert result.classification_confidence == Decimal("0.85")
    assert result.affected_symbols == ["TCS", "INFY"]


@pytest.mark.asyncio
async def test_classify_llm_low_confidence_preserves_symbols(classifier_with_mock):
    """
    Symbols from a low-confidence LLM result must survive the rule-based fallback.
    This is the core invariant for the symbol-preservation fix.
    """
    classifier_with_mock.ollama_client.generate_structured.return_value = (
        _ClassificationSchema(
            event_type="general",
            impact_score=40.0,
            confidence=0.3,          # Below 0.5 threshold → rule-based fallback
            affected_symbols=["VEDL", "HINDZINC"],
            reasoning="Uncertain about event type but companies identified.",
            decay_hours=12,
            decay_slow_hours=48,
        )
    )

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()
    db_mock.refresh = AsyncMock()

    with patch.object(
        classifier_with_mock,
        "_extract_symbols_from_content",
        return_value=[],
    ):
        with patch(
            "app.ai.intelligence.event_classifier.normalize_and_validate_symbols",
            return_value=["VEDL", "HINDZINC"],
        ):
            result = await classifier_with_mock.classify(
                db=db_mock,
                nlp_result_id=2,
                content="Vedanta Hindustan Zinc shares slip after ED visit",
                entities={"companies": [], "people": [], "locations": []},
            )

    # Classification falls back to rule-based (confidence 0.6) but symbols survive
    assert result.classification_confidence == Decimal("0.6")
    assert result.affected_symbols == ["VEDL", "HINDZINC"]


@pytest.mark.asyncio
async def test_classify_llm_failure_falls_back_to_rule_based(classifier_with_mock):
    """LLM failure propagates to rule-based classification."""
    classifier_with_mock.ollama_client.generate_structured.side_effect = Exception(
        "Ollama connection timeout"
    )

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()
    db_mock.refresh = AsyncMock()

    with patch.object(
        classifier_with_mock,
        "_extract_symbols_from_content",
        return_value=[],
    ):
        with patch(
            "app.ai.intelligence.event_classifier.normalize_and_validate_symbols",
            return_value=[],
        ):
            result = await classifier_with_mock.classify(
                db=db_mock,
                nlp_result_id=3,
                content="Federal Reserve announces interest rate hike at FOMC",
                entities={"companies": [], "people": [], "locations": []},
            )

    assert result.event_type == "fed_announcement"
    assert result.classification_confidence == Decimal("0.6")


# ── Rule-based path tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_rule_based_earnings():
    """Rule-based: earnings keywords → event_type=earnings, fast_hl=12."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="Company reports Q4 earnings beat expectations with EPS of $2.50",
        entities={"organizations": ["Company"]},
    )
    assert result["event_type"] == "earnings"
    assert result["impact_score"] == 70.0
    assert result["confidence"] == 0.6
    assert result["decay_hours"] == 12   # earnings fast_hl per _DECAY_HALF_LIVES


@pytest.mark.asyncio
async def test_classify_rule_based_fed_announcement():
    """Rule-based: Fed/RBI keywords → event_type=fed_announcement, fast_hl=8."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="Federal Reserve announces 0.25% interest rate hike at FOMC meeting",
        entities={},
    )
    assert result["event_type"] == "fed_announcement"
    assert result["impact_score"] == 85.0
    assert result["decay_hours"] == 8    # fed_announcement fast_hl per _DECAY_HALF_LIVES


@pytest.mark.asyncio
async def test_classify_rule_based_regulatory():
    """Rule-based: SEBI/SEC keywords → event_type=regulatory."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="SEC launches investigation into company accounting practices",
        entities={},
    )
    assert result["event_type"] == "regulatory"
    assert result["impact_score"] == 75.0


@pytest.mark.asyncio
async def test_classify_rule_based_geopolitical():
    """Rule-based: sanctions/war keywords → event_type=geopolitical, fast_hl=8."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="Trade war escalates as new sanctions announced",
        entities={},
    )
    assert result["event_type"] == "geopolitical"
    assert result["impact_score"] == 90.0
    assert result["decay_hours"] == 8    # geopolitical fast_hl per _DECAY_HALF_LIVES


@pytest.mark.asyncio
async def test_classify_rule_based_market_data():
    """Rule-based: GDP/CPI keywords → event_type=market_data."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="GDP growth exceeds expectations, unemployment falls to 3.5%",
        entities={},
    )
    assert result["event_type"] == "market_data"
    assert result["impact_score"] == 65.0


@pytest.mark.asyncio
async def test_classify_rule_based_general():
    """Rule-based: no keyword match → event_type=general, impact=45.0."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="Some random news that doesn't match any category",
        entities={},
    )
    assert result["event_type"] == "general"
    assert result["impact_score"] == 45.0   # general base score per _score_impact
    assert result["confidence"] == 0.6


@pytest.mark.asyncio
async def test_classify_rule_based_extracts_ner_symbols():
    """Rule-based uses NER company entities when available (capped at 3)."""
    classifier = EventClassifier(use_llm=False)
    result = classifier._classify_rule_based(
        content="Multiple companies affected",
        entities={"companies": ["RELIANCE", "TCS", "INFY", "HDFC"]},
    )
    assert len(result["affected_symbols"]) == 3
    assert result["affected_symbols"] == ["RELIANCE", "TCS", "INFY"]


# ── Content-level symbol extraction regex tests ────────────────────────────────

def _extract_candidates(content: str) -> list[str]:
    """Helper: run both regex passes and return deduplicated candidates."""
    seen: set[str] = set()
    out: list[str] = []

    for m in _ALLCAPS_RE.finditer(content):
        token = m.group(1).rstrip("-&")
        if len(token) >= 3 and token not in _NON_TICKER_WORDS:
            if token not in seen:
                seen.add(token)
                out.append(token)

    for m in _CORP_NAME_RE.finditer(content):
        token = m.group(1).strip()
        if token not in seen:
            seen.add(token)
            out.append(token)

    return out


def test_content_extraction_allcaps_tickers():
    """ALL-CAPS NSE tickers in content are extracted correctly."""
    candidates = _extract_candidates("INFY TCS HCLTECH decline as Nifty IT falls 5.6%")
    assert "INFY" in candidates
    assert "TCS" in candidates
    assert "HCLTECH" in candidates
    # "IT" should be blocked
    assert "IT" not in candidates


def test_content_extraction_corporate_suffix():
    """Company names with corporate suffixes are extracted via corp-name regex."""
    candidates = _extract_candidates(
        "SEBI Interim Order in the matter of Rajesh Exports Limited"
    )
    assert any("Rajesh Exports" in c for c in candidates)


def test_content_extraction_no_false_positives_from_hyphen():
    """'US-Iran' must NOT produce 'US-' or 'US' as a ticker candidate."""
    candidates = _extract_candidates(
        "US-Iran tensions may impact Indian refiners IOC BPCL HPCL"
    )
    assert "US" not in candidates
    assert "US-" not in candidates
    # Real tickers should still be found
    assert "IOC" in candidates
    assert "BPCL" in candidates


def test_content_extraction_non_ticker_words_blocked():
    """Blocklisted acronyms (SEBI, NSE, FII, ETF…) are never returned."""
    candidates = _extract_candidates(
        "SEBI NSE BSE FII ETF IPO NIFTY SENSEX GDP CPI PMI RBI announcement"
    )
    for blocked in ("SEBI", "NSE", "BSE", "FII", "ETF", "IPO", "NIFTY", "SENSEX",
                    "GDP", "CPI", "PMI", "RBI"):
        assert blocked not in candidates, f"{blocked} should be blocked"


def test_content_extraction_sagarmala():
    """Corporate-suffix company name is extracted for name-based DB lookup."""
    candidates = _extract_candidates(
        "Sagarmala Finance Corporation is set to launch India's first blue bonds"
    )
    assert any("Sagarmala Finance" in c for c in candidates)


# ── Pydantic schema tests ──────────────────────────────────────────────────────

def test_classification_schema_valid():
    """_ClassificationSchema validates correct structured output."""
    s = _ClassificationSchema(
        event_type="regulatory",
        impact_score=80.0,
        confidence=0.9,
        affected_symbols=["VEDL"],
        reasoning="ED visit after FEMA investigation.",
        decay_hours=24,
        decay_slow_hours=168,
    )
    assert s.event_type == "regulatory"
    assert s.affected_symbols == ["VEDL"]


def test_classification_schema_string_coercion():
    """Pydantic coerces string numbers from LLM output to float/int."""
    s = _ClassificationSchema.model_validate({
        "event_type": "earnings",
        "impact_score": "75",    # string
        "confidence": "0.85",    # string
        "affected_symbols": ["TCS"],
    })
    assert s.impact_score == 75.0
    assert s.confidence == 0.85


def test_classification_schema_invalid_event_type_raises():
    """Unknown event_type raises ValidationError (not silently coerced to garbage)."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        _ClassificationSchema.model_validate({"event_type": "completely_unknown"})


def test_classification_schema_defaults():
    """Default values are sensible when LLM omits optional fields."""
    s = _ClassificationSchema()
    assert s.event_type == "general"
    assert s.confidence == 0.0
    assert s.affected_symbols == []
    assert 4 <= s.decay_hours <= 48
    assert 24 <= s.decay_slow_hours <= 168
