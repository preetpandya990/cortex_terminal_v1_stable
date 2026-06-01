"""
Latency Monitoring for ML Prediction System

Tracks prediction latency metrics:
- P50, P95, P99 latency percentiles
- Latency breakdown by component (feature loading, inference, SHAP)
- Slow prediction logging and alerting

Requirements: 3.3, 17.3, 24.5
"""

import logging
import time
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from collections import deque
import asyncio
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class LatencyBreakdown:
    """Breakdown of latency by component."""
    total_ms: float
    feature_loading_ms: float
    model_inference_ms: float
    shap_computation_ms: float
    post_processing_ms: float
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class LatencyStats:
    """Latency statistics."""
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    mean_ms: float
    count: int
    slow_predictions: int  # Count of predictions >150ms


class LatencyMonitor:
    """
    Monitors prediction latency and provides statistics.
    
    Tracks:
    - Real-time latency percentiles (P50, P95, P99)
    - Latency breakdown by component
    - Slow prediction detection and logging
    - Alerting when latency exceeds thresholds
    """
    
    def __init__(
        self,
        window_size: int = 1000,
        slow_threshold_ms: float = 150.0,
        alert_threshold_p95_ms: float = 200.0
    ):
        """
        Initialize latency monitor.
        
        Args:
            window_size: Number of recent predictions to track (default: 1000)
            slow_threshold_ms: Threshold for slow prediction logging (default: 150ms)
            alert_threshold_p95_ms: P95 threshold for alerting (default: 200ms)
        """
        self.window_size = window_size
        self.slow_threshold_ms = slow_threshold_ms
        self.alert_threshold_p95_ms = alert_threshold_p95_ms
        
        # Rolling window of latencies
        self._latencies: deque = deque(maxlen=window_size)
        self._breakdowns: deque = deque(maxlen=window_size)
        
        # Counters
        self._total_predictions = 0
        self._slow_predictions = 0
        
        # Lock for thread safety
        self._lock = asyncio.Lock()
    
    async def record_latency(
        self,
        breakdown: LatencyBreakdown,
        symbol: str,
        timeframe: str
    ) -> None:
        """
        Record latency for a prediction.
        
        Args:
            breakdown: Latency breakdown by component
            symbol: Stock symbol
            timeframe: Timeframe
            
        Requirements: 3.3, 24.5
        """
        async with self._lock:
            self._latencies.append(breakdown.total_ms)
            self._breakdowns.append(breakdown)
            self._total_predictions += 1
            
            # Check if slow prediction
            if breakdown.total_ms > self.slow_threshold_ms:
                self._slow_predictions += 1
                await self._log_slow_prediction(breakdown, symbol, timeframe)
            
            # Check if P95 exceeds alert threshold (every 100 predictions)
            if self._total_predictions % 100 == 0:
                await self._check_alert_threshold()
    
    async def _log_slow_prediction(
        self,
        breakdown: LatencyBreakdown,
        symbol: str,
        timeframe: str
    ) -> None:
        """
        Log slow prediction with timing details.
        
        Args:
            breakdown: Latency breakdown
            symbol: Stock symbol
            timeframe: Timeframe
            
        Requirements: 24.5
        """
        logger.warning(
            f"Slow prediction detected: {breakdown.total_ms:.2f}ms for {symbol} ({timeframe}). "
            f"Breakdown: feature_loading={breakdown.feature_loading_ms:.2f}ms, "
            f"inference={breakdown.model_inference_ms:.2f}ms, "
            f"shap={breakdown.shap_computation_ms:.2f}ms, "
            f"post_processing={breakdown.post_processing_ms:.2f}ms"
        )
    
    async def _check_alert_threshold(self) -> None:
        """
        Check if P95 latency exceeds alert threshold.
        
        Requirements: 17.3
        """
        stats = await self.get_stats()
        
        if stats.p95_ms > self.alert_threshold_p95_ms:
            logger.error(
                f"ALERT: P95 latency ({stats.p95_ms:.2f}ms) exceeds threshold "
                f"({self.alert_threshold_p95_ms}ms). "
                f"P99={stats.p99_ms:.2f}ms, max={stats.max_ms:.2f}ms"
            )
            # In production, this would trigger alerting system (Slack, PagerDuty, etc.)
    
    async def get_stats(self) -> LatencyStats:
        """
        Get current latency statistics.
        
        Returns:
            LatencyStats with percentiles and counts
            
        Requirements: 3.3, 17.3
        """
        async with self._lock:
            if not self._latencies:
                return LatencyStats(
                    p50_ms=0.0,
                    p95_ms=0.0,
                    p99_ms=0.0,
                    max_ms=0.0,
                    mean_ms=0.0,
                    count=0,
                    slow_predictions=0
                )
            
            latencies_sorted = sorted(self._latencies)
            n = len(latencies_sorted)
            
            p50_idx = int(n * 0.50)
            p95_idx = int(n * 0.95)
            p99_idx = int(n * 0.99)
            
            return LatencyStats(
                p50_ms=latencies_sorted[p50_idx],
                p95_ms=latencies_sorted[p95_idx],
                p99_ms=latencies_sorted[p99_idx],
                max_ms=max(latencies_sorted),
                mean_ms=sum(latencies_sorted) / n,
                count=n,
                slow_predictions=self._slow_predictions
            )
    
    async def get_component_stats(self) -> Dict[str, LatencyStats]:
        """
        Get latency statistics broken down by component.
        
        Returns:
            Dict mapping component name to LatencyStats
            
        Requirements: 24.5
        """
        async with self._lock:
            if not self._breakdowns:
                return {}
            
            components = {
                "feature_loading": [b.feature_loading_ms for b in self._breakdowns],
                "model_inference": [b.model_inference_ms for b in self._breakdowns],
                "shap_computation": [b.shap_computation_ms for b in self._breakdowns],
                "post_processing": [b.post_processing_ms for b in self._breakdowns]
            }
            
            stats = {}
            for component, latencies in components.items():
                latencies_sorted = sorted(latencies)
                n = len(latencies_sorted)
                
                p50_idx = int(n * 0.50)
                p95_idx = int(n * 0.95)
                p99_idx = int(n * 0.99)
                
                stats[component] = LatencyStats(
                    p50_ms=latencies_sorted[p50_idx],
                    p95_ms=latencies_sorted[p95_idx],
                    p99_ms=latencies_sorted[p99_idx],
                    max_ms=max(latencies_sorted),
                    mean_ms=sum(latencies_sorted) / n,
                    count=n,
                    slow_predictions=0  # Not applicable for components
                )
            
            return stats
    
    async def get_recent_breakdowns(self, limit: int = 10) -> List[LatencyBreakdown]:
        """
        Get recent latency breakdowns.
        
        Args:
            limit: Number of recent breakdowns to return
            
        Returns:
            List of recent LatencyBreakdown objects
        """
        async with self._lock:
            return list(self._breakdowns)[-limit:]
    
    async def reset(self) -> None:
        """Reset all latency statistics."""
        async with self._lock:
            self._latencies.clear()
            self._breakdowns.clear()
            self._total_predictions = 0
            self._slow_predictions = 0


