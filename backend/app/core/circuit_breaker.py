"""
Cortex AI — Circuit Breaker Pattern
====================================
Production-grade circuit breaker for external API calls to prevent cascading failures.

Circuit Breaker States:
- CLOSED: Normal operation, requests pass through
- OPEN: Failure threshold exceeded, requests fail fast
- HALF_OPEN: Testing recovery, limited requests allowed

Best Practices:
- Fail fast when service is down (don't wait for timeout)
- Automatic recovery testing (half-open state)
- Metrics tracking for monitoring
- Configurable per-service thresholds
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiobreaker import CircuitBreaker, CircuitBreakerError
from prometheus_client import Counter, Gauge

logger = logging.getLogger(__name__)

# ── Prometheus Metrics ─────────────────────────────────────────────────────

circuit_breaker_state = Gauge(
    'circuit_breaker_state',
    'Circuit breaker state (0=closed, 1=open, 2=half_open)',
    ['service']
)

circuit_breaker_failures = Counter(
    'circuit_breaker_failures_total',
    'Total circuit breaker failures',
    ['service', 'exception_type']
)

circuit_breaker_successes = Counter(
    'circuit_breaker_successes_total',
    'Total circuit breaker successes',
    ['service']
)

circuit_breaker_rejections = Counter(
    'circuit_breaker_rejections_total',
    'Total circuit breaker rejections (open state)',
    ['service']
)


# ── Circuit Breaker Factory ────────────────────────────────────────────────

def create_circuit_breaker(
    service_name: str,
    failure_threshold: int = 5,
    recovery_timeout: int = 60,
    exclude: list[type[Exception]] | None = None,
) -> CircuitBreaker:
    """
    Create a circuit breaker for a service.

    Args:
        service_name: Service identifier for metrics
        failure_threshold: Number of failures before opening circuit
        recovery_timeout: Seconds to wait before attempting recovery
        exclude: Exception types that must NOT count as failures.
                 Use for permanent, non-transient errors (e.g. invalid input)
                 that say nothing about the upstream service's health.

    Returns:
        Configured CircuitBreaker instance
    """
    breaker = CircuitBreaker(
        fail_max=failure_threshold,
        timeout_duration=timedelta(seconds=recovery_timeout),
        name=service_name,
        exclude=exclude or [],
    )
    
    # Initialize state metric
    circuit_breaker_state.labels(service=service_name).set(0)
    
    logger.info(
        f"Circuit breaker created for {service_name}",
        extra={
            "service": service_name,
            "failure_threshold": failure_threshold,
            "recovery_timeout": recovery_timeout
        }
    )
    
    return breaker


def handle_circuit_breaker_error(service_name: str, error: CircuitBreakerError) -> None:
    """
    Handle circuit breaker rejection (circuit is open).
    
    Args:
        service_name: Service identifier
        error: CircuitBreakerError from aiobreaker
    """
    circuit_breaker_rejections.labels(service=service_name).inc()
    logger.warning(
        f"Circuit breaker REJECTED request for {service_name} (circuit is open)",
        extra={"service": service_name}
    )


# ── Pre-configured Circuit Breakers ────────────────────────────────────────

# Upstox API circuit breaker
# - 5 failures triggers open state
# - 60 second recovery timeout
# - Protects against Upstox API outages
from app.exceptions import UpstoxInvalidInstrumentError, UpstoxRateLimitError  # noqa: E402

upstox_breaker = create_circuit_breaker(
    service_name="upstox",
    failure_threshold=5,
    recovery_timeout=60,
    # Excluded from circuit breaker failure count:
    #   429 — rate limited (back off and retry; API is healthy)
    #   400 — invalid instrument key (permanent data error; API is healthy)
    exclude=[UpstoxRateLimitError, UpstoxInvalidInstrumentError],
)


# ── Initialize Metrics ─────────────────────────────────────────────────────

def init_circuit_breaker_metrics() -> None:
    """Initialize circuit breaker metrics on application startup."""
    # Metrics are automatically initialized when circuit breakers are created
    logger.info("Circuit breaker metrics initialized")


# Initialize on module import
init_circuit_breaker_metrics()
