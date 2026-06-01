"""
Integration Tests for Drift Monitoring System

Verifies complete drift detection pipeline:
- Background task configuration
- Drift detector functionality
- Alert system
- Database integration
- Redis pub/sub
"""
import asyncio
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fusion.models import AIDriftReport, AIMLModel
from app.ai.governance.drift_detector import DriftDetector
from app.core.config import get_settings
from app.core.redis import PubSubClient, RedisChannels, init_redis, close_redis, get_redis
from app.models.ml_data import MLModelMetadata, MLPrediction


settings = get_settings()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_monitoring_configuration():
    """Verify drift monitoring configuration is production-ready."""
    # Check critical settings
    assert settings.ENABLE_BACKGROUND_TASKS is True, "Background tasks must be enabled"
    assert settings.DRIFT_CHECK_INTERVAL_SECONDS >= 60, "Drift check interval too frequent"
    assert settings.DRIFT_CHECK_INTERVAL_SECONDS <= 3600, "Drift check interval too infrequent"
    assert settings.ML_DRIFT_THRESHOLD_SIGMA >= 1.0, "Drift threshold too sensitive"
    assert settings.ML_DRIFT_THRESHOLD_SIGMA <= 5.0, "Drift threshold too lenient"
    
    # Verify reasonable defaults
    assert settings.DRIFT_CHECK_INTERVAL_SECONDS == 300, "Expected 5-minute check interval"
    assert settings.ML_DRIFT_THRESHOLD_SIGMA == 2.0, "Expected 2-sigma threshold (95% confidence)"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_detector_with_baseline_statistics(db_session: AsyncSession):
    """Test drift detector uses baseline statistics correctly."""
    await init_redis()
    
    try:
        # Get production model with baseline statistics
        stmt = select(MLModelMetadata).where(
            MLModelMetadata.status == 'production',
            MLModelMetadata.is_active == True,
            MLModelMetadata.model_name == 'xgboost'
        ).limit(1)
        
        result = await db_session.execute(stmt)
        model = result.scalar_one_or_none()
        
        if not model:
            pytest.skip("No production XGBoost model found")
        
        # Verify baseline statistics exist
        assert model.training_prediction_stats is not None, "Baseline statistics missing"
        assert 'raw_predictions' in model.training_prediction_stats
        assert 'mean' in model.training_prediction_stats['raw_predictions']
        assert 'std' in model.training_prediction_stats['raw_predictions']
        
        baseline_mean = float(model.training_prediction_stats['raw_predictions']['mean'])
        baseline_std = float(model.training_prediction_stats['raw_predictions']['std'])
        
        assert 0.0 <= baseline_mean <= 1.0, "Baseline mean out of range"
        assert baseline_std > 0, "Baseline std must be positive"
        
        print(f"✓ Baseline statistics: mean={baseline_mean:.4f}, std={baseline_std:.4f}")
        
    finally:
        await close_redis()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_detector_no_drift_scenario(db_session: AsyncSession):
    """Test drift detector when predictions are within baseline."""
    await init_redis()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    try:
        # Create test AI model
        ai_model = AIMLModel(
            model_name="test_drift_no_drift",
            model_type="xgboost",
            deployment_state="live",
            accuracy=Decimal("0.65"),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(ai_model)
        await db_session.commit()
        await db_session.refresh(ai_model)
        
        # Create corresponding ML model with baseline
        ml_model = MLModelMetadata(
            model_id=f"ml_{ai_model.model_name}",
            model_name=ai_model.model_name,
            model_version="1.0.0",
            status="production",
            is_active=True,
            training_prediction_stats={
                'mean': 0.65,
                'std': 0.15,
                'min': 0.0,
                'max': 1.0,
            }
        )
        db_session.add(ml_model)
        await db_session.commit()
        
        # Create predictions within baseline (no drift)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for i in range(50):
            prediction = MLPrediction(
                model_id=ml_model.model_id,
                timestamp=cutoff + timedelta(minutes=i*10),
                prediction=0.65 + (i % 10 - 5) * 0.01,  # Small variations around mean
                prediction_proba={'confidence': 0.65},
                symbol="TEST_SYMBOL",
                prediction_type="classification",
                model_version="1.0.0",
            )
            db_session.add(prediction)
        
        await db_session.commit()
        
        # Run drift detection
        detector = DriftDetector()
        report = await detector.check_drift(
            db=db_session,
            pubsub=pubsub,
            model_id=ai_model.id,
            lookback_hours=24,
        )
        
        # Verify no drift detected
        assert report is not None
        assert report.drift_detected is False, "Should not detect drift for stable predictions"
        assert report.drift_score < Decimal(str(settings.ML_DRIFT_THRESHOLD_SIGMA))
        
        print(f"✓ No drift detected: score={float(report.drift_score):.4f}")
        
    finally:
        await close_redis()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_detector_drift_scenario(db: AsyncSession):
    """Test drift detector when predictions drift from baseline."""
    await init_redis()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    try:
        # Create test AI model
        ai_model = AIMLModel(
            model_name="test_drift_with_drift",
            model_type="xgboost",
            deployment_state="live",
            accuracy=Decimal("0.65"),
            created_at=datetime.now(timezone.utc),
        )
        db.add(ai_model)
        await db.commit()
        await db.refresh(ai_model)
        
        # Create corresponding ML model with baseline
        ml_model = MLModelMetadata(
            model_id=f"ml_{ai_model.model_name}",
            model_name=ai_model.model_name,
            model_version="1.0.0",
            status="production",
            is_active=True,
            training_prediction_stats={
                'mean': 0.65,
                'std': 0.10,
                'min': 0.0,
                'max': 1.0,
            }
        )
        db.add(ml_model)
        await db.commit()
        
        # Create predictions with significant drift
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for i in range(50):
            # Predictions shifted significantly from baseline (0.65 → 0.85)
            prediction = MLPrediction(
                model_id=ml_model.model_id,
                timestamp=cutoff + timedelta(minutes=i*10),
                prediction=0.85 + (i % 10 - 5) * 0.01,  # Shifted mean
                prediction_proba={'confidence': 0.85},
                symbol="TEST_SYMBOL",
                prediction_type="classification",
                model_version="1.0.0",
            )
            db.add(prediction)
        
        await db.commit()
        
        # Run drift detection
        detector = DriftDetector()
        report = await detector.check_drift(
            db=db,
            pubsub=pubsub,
            model_id=ai_model.id,
            lookback_hours=24,
        )
        
        # Verify drift detected
        assert report is not None
        assert report.drift_detected is True, "Should detect drift for shifted predictions"
        assert report.drift_score >= Decimal(str(settings.ML_DRIFT_THRESHOLD_SIGMA))
        assert report.action_taken is not None, "Should take action on drift"
        
        # Verify model was demoted
        await db.refresh(ai_model)
        assert ai_model.deployment_state != "live", "Model should be demoted from live"
        
        print(f"✓ Drift detected: score={float(report.drift_score):.4f}, action={report.action_taken}")
        
    finally:
        await close_redis()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_alert_published_to_redis(db: AsyncSession):
    """Test drift alerts are published to Redis pub/sub."""
    await init_redis()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    # Subscribe to drift alerts channel
    alerts_received = []
    
    async def alert_handler(message):
        alerts_received.append(message)
    
    await pubsub.subscribe(RedisChannels.MODELS_DRIFT_ALERTS, alert_handler)
    
    try:
        # Create test model with drift
        ai_model = AIMLModel(
            model_name="test_drift_alert",
            model_type="xgboost",
            deployment_state="live",
            accuracy=Decimal("0.65"),
            created_at=datetime.now(timezone.utc),
        )
        db.add(ai_model)
        await db.commit()
        await db.refresh(ai_model)
        
        ml_model = MLModelMetadata(
            model_id=f"ml_{ai_model.model_name}",
            model_name=ai_model.model_name,
            model_version="1.0.0",
            status="production",
            is_active=True,
            training_prediction_stats={
                'mean': 0.50,
                'std': 0.10,
                'min': 0.0,
                'max': 1.0,
            }
        )
        db.add(ml_model)
        await db.commit()
        
        # Create drifted predictions
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for i in range(30):
            prediction = MLPrediction(
                model_id=ml_model.model_id,
                timestamp=cutoff + timedelta(minutes=i*10),
                prediction=0.90,  # Significant drift
                prediction_proba={'confidence': 0.90},
                symbol="TEST_SYMBOL",
                prediction_type="classification",
                model_version="1.0.0",
            )
            db.add(prediction)
        
        await db.commit()
        
        # Run drift detection
        detector = DriftDetector()
        report = await detector.check_drift(
            db=db,
            pubsub=pubsub,
            model_id=ai_model.id,
            lookback_hours=24,
        )
        
        # Wait for alert to be published
        await asyncio.sleep(0.5)
        
        # Verify alert was published
        assert len(alerts_received) > 0, "Alert should be published to Redis"
        
        alert = alerts_received[0]
        assert 'model_id' in alert
        assert 'drift_score' in alert
        assert 'action' in alert
        
        print(f"✓ Alert published: {alert}")
        
    finally:
        await pubsub.unsubscribe(RedisChannels.MODELS_DRIFT_ALERTS)
        await close_redis()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_drift_monitoring_performance(db: AsyncSession):
    """Test drift detection completes within performance budget."""
    await init_redis()
    redis = await get_redis()
    pubsub = PubSubClient(redis)
    
    try:
        # Get production model
        stmt = select(MLModelMetadata).where(
            MLModelMetadata.status == 'production',
            MLModelMetadata.is_active == True
        ).limit(1)
        
        result = await db.execute(stmt)
        model = result.scalar_one_or_none()
        
        if not model:
            pytest.skip("No production model found")
        
        # Find corresponding AI model
        stmt = select(AIMLModel).where(
            AIMLModel.model_name == model.model_name
        ).limit(1)
        
        result = await db.execute(stmt)
        ai_model = result.scalar_one_or_none()
        
        if not ai_model:
            pytest.skip("No AI model found")
        
        # Measure drift detection performance
        start_time = datetime.now()
        
        detector = DriftDetector()
        report = await detector.check_drift(
            db=db,
            pubsub=pubsub,
            model_id=ai_model.id,
            lookback_hours=24,
        )
        
        elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        
        # Verify performance
        assert elapsed_ms < 5000, f"Drift detection too slow: {elapsed_ms:.0f}ms (target: <5000ms)"
        
        print(f"✓ Drift detection completed in {elapsed_ms:.0f}ms")
        
    finally:
        await close_redis()


@pytest.mark.integration
def test_drift_monitoring_imports():
    """Verify all drift monitoring components can be imported."""
    # Test imports
    from app.ml.monitoring.drift_scheduler import drift_detection_loop, DriftMonitoringScheduler
    from app.ai.governance.drift_detector import DriftDetector
    from app.worker import main as worker_main
    
    assert drift_detection_loop is not None
    assert DriftMonitoringScheduler is not None
    assert DriftDetector is not None
    assert worker_main is not None
    
    print("✓ All drift monitoring components importable")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_background_task_configuration():
    """Verify background task is properly configured in worker."""
    from app.worker import worker_lifespan
    
    # Test lifespan context manager
    async with worker_lifespan() as (session_factory, redis_client, ml_components, upstox_client):
        assert session_factory is not None
        assert redis_client is not None
        assert ml_components is not None
        assert upstox_client is not None
        
        print("✓ Worker lifespan configured correctly")