class LatencyTracker:
    """
    Context manager for tracking latency of operations.
    
    Usage:
        async with LatencyTracker() as tracker:
            # Do work
            tracker.mark("feature_loading")
            # More work
            tracker.mark("inference")
        
        breakdown = tracker.get_breakdown()
    """
    
    def __init__(self):
        self._start_time: Optional[float] = None
        self._marks: Dict[str, float] = {}
        self._mark_order: List[str] = []
    
    async def __aenter__(self):
        self._start_time = time.perf_counter()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
    
    def mark(self, name: str) -> None:
        """
        Mark a point in time for latency tracking.
        
        Args:
            name: Name of the checkpoint
        """
        if self._start_time is None:
            raise RuntimeError("LatencyTracker not started")
        
        current_time = time.perf_counter()
        self._marks[name] = current_time
        self._mark_order.append(name)
    
    def get_breakdown(self) -> LatencyBreakdown:
        """
        Get latency breakdown from marks.
        
        Returns:
            LatencyBreakdown with component timings
        """
        if self._start_time is None:
            raise RuntimeError("LatencyTracker not started")
        
        end_time = time.perf_counter()
        total_ms = (end_time - self._start_time) * 1000
        
        # Calculate component durations
        feature_loading_ms = 0.0
        model_inference_ms = 0.0
        shap_computation_ms = 0.0
        post_processing_ms = 0.0
        
        prev_time = self._start_time
        for mark_name in self._mark_order:
            mark_time = self._marks[mark_name]
            duration_ms = (mark_time - prev_time) * 1000
            
            if "feature" in mark_name.lower():
                feature_loading_ms += duration_ms
            elif "inference" in mark_name.lower() or "model" in mark_name.lower():
                model_inference_ms += duration_ms
            elif "shap" in mark_name.lower() or "explain" in mark_name.lower():
                shap_computation_ms += duration_ms
            else:
                post_processing_ms += duration_ms
            
            prev_time = mark_time
        
        # Add remaining time to post-processing
        post_processing_ms += (end_time - prev_time) * 1000
        
        return LatencyBreakdown(
            total_ms=total_ms,
            feature_loading_ms=feature_loading_ms,
            model_inference_ms=model_inference_ms,
            shap_computation_ms=shap_computation_ms,
            post_processing_ms=post_processing_ms
        )


# Global latency monitor instance
_latency_monitor: Optional[LatencyMonitor] = None


def get_latency_monitor() -> LatencyMonitor:
    """Get global latency monitor instance."""
    global _latency_monitor
    if _latency_monitor is None:
        _latency_monitor = LatencyMonitor()
    return _latency_monitor
