"""
Test Ollama LLM Client

Tests OllamaClient with mocked ollama.Client.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from app.ai.intelligence.llm_client import OllamaClient, get_ollama_client


@pytest.fixture
def mock_ollama_client():
    """Mock ollama.Client."""
    with patch("app.ai.intelligence.llm_client.ollama.Client") as mock:
        yield mock


@pytest.fixture
def ollama_client(mock_ollama_client):
    """Create OllamaClient with mocked ollama.Client."""
    # Reset singleton
    OllamaClient._instance = None
    client = OllamaClient()
    return client


@pytest.mark.asyncio
async def test_ollama_client_singleton():
    """Test OllamaClient is a singleton."""
    OllamaClient._instance = None
    client1 = OllamaClient()
    client2 = OllamaClient()
    assert client1 is client2


@pytest.mark.asyncio
async def test_verify_model_availability_success(ollama_client):
    """Test verify_model_availability succeeds when model exists."""
    # Mock list response
    ollama_client.client.list = MagicMock(return_value={
        "models": [
            {"name": "llama3.1:8b"},
            {"name": "mistral:7b"},
        ]
    })
    
    result = await ollama_client.verify_model_availability(max_retries=1)
    
    assert result is True
    ollama_client.client.list.assert_called_once()


@pytest.mark.asyncio
async def test_verify_model_availability_model_not_found(ollama_client):
    """Test verify_model_availability fails when model not found."""
    ollama_client.client.list = MagicMock(return_value={
        "models": [
            {"name": "mistral:7b"},
        ]
    })
    
    result = await ollama_client.verify_model_availability(max_retries=1)
    
    assert result is False


@pytest.mark.asyncio
async def test_verify_model_availability_retries(ollama_client):
    """Test verify_model_availability retries on failure."""
    # Fail first 2 times, succeed on 3rd
    ollama_client.client.list = MagicMock(side_effect=[
        Exception("Connection refused"),
        Exception("Connection refused"),
        {"models": [{"name": "llama3.1:8b"}]},
    ])
    
    result = await ollama_client.verify_model_availability(
        max_retries=3,
        retry_delay=0.1,  # Fast retry for testing
    )
    
    assert result is True
    assert ollama_client.client.list.call_count == 3


@pytest.mark.asyncio
async def test_verify_model_availability_max_retries_exceeded(ollama_client):
    """Test verify_model_availability fails after max retries."""
    ollama_client.client.list = MagicMock(side_effect=Exception("Connection refused"))
    
    result = await ollama_client.verify_model_availability(
        max_retries=2,
        retry_delay=0.1,
    )
    
    assert result is False
    assert ollama_client.client.list.call_count == 2


@pytest.mark.asyncio
async def test_generate_success(ollama_client):
    """Test generate returns response successfully."""
    ollama_client.client.chat = MagicMock(return_value={
        "message": {"content": "This is a test response"},
        "done": True,
        "total_duration": 1000000,
        "prompt_eval_count": 10,
        "eval_count": 20,
    })
    
    response = await ollama_client.generate(
        prompt="Test prompt",
        system="Test system",
        temperature=0.7,
    )
    
    assert response["content"] == "This is a test response"
    assert response["model"] == "llama3.1:8b"
    assert response["done"] is True
    
    # Verify chat was called with correct args
    call_args = ollama_client.client.chat.call_args
    assert call_args[1]["model"] == "llama3.1:8b"
    assert len(call_args[1]["messages"]) == 2
    assert call_args[1]["messages"][0]["role"] == "system"
    assert call_args[1]["messages"][1]["role"] == "user"


@pytest.mark.asyncio
async def test_generate_without_system_prompt(ollama_client):
    """Test generate works without system prompt."""
    ollama_client.client.chat = MagicMock(return_value={
        "message": {"content": "Response"},
        "done": True,
    })
    
    response = await ollama_client.generate(prompt="Test")
    
    # Should only have user message
    call_args = ollama_client.client.chat.call_args
    assert len(call_args[1]["messages"]) == 1
    assert call_args[1]["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_generate_with_retry_success_after_failure(ollama_client):
    """Test generate retries and succeeds."""
    ollama_client.client.chat = MagicMock(side_effect=[
        Exception("Timeout"),
        {"message": {"content": "Success"}, "done": True},
    ])
    
    response = await ollama_client.generate(prompt="Test")
    
    assert response["content"] == "Success"
    assert ollama_client.client.chat.call_count == 2


@pytest.mark.asyncio
async def test_generate_fails_after_max_retries(ollama_client):
    """Test generate raises after max retries."""
    ollama_client.client.chat = MagicMock(side_effect=Exception("Timeout"))
    
    with pytest.raises(Exception, match="failed after 3 attempts"):
        await ollama_client.generate(prompt="Test")


@pytest.mark.asyncio
async def test_generate_json_success(ollama_client):
    """Test generate_json parses JSON response."""
    ollama_client.client.chat = MagicMock(return_value={
        "message": {"content": '{"key": "value", "number": 42}'},
        "done": True,
    })
    
    result = await ollama_client.generate_json(prompt="Return JSON")
    
    assert result == {"key": "value", "number": 42}


@pytest.mark.asyncio
async def test_generate_json_extracts_from_markdown(ollama_client):
    """Test generate_json extracts JSON from markdown code blocks."""
    ollama_client.client.chat = MagicMock(return_value={
        "message": {"content": '```json\n{"extracted": true}\n```'},
        "done": True,
    })
    
    result = await ollama_client.generate_json(prompt="Return JSON")
    
    assert result == {"extracted": True}


@pytest.mark.asyncio
async def test_generate_json_invalid_json_raises(ollama_client):
    """Test generate_json raises on invalid JSON."""
    ollama_client.client.chat = MagicMock(return_value={
        "message": {"content": "This is not JSON"},
        "done": True,
    })
    
    with pytest.raises(ValueError, match="Invalid JSON response"):
        await ollama_client.generate_json(prompt="Return JSON")


@pytest.mark.asyncio
async def test_health_check_success(ollama_client):
    """Test health_check returns True when service is healthy."""
    ollama_client.client.list = MagicMock(return_value={"models": []})
    
    result = await ollama_client.health_check()
    
    assert result is True


@pytest.mark.asyncio
async def test_health_check_failure(ollama_client):
    """Test health_check returns False on failure."""
    ollama_client.client.list = MagicMock(side_effect=Exception("Connection refused"))
    
    result = await ollama_client.health_check()
    
    assert result is False


def test_get_ollama_client_returns_singleton():
    """Test get_ollama_client returns singleton instance."""
    with patch("app.ai.intelligence.llm_client.ollama.Client"):
        client1 = get_ollama_client()
        client2 = get_ollama_client()
        assert client1 is client2
