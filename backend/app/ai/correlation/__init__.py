"""
Event Correlation Package
=========================
Bidirectional multi-agent consensus system for trade suggestions.
"""
from app.ai.correlation.engine import CircuitBreaker, EventCorrelationEngine

__all__ = [
    "EventCorrelationEngine",
    "CircuitBreaker",
]
