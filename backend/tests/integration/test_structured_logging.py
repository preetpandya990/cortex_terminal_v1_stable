"""
Integration Tests for Structured Logging
=========================================
Tests JSON logging, request ID propagation, correlation tracking, and PII masking.
"""
import json
import logging
from io import StringIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.json_formatter import ContextualJsonFormatter
from app.core.logging_config import (
    mask_credit_card,
    mask_email,
    mask_phone,
    mask_pii,
    mask_token,
)
from app.core.request_context import (
    clear_request_context,
    get_request_context,
    set_request_context,
)
from app.main import create_app


@pytest.fixture
def app():
    """Create FastAPI app for testing."""
    return create_app()


@pytest.fixture
def client(app):
    """Create test client."""
    return TestClient(app)


@pytest.fixture
def log_stream():
    """Create log stream for capturing logs."""
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextualJsonFormatter())
    
    logger = logging.getLogger("test_logger")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    
    yield stream, logger
    
    logger.removeHandler(handler)


class TestJSONFormatter:
    """Test JSON log formatting."""
    
    def test_basic_json_format(self, log_stream):
        """Test that logs are valid JSON."""
        stream, logger = log_stream
        
        logger.info("Test message")
        
        log_output = stream.getvalue()
        log_json = json.loads(log_output)
        
        assert log_json["message"] == "Test message"
        assert log_json["level"] == "INFO"
        assert log_json["logger"] == "test_logger"
        assert "timestamp" in log_json
    
    def test_extra_fields(self, log_stream):
        """Test that extra fields are included in logs."""
        stream, logger = log_stream
        
        logger.info(
            "Test with extras",
            extra={
                "user_id": "user123",
                "request_id": "req456",
                "custom_field": "custom_value",
            }
        )
        
        log_output = stream.getvalue()
        log_json = json.loads(log_output)
        
        assert log_json["user_id"] == "user123"
        assert log_json["request_id"] == "req456"
        assert log_json["custom_field"] == "custom_value"
    
    def test_context_fields(self, log_stream):
        """Test that context fields are automatically included."""
        stream, logger = log_stream
        
        # Set request context
        set_request_context(
            request_id="ctx-req-123",
            correlation_id="ctx-corr-456",
            user_id="ctx-user-789",
            endpoint="/api/v1/test",
            method="GET",
        )
        
        logger.info("Test with context")
        
        log_output = stream.getvalue()
        log_json = json.loads(log_output)
        
        assert log_json["request_id"] == "ctx-req-123"
        assert log_json["correlation_id"] == "ctx-corr-456"
        assert log_json["user_id"] == "ctx-user-789"
        assert log_json["endpoint"] == "/api/v1/test"
        assert log_json["method"] == "GET"
        
        # Cleanup
        clear_request_context()


class TestRequestIDMiddleware:
    """Test request ID middleware."""
    
    def test_request_id_generation(self, client):
        """Test that request ID is generated if not provided."""
        response = client.get("/health")
        
        assert response.status_code == 200
        assert "X-Request-ID" in response.headers
        assert "X-Correlation-ID" in response.headers
        
        # Should be valid UUIDs
        request_id = response.headers["X-Request-ID"]
        correlation_id = response.headers["X-Correlation-ID"]
        
        assert len(request_id) == 36  # UUID format
        assert len(correlation_id) == 36
    
    def test_request_id_propagation(self, client):
        """Test that provided request ID is propagated."""
        custom_request_id = str(uuid4())
        
        response = client.get(
            "/health",
            headers={"X-Request-ID": custom_request_id}
        )
        
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == custom_request_id
    
    def test_correlation_id_propagation(self, client):
        """Test that provided correlation ID is propagated."""
        custom_correlation_id = str(uuid4())
        
        response = client.get(
            "/health",
            headers={"X-Correlation-ID": custom_correlation_id}
        )
        
        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"] == custom_correlation_id
    
    def test_both_ids_propagation(self, client):
        """Test that both IDs are propagated together."""
        custom_request_id = str(uuid4())
        custom_correlation_id = str(uuid4())
        
        response = client.get(
            "/health",
            headers={
                "X-Request-ID": custom_request_id,
                "X-Correlation-ID": custom_correlation_id,
            }
        )
        
        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == custom_request_id
        assert response.headers["X-Correlation-ID"] == custom_correlation_id


