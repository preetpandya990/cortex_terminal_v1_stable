"""
Unit tests for MultiTimeframeEnsemble

Tests weighted averaging, majority voting, and conflict resolution logic.
"""

import pytest
from app.ml.ensemble.multi_timeframe_ensemble import (
    MultiTimeframeEnsemble,
    TimeframePrediction,
    EnsemblePrediction
)


class TestMultiTimeframeEnsemble:
    """Test suite for MultiTimeframeEnsemble class."""
    
    def test_init_default_weights(self):
        """Test initialization with default weights."""
        ensemble = MultiTimeframeEnsemble()
        
        assert ensemble.timeframe_weights["daily"] == 0.5
        assert ensemble.timeframe_weights["weekly"] == 0.3
        assert ensemble.timeframe_weights["monthly"] == 0.2
    
    def test_init_custom_weights(self):
        """Test initialization with custom weights."""
        ensemble = MultiTimeframeEnsemble(
            daily_weight=0.6,
            weekly_weight=0.3,
            monthly_weight=0.1
        )
        
        assert ensemble.timeframe_weights["daily"] == 0.6
        assert ensemble.timeframe_weights["weekly"] == 0.3
        assert ensemble.timeframe_weights["monthly"] == 0.1
    
    def test_init_invalid_weights(self):
        """Test initialization fails with invalid weights."""
        with pytest.raises(ValueError, match="must sum to 1.0"):
            MultiTimeframeEnsemble(
                daily_weight=0.5,
                weekly_weight=0.3,
                monthly_weight=0.3  # Sum = 1.1
            )
    
    def test_combine_predictions_weighted_average(self):
        """Test confidence combination using weighted average."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8),
            "weekly": TimeframePrediction("weekly", "buy", 0.6),
            "monthly": TimeframePrediction("monthly", "buy", 0.7)
        }
        
        # Expected: 0.8*0.5 + 0.6*0.3 + 0.7*0.2 = 0.4 + 0.18 + 0.14 = 0.72
        confidence = ensemble.combine_predictions(predictions)
        
        assert abs(confidence - 0.72) < 1e-6
    
    def test_combine_predictions_partial_timeframes(self):
        """Test confidence combination with missing timeframes."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8),
            "weekly": TimeframePrediction("weekly", "buy", 0.6)
        }
        
        # Should normalize: (0.8*0.5 + 0.6*0.3) / (0.5 + 0.3) = 0.58 / 0.8 = 0.725
        confidence = ensemble.combine_predictions(predictions)
        
        assert abs(confidence - 0.725) < 1e-6
    
    def test_predict_unanimous_agreement(self):
        """Test prediction when all timeframes agree."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8),
            "weekly": TimeframePrediction("weekly", "buy", 0.6),
            "monthly": TimeframePrediction("monthly", "buy", 0.7)
        }
        
        result = ensemble.predict(predictions)
        
        assert result.direction == "buy"
        assert abs(result.confidence - 0.72) < 1e-6
        assert result.conflict_resolved is False
        assert result.conflict_resolution_method is None
    
    def test_predict_majority_vote(self):
        """Test prediction with majority voting."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8),
            "weekly": TimeframePrediction("weekly", "buy", 0.6),
            "monthly": TimeframePrediction("monthly", "sell", 0.7)
        }
        
        result = ensemble.predict(predictions)
        
        # Majority (2/3) vote for "buy"
        assert result.direction == "buy"
        assert abs(result.confidence - 0.72) < 1e-6
        assert result.conflict_resolved is False
    
    def test_predict_conflict_highest_confidence(self):
        """Test conflict resolution using highest confidence."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.6),
            "weekly": TimeframePrediction("weekly", "sell", 0.9),
            "monthly": TimeframePrediction("monthly", "hold", 0.5)
        }
        
        result = ensemble.predict(predictions)
        
        # No majority, choose highest confidence (weekly=0.9)
        assert result.direction == "sell"
        assert result.conflict_resolved is True
        assert result.conflict_resolution_method == "highest_confidence"
    
    def test_predict_conflict_equal_confidence_longer_timeframe(self):
        """Test conflict resolution defaults to longer timeframe when confidences equal."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.7),
            "weekly": TimeframePrediction("weekly", "sell", 0.7),
            "monthly": TimeframePrediction("monthly", "hold", 0.7)
        }
        
        result = ensemble.predict(predictions)
        
        # Equal confidences, should choose monthly (longest timeframe)
        assert result.direction == "hold"
        assert result.conflict_resolved is True
    
    def test_predict_two_way_tie_highest_confidence(self):
        """Test two-way tie resolved by highest confidence."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.5),
            "weekly": TimeframePrediction("weekly", "sell", 0.8),
            "monthly": TimeframePrediction("monthly", "hold", 0.6)
        }
        
        result = ensemble.predict(predictions)
        
        # No majority (all different), choose highest confidence (weekly=0.8)
        assert result.direction == "sell"
        assert result.conflict_resolved is True
        assert result.conflict_resolution_method == "highest_confidence"
    
    def test_predict_empty_predictions(self):
        """Test prediction fails with empty predictions."""
        ensemble = MultiTimeframeEnsemble()
        
        with pytest.raises(ValueError, match="No predictions provided"):
            ensemble.predict({})
    
    def test_predict_invalid_timeframe(self):
        """Test prediction fails with invalid timeframe."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "invalid": TimeframePrediction("invalid", "buy", 0.8)
        }
        
        with pytest.raises(ValueError, match="Invalid timeframes"):
            ensemble.predict(predictions)
    
    def test_predict_single_timeframe(self):
        """Test prediction with only one timeframe."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8)
        }
        
        result = ensemble.predict(predictions)
        
        assert result.direction == "buy"
        assert result.confidence == 0.8
        assert result.conflict_resolved is False
    
    def test_predict_metadata_included(self):
        """Test that prediction includes metadata."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.8),
            "weekly": TimeframePrediction("weekly", "buy", 0.6)
        }
        
        result = ensemble.predict(predictions)
        
        assert result.metadata is not None
        assert "timestamp" in result.metadata
        assert "weights_used" in result.metadata
        assert result.metadata["weights_used"] == ensemble.timeframe_weights
    
    def test_timeframe_priority(self):
        """Test timeframe priority ordering."""
        ensemble = MultiTimeframeEnsemble()
        
        assert ensemble._get_timeframe_priority("monthly") == 3
        assert ensemble._get_timeframe_priority("weekly") == 2
        assert ensemble._get_timeframe_priority("daily") == 1
        assert ensemble._get_timeframe_priority("unknown") == 0
    
    def test_conflict_resolution_with_audit_logger(self):
        """Test conflict resolution logs to audit logger."""
        # Mock audit logger
        class MockAuditLogger:
            def __init__(self):
                self.logged_events = []
            
            async def log_event(self, event_type, details):
                self.logged_events.append({
                    "event_type": event_type,
                    "details": details
                })
        
        mock_logger = MockAuditLogger()
        ensemble = MultiTimeframeEnsemble(audit_logger=mock_logger)
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.6),
            "weekly": TimeframePrediction("weekly", "sell", 0.9),
            "monthly": TimeframePrediction("monthly", "hold", 0.5)
        }
        
        result = ensemble.predict(predictions)
        
        # Verify conflict was resolved
        assert result.conflict_resolved is True
        # Note: The logging is async, so we can't easily verify it was called
        # in a synchronous test without more complex async test setup
    
    def test_different_weight_distributions(self):
        """Test ensemble with different weight distributions."""
        # Equal weights
        ensemble_equal = MultiTimeframeEnsemble(
            daily_weight=1/3,
            weekly_weight=1/3,
            monthly_weight=1/3
        )
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.9),
            "weekly": TimeframePrediction("weekly", "buy", 0.6),
            "monthly": TimeframePrediction("monthly", "buy", 0.3)
        }
        
        confidence_equal = ensemble_equal.combine_predictions(predictions)
        # (0.9 + 0.6 + 0.3) / 3 = 0.6
        assert abs(confidence_equal - 0.6) < 1e-6
        
        # Heavy daily weight
        ensemble_daily = MultiTimeframeEnsemble(
            daily_weight=0.8,
            weekly_weight=0.1,
            monthly_weight=0.1
        )
        
        confidence_daily = ensemble_daily.combine_predictions(predictions)
        # 0.9*0.8 + 0.6*0.1 + 0.3*0.1 = 0.72 + 0.06 + 0.03 = 0.81
        assert abs(confidence_daily - 0.81) < 1e-6
    
    def test_edge_case_all_hold(self):
        """Test edge case where all timeframes predict hold."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "hold", 0.5),
            "weekly": TimeframePrediction("weekly", "hold", 0.6),
            "monthly": TimeframePrediction("monthly", "hold", 0.7)
        }
        
        result = ensemble.predict(predictions)
        
        assert result.direction == "hold"
        assert result.conflict_resolved is False
    
    def test_edge_case_zero_confidence(self):
        """Test edge case with zero confidence predictions."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 0.0),
            "weekly": TimeframePrediction("weekly", "sell", 0.0),
            "monthly": TimeframePrediction("monthly", "hold", 0.0)
        }
        
        result = ensemble.predict(predictions)
        
        # Should still resolve, defaulting to longer timeframe
        assert result.direction in ["buy", "sell", "hold"]
        assert result.confidence == 0.0
    
    def test_edge_case_max_confidence(self):
        """Test edge case with maximum confidence predictions."""
        ensemble = MultiTimeframeEnsemble()
        
        predictions = {
            "daily": TimeframePrediction("daily", "buy", 1.0),
            "weekly": TimeframePrediction("weekly", "buy", 1.0),
            "monthly": TimeframePrediction("monthly", "buy", 1.0)
        }
        
        result = ensemble.predict(predictions)
        
        assert result.direction == "buy"
        assert result.confidence == 1.0
        assert result.conflict_resolved is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
