"""
Multi-Timeframe Ensemble for ML Predictions

Combines predictions from daily, weekly, and monthly timeframe models
using weighted averaging and majority voting with conflict resolution.

Requirements: 4.1, 4.4, 9.3
"""

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TimeframePrediction:
    """Prediction from a single timeframe model."""
    timeframe: str  # "daily", "weekly", "monthly"
    direction: str  # "buy", "sell", "hold"
    confidence: float  # 0.0 to 1.0
    metadata: Optional[Dict[str, Any]] = None


@dataclass
class EnsemblePrediction:
    """Combined prediction from multiple timeframes."""
    direction: str
    confidence: float
    timeframe_predictions: Dict[str, TimeframePrediction]
    conflict_resolved: bool
    conflict_resolution_method: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class MultiTimeframeEnsemble:
    """
    Multi-timeframe ensemble for combining predictions.
    
    Combines predictions from daily, weekly, and monthly models using:
    - Weighted averaging for confidence scores
    - Majority voting for direction with conflict resolution
    
    Default weights:
    - Daily: 0.5 (most recent, highest weight)
    - Weekly: 0.3 (medium-term trend)
    - Monthly: 0.2 (long-term trend)
    
    Requirements: 4.1, 4.4, 9.3
    """
    
    def __init__(
        self,
        daily_weight: float = 0.5,
        weekly_weight: float = 0.3,
        monthly_weight: float = 0.2,
        audit_logger: Optional[Any] = None
    ):
        """
        Initialize multi-timeframe ensemble.
        
        Args:
            daily_weight: Weight for daily timeframe predictions (default: 0.5)
            weekly_weight: Weight for weekly timeframe predictions (default: 0.3)
            monthly_weight: Weight for monthly timeframe predictions (default: 0.2)
            audit_logger: Optional audit logger for conflict resolution logging
            
        Raises:
            ValueError: If weights don't sum to 1.0
        """
        # Validate weights
        total_weight = daily_weight + weekly_weight + monthly_weight
        if abs(total_weight - 1.0) > 1e-6:
            raise ValueError(
                f"Timeframe weights must sum to 1.0, got {total_weight}"
            )
        
        self.timeframe_weights = {
            "daily": daily_weight,
            "weekly": weekly_weight,
            "monthly": monthly_weight
        }
        self.audit_logger = audit_logger
        
        logger.info(
            f"Initialized MultiTimeframeEnsemble with weights: "
            f"daily={daily_weight}, weekly={weekly_weight}, monthly={monthly_weight}"
        )
    
    def predict(
        self,
        predictions: Dict[str, TimeframePrediction]
    ) -> EnsemblePrediction:
        """
        Generate ensemble prediction from multiple timeframe predictions.
        
        Args:
            predictions: Dictionary mapping timeframe to prediction
                        Keys: "daily", "weekly", "monthly"
                        
        Returns:
            EnsemblePrediction with combined direction and confidence
            
        Requirements: 4.1, 4.4
        """
        if not predictions:
            raise ValueError("No predictions provided")
        
        # Validate all required timeframes are present
        required_timeframes = set(self.timeframe_weights.keys())
        provided_timeframes = set(predictions.keys())
        
        if not provided_timeframes.issubset(required_timeframes):
            invalid = provided_timeframes - required_timeframes
            raise ValueError(f"Invalid timeframes provided: {invalid}")
        
        # Get predictions for all available timeframes
        available_predictions = {
            tf: pred for tf, pred in predictions.items()
            if tf in self.timeframe_weights
        }
        
        if not available_predictions:
            raise ValueError("No valid timeframe predictions available")
        
        # Combine predictions
        combined_confidence = self.combine_predictions(available_predictions)
        final_direction, conflict_resolved, resolution_method = self._resolve_direction(
            available_predictions
        )
        
        ensemble_prediction = EnsemblePrediction(
            direction=final_direction,
            confidence=combined_confidence,
            timeframe_predictions=available_predictions,
            conflict_resolved=conflict_resolved,
            conflict_resolution_method=resolution_method,
            metadata={
                "timestamp": datetime.utcnow().isoformat(),
                "weights_used": self.timeframe_weights
            }
        )
        
        logger.info(
            f"Ensemble prediction: direction={final_direction}, "
            f"confidence={combined_confidence:.3f}, "
            f"conflict_resolved={conflict_resolved}"
        )
        
        return ensemble_prediction
    
    def combine_predictions(
        self,
        predictions: Dict[str, TimeframePrediction]
    ) -> float:
        """
        Combine confidence scores using weighted average.
        
        Args:
            predictions: Dictionary of timeframe predictions
            
        Returns:
            Weighted average confidence score
            
        Requirements: 4.1
        """
        if not predictions:
            return 0.0
        
        weighted_confidence = 0.0
        total_weight = 0.0
        
        for timeframe, prediction in predictions.items():
            if timeframe in self.timeframe_weights:
                weight = self.timeframe_weights[timeframe]
                weighted_confidence += prediction.confidence * weight
                total_weight += weight
        
        # Normalize by actual total weight (in case not all timeframes present)
        if total_weight > 0:
            weighted_confidence /= total_weight
        
        logger.debug(
            f"Combined confidence: {weighted_confidence:.3f} "
            f"from {len(predictions)} timeframes"
        )
        
        return weighted_confidence
    
    def _resolve_direction(
        self,
        predictions: Dict[str, TimeframePrediction]
    ) -> tuple[str, bool, Optional[str]]:
        """
        Resolve final direction using majority voting with conflict resolution.
        
        Conflict resolution rules:
        1. If all agree: use unanimous direction
        2. If majority agrees: use majority direction
        3. If no majority (3-way tie): choose highest confidence
        4. If confidences equal: default to longer timeframe
        
        Args:
            predictions: Dictionary of timeframe predictions
            
        Returns:
            Tuple of (final_direction, conflict_resolved, resolution_method)
            
        Requirements: 4.4, 9.3
        """
        if not predictions:
            return "hold", False, None
        
        # Count votes for each direction
        direction_votes: Dict[str, List[str]] = {}
        for timeframe, pred in predictions.items():
            direction = pred.direction
            if direction not in direction_votes:
                direction_votes[direction] = []
            direction_votes[direction].append(timeframe)
        
        # Check for unanimous agreement
        if len(direction_votes) == 1:
            direction = list(direction_votes.keys())[0]
            logger.debug(f"Unanimous agreement on direction: {direction}")
            return direction, False, None
        
        # Check for majority (more than half)
        max_votes = max(len(votes) for votes in direction_votes.values())
        majority_directions = [
            direction for direction, votes in direction_votes.items()
            if len(votes) == max_votes
        ]
        
        if len(majority_directions) == 1 and max_votes > len(predictions) / 2:
            direction = majority_directions[0]
            logger.debug(f"Majority vote for direction: {direction}")
            return direction, False, None
        
        # Conflict detected - resolve using confidence
        conflict_resolved = True
        
        # If multiple directions tied for most votes, or no clear majority
        if len(majority_directions) > 1 or max_votes <= len(predictions) / 2:
            # Choose direction with highest confidence
            max_confidence = -1.0
            best_direction = "hold"
            best_timeframe = None
            
            for timeframe, pred in predictions.items():
                if pred.confidence > max_confidence:
                    max_confidence = pred.confidence
                    best_direction = pred.direction
                    best_timeframe = timeframe
                elif pred.confidence == max_confidence:
                    # If confidences equal, prefer longer timeframe
                    current_priority = self._get_timeframe_priority(best_timeframe)
                    new_priority = self._get_timeframe_priority(timeframe)
                    if new_priority > current_priority:
                        best_direction = pred.direction
                        best_timeframe = timeframe
            
            resolution_method = (
                "highest_confidence" if max_confidence > 0
                else "default_longer_timeframe"
            )
            
            # Log conflict resolution to audit logs
            self._log_conflict_resolution(
                predictions=predictions,
                final_direction=best_direction,
                resolution_method=resolution_method,
                chosen_timeframe=best_timeframe
            )
            
            logger.info(
                f"Conflict resolved: chose {best_direction} from {best_timeframe} "
                f"using {resolution_method}"
            )
            
            return best_direction, conflict_resolved, resolution_method
        
        # Fallback (shouldn't reach here)
        return "hold", True, "fallback"
    
    def _get_timeframe_priority(self, timeframe: Optional[str]) -> int:
        """
        Get priority for timeframe (higher = longer term).
        
        Args:
            timeframe: Timeframe name
            
        Returns:
            Priority value (monthly=3, weekly=2, daily=1, unknown=0)
        """
        priorities = {
            "monthly": 3,
            "weekly": 2,
            "daily": 1
        }
        return priorities.get(timeframe, 0)
    
    def _log_conflict_resolution(
        self,
        predictions: Dict[str, TimeframePrediction],
        final_direction: str,
        resolution_method: str,
        chosen_timeframe: Optional[str]
    ) -> None:
        """
        Log conflict resolution decision to audit logs.

        Args:
            predictions: All timeframe predictions
            final_direction: Final resolved direction
            resolution_method: Method used for resolution
            chosen_timeframe: Timeframe that was chosen

        Requirements: 9.3
        """
        conflict_details = {
            "event_type": "ensemble_conflict_resolution",
            "timestamp": datetime.utcnow().isoformat(),
            "predictions": {
                tf: {
                    "direction": pred.direction,
                    "confidence": pred.confidence
                }
                for tf, pred in predictions.items()
            },
            "final_direction": final_direction,
            "resolution_method": resolution_method,
            "chosen_timeframe": chosen_timeframe,
            "weights": self.timeframe_weights
        }

        # Always log to application logs for audit trail
        logger.info(
            f"AUDIT: Ensemble conflict resolution - "
            f"method={resolution_method}, direction={final_direction}, "
            f"timeframe={chosen_timeframe}, predictions={conflict_details['predictions']}"
        )

        # If audit logger is provided, queue the event for async processing
        if self.audit_logger:
            try:
                # If audit logger has a sync log method, use it
                if hasattr(self.audit_logger, 'log_event_sync'):
                    self.audit_logger.log_event_sync(
                        event_type="ensemble_conflict_resolution",
                        details=conflict_details
                    )
                else:
                    # Otherwise just rely on application logs
                    logger.debug("Audit logger doesn't support sync logging")

            except Exception as e:
                logger.error(f"Failed to log conflict resolution: {e}", exc_info=True)