class TestRequestContext:
    """Test request context storage."""
    
    def test_set_and_get_context(self):
        """Test setting and getting request context."""
        set_request_context(
            request_id="test-req-123",
            correlation_id="test-corr-456",
            user_id="test-user-789",
            endpoint="/api/v1/test",
            method="POST",
        )
        
        context = get_request_context()
        
        assert context["request_id"] == "test-req-123"
        assert context["correlation_id"] == "test-corr-456"
        assert context["user_id"] == "test-user-789"
        assert context["endpoint"] == "/api/v1/test"
        assert context["method"] == "POST"
        
        clear_request_context()
    
    def test_clear_context(self):
        """Test clearing request context."""
        set_request_context(request_id="test-123")
        clear_request_context()
        
        context = get_request_context()
        
        assert context["request_id"] is None
        assert context["correlation_id"] is None
        assert context["user_id"] is None


class TestPIIMasking:
    """Test PII masking utilities."""
    
    def test_mask_email(self):
        """Test email masking."""
        assert mask_email("user@example.com") == "u***@example.com"
        assert mask_email("a@test.com") == "a***@test.com"
        assert mask_email("invalid") == "invalid"
    
    def test_mask_token(self):
        """Test token masking."""
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        assert mask_token(token) == "eyJh..."
        assert mask_token("short") == "shor..."  # Short tokens still get masked
    
    def test_mask_phone(self):
        """Test phone number masking."""
        assert mask_phone("+1234567890") == "+123***7890"
        assert mask_phone("123") == "***"
    
    def test_mask_credit_card(self):
        """Test credit card masking."""
        assert mask_credit_card("1234567890123456") == "1234********3456"
        assert mask_credit_card("1234567") == "***"
    
    def test_mask_pii_dict(self):
        """Test PII masking in dictionaries."""
        data = {
            "email": "user@example.com",
            "password": "secret123",
            "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "phone": "+1234567890",
            "credit_card": "1234567890123456",
            "safe_field": "safe_value",
        }
        
        masked = mask_pii(data)
        
        assert masked["email"] == "u***@example.com"
        assert masked["password"] == "***"
        assert masked["token"] == "eyJh..."
        assert masked["phone"] == "+123***7890"
        assert masked["credit_card"] == "1234********3456"
        assert masked["safe_field"] == "safe_value"
    
    def test_mask_pii_nested(self):
        """Test PII masking in nested structures."""
        data = {
            "user": {
                "email": "user@example.com",
                "password": "secret123",
            },
            "tokens": ["token1", "token2"],
        }
        
        masked = mask_pii(data)
        
        assert masked["user"]["email"] == "u***@example.com"
        assert masked["user"]["password"] == "***"
        assert isinstance(masked["tokens"], list)


class TestEndToEndLogging:
    """Test end-to-end logging flow."""
    
    def test_api_request_logging(self, client):
        """Test that API requests include request context in headers."""
        response = client.get("/health")
        
        assert response.status_code == 200
        
        # Verify request ID and correlation ID are in response headers
        assert "X-Request-ID" in response.headers
        assert "X-Correlation-ID" in response.headers
        
        # Verify they are valid UUIDs
        request_id = response.headers["X-Request-ID"]
        correlation_id = response.headers["X-Correlation-ID"]
        
        assert len(request_id) == 36  # UUID format
        assert len(correlation_id) == 36
        assert "-" in request_id
        assert "-" in correlation_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
