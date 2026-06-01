"""
Unit tests for Circuit Breaker
"""
import pytest
from datetime import datetime, timedelta

from app.ai.fusion.signal_pipeline import CircuitBreaker


class TestCircuitBreaker:
    """Test circuit breaker functionality."""

    def test_init_state_is_closed(self):
        """Test that circuit breaker starts in closed state."""
        breaker = CircuitBreaker()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_record_success_in_closed_state(self):
        """Test recording success in closed state."""
        breaker = CircuitBreaker()
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0

    def test_record_failure_increments_count(self):
        """Test that failures increment counter."""
        breaker = CircuitBreaker(failure_threshold=3)
        breaker.record_failure()
        assert breaker.failure_count == 1
        assert breaker.state == "closed"

    def test_circuit_opens_after_threshold(self):
        """Test that circuit opens after threshold failures."""
        breaker = CircuitBreaker(failure_threshold=3)
        
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == "closed"
        
        breaker.record_failure()
        assert breaker.state == "open"

    def test_can_attempt_when_closed(self):
        """Test that attempts are allowed when closed."""
        breaker = CircuitBreaker()
        assert breaker.can_attempt() is True

    def test_cannot_attempt_when_open(self):
        """Test that attempts are blocked when open."""
        breaker = CircuitBreaker(failure_threshold=1)
        breaker.record_failure()
        assert breaker.state == "open"
        assert breaker.can_attempt() is False

    def test_transitions_to_half_open_after_timeout(self):
        """Test transition to half-open after timeout."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=1)
        breaker.record_failure()
        assert breaker.state == "open"
        
        # Simulate timeout by setting last_failure_time in past
        breaker.last_failure_time = datetime.utcnow() - timedelta(seconds=2)
        
        assert breaker.can_attempt() is True
        assert breaker.state == "half_open"

    def test_half_open_closes_on_success(self):
        """Test that half-open closes on success."""
        breaker = CircuitBreaker(failure_threshold=1, timeout_seconds=1)
        breaker.record_failure()
        breaker.last_failure_time = datetime.utcnow() - timedelta(seconds=2)
        breaker.can_attempt()  # Transition to half_open
        
        breaker.record_success()
        assert breaker.state == "closed"
        assert breaker.failure_count == 0
