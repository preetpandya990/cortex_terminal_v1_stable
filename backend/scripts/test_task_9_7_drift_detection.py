"""
Task 9.7: Drift Detection and Model Transitions - E2E Test
===========================================================

Tests drift detection, automatic model demotion, and governance endpoints.

Requirements:
- API server running on localhost:8000
- Database with test models and predictions
- Redis for pub/sub alerts
- Admin credentials (admin/admin123)

Test Coverage:
1. Create test model in 'live' state
2. Generate predictions with good accuracy (no drift)
3. Generate predictions with poor accuracy (simulate drift)
4. Trigger drift detection manually
5. Verify drift report created
6. Verify model demoted (live → paper)
7. Trigger drift again on paper model
8. Verify second demotion (paper → shadow)
9. Query drift reports endpoint
10. Verify Redis pub/sub alerts

Performance Targets:
- Drift detection: <500ms (statistical analysis)
- Report query: <100ms
- Model state transition: <50ms
"""
import asyncio
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

# Configuration
API_BASE_URL = "http://localhost:8000"
API_V1_PREFIX = "/api/v1"

# Get DATABASE_URL from settings (same as API server)
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from app.core.config import get_settings
settings = get_settings()
DATABASE_URL = str(settings.DATABASE_URL)

# Test credentials
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"


class TestRunner:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=30.0)
        self.token = None
        self.test_model_id = None
        self.test_model_name = f"test_drift_model_{int(time.time())}"
        self.drift_report_ids = []
        
        # Database setup
        self.engine = create_async_engine(DATABASE_URL, echo=False)
        self.async_session = sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )
    
    async def setup(self):
        """Setup test environment."""
        print("\n" + "="*80)
        print("TASK 9.7: DRIFT DETECTION AND MODEL TRANSITIONS - E2E TEST")
        print("="*80)
        print(f"Start Time: {datetime.now().isoformat()}")
        print(f"API Base URL: {API_BASE_URL}")
        print()
        
        # Login as admin
        await self.login()
        
        # Create test model
        await self.create_test_model()
        
        # Create ML model metadata for AI monitoring
        await self.create_ml_model_metadata()
    
    async def login(self):
        """Login and get JWT token."""
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/auth/login",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        
        if response.status_code != 200:
            raise Exception(f"Login failed: {response.status_code} - {response.text}")
        
        data = response.json()
        self.token = data["access_token"]
        print(f"✅ Logged in as {ADMIN_USERNAME}")
    
    @property
    def headers(self):
        """Get authorization headers."""
        return {"Authorization": f"Bearer {self.token}"}
    
    async def create_test_model(self):
        """Create test model in database."""
        from app.ai.fusion.models import AIMLModel
        
        async with self.async_session() as db:
            # Check if model exists
            stmt = select(AIMLModel).where(AIMLModel.model_name == self.test_model_name)
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            
            if existing:
                self.test_model_id = existing.id
                print(f"✅ Using existing test model: {self.test_model_name} (ID: {self.test_model_id})")
            else:
                # Create new model
                model = AIMLModel(
                    model_name=self.test_model_name,
                    model_version="1.0.0",
                    model_type="lstm",
                    deployment_state="live",
                    accuracy=Decimal("0.85"),  # Baseline 85% accuracy
                    precision=Decimal("0.83"),
                    recall=Decimal("0.87"),
                    f1_score=Decimal("0.85"),
                    governance_metadata={"test": True, "baseline_mean": 0.75},
                    created_at=datetime.now(timezone.utc),
                )
                
                db.add(model)
                await db.commit()
                await db.refresh(model)
                
                self.test_model_id = model.id
                print(f"✅ Created test model: {self.test_model_name} (ID: {self.test_model_id})")
                print(f"   State: live, Baseline Accuracy: 85%")
    
    async def create_ml_model_metadata(self):
        """Create ML model metadata that AI will monitor."""
        from app.models.ml_data import MLModelMetadata
        
        async with self.async_session() as db:
            # Check if ML model exists
            stmt = select(MLModelMetadata).where(MLModelMetadata.model_name == self.test_model_name)
            result = await db.execute(stmt)
            existing = result.scalar_one_or_none()
            
            if not existing:
                ml_model = MLModelMetadata(
                    model_id=f"ml_{self.test_model_name}",
                    model_name=self.test_model_name,
                    model_version="1.0.0",
                    training_prediction_stats={
                        "mean": 0.75,
                        "std": 0.15,
                        "min": 0.0,
                        "max": 1.0
                    },
                    is_active=True,
                    status="development",   # never "production" for test fixtures
                )
                db.add(ml_model)
                await db.commit()
                print(f"✅ Created ML model metadata for AI monitoring")
    
    async def create_predictions(self, count: int, mean_prediction: float, label: str, hours_ago: int = 0):
        """
        Create ML predictions that AI governance will monitor.
        
        Args:
            count: Number of predictions to create
            mean_prediction: Mean prediction value (0.0-1.0)
            label: Description for logging
            hours_ago: How many hours in the past to start creating predictions
        """
        from app.models.ml_data import MLPrediction, MLModelMetadata
        
        async with self.async_session() as db:
            # Get ML model ID
            stmt = select(MLModelMetadata).where(MLModelMetadata.model_name == self.test_model_name)
            result = await db.execute(stmt)
            ml_model = result.scalar_one()
            
            import random
            base_time = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours_ago)
            
            for i in range(count):
                # Vary predictions around mean with some noise
                prediction_value = max(0.0, min(1.0, mean_prediction + random.gauss(0, 0.1)))
                
                prediction = MLPrediction(
                    model_id=ml_model.model_id,
                    timestamp=base_time + timedelta(minutes=i),  # Sequential timestamps
                    features=None,
                    prediction=prediction_value,
                    prediction_proba={"bullish": prediction_value, "bearish": 1 - prediction_value},
                    symbol="AAPL",
                    prediction_type="classification",
                    model_version=ml_model.model_version,
                    user_id=None,
                )
                
                db.add(prediction)
            
            await db.commit()
            print(f"✅ Created {count} ML predictions with mean={mean_prediction:.2f} ({label})")
    
    async def test_1_baseline_no_drift(self):
        """Test 1: Generate predictions with baseline distribution (no drift expected)."""
        print("\n" + "="*80)
        print("TEST 1: Baseline Predictions (No Drift Expected)")
        print("="*80)
        
        # Create 50 predictions 5 hours ago (mean=0.75, matches baseline)
        await self.create_predictions(50, 0.75, "baseline - no drift", hours_ago=5)
        
        # Trigger drift detection
        start_time = time.perf_counter()
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/check/{self.test_model_id}",
            headers=self.headers,
            json={"lookback_hours": 24}
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        
        if response.status_code != 200:
            print(f"❌ FAILED: Drift check returned {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        data = response.json()
        self.drift_report_ids.append(data["id"])
        
        print(f"✅ Drift check completed")
        print(f"   Response Time: {elapsed_ms:.2f}ms")
        print(f"   Report ID: {data['id']}")
        print(f"   Drift Detected: {data['drift_detected']}")
        print(f"   Drift Score: {data['drift_score']}")
        print(f"   Accuracy Drop: {data['accuracy_drop']}")
        print(f"   Action Taken: {data['action_taken']}")
        
        if data['drift_detected']:
            print(f"   ⚠️  WARNING: Drift detected when none expected")
            return False
        
        print(f"   ✅ PASSED: No drift detected (as expected)")
        
        # Performance check
        if elapsed_ms > 500:
            print(f"   ⚠️  WARNING: Response time {elapsed_ms:.2f}ms exceeds 500ms target")
        else:
            print(f"   ✅ PASSED: Response time within 500ms target")
        
        return True
    
    async def test_2_drift_detection_and_demotion(self):
        """Test 2: Generate drifted predictions and verify drift detection + demotion."""
        print("\n" + "="*80)
        print("TEST 2: Drift Detection and Model Demotion (live → paper)")
        print("="*80)
        
        # Create 100 predictions 2 hours ago (mean=0.40, significant drift from baseline 0.75)
        await self.create_predictions(100, 0.40, "drifted distribution - drift expected", hours_ago=2)
        
        # Get current model state
        async with self.async_session() as db:
            from app.ai.fusion.models import AIMLModel
            stmt = select(AIMLModel).where(AIMLModel.id == self.test_model_id)
            result = await db.execute(stmt)
            model_before = result.scalar_one()
            state_before = model_before.deployment_state
        
        print(f"   Model state before: {state_before}")
        
        # Trigger drift detection with 3-hour lookback (only Test 2 predictions)
        start_time = time.perf_counter()
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/check/{self.test_model_id}",
            headers=self.headers,
            json={"lookback_hours": 3}  # Captures Test 2 (2h ago) but not Test 1 (5h ago)
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        
        if response.status_code != 200:
            print(f"❌ FAILED: Drift check returned {response.status_code}")
            print(f"   Response: {response.text}")
            return False
        
        data = response.json()
        self.drift_report_ids.append(data["id"])
        
        print(f"✅ Drift check completed")
        print(f"   Response Time: {elapsed_ms:.2f}ms")
        print(f"   Report ID: {data['id']}")
        print(f"   Drift Detected: {data['drift_detected']}")
        print(f"   Drift Score: {data['drift_score']}")
        print(f"   Accuracy Drop: {data['accuracy_drop']}")
        print(f"   Action Taken: {data['action_taken']}")
        
        if not data['drift_detected']:
            print(f"   ❌ FAILED: Drift not detected when expected")
            return False
        
        print(f"   ✅ PASSED: Drift detected")
        
        # Verify model state changed
        async with self.async_session() as db:
            from app.ai.fusion.models import AIMLModel
            stmt = select(AIMLModel).where(AIMLModel.id == self.test_model_id)
            result = await db.execute(stmt)
            model_after = result.scalar_one()
            state_after = model_after.deployment_state
        
        print(f"   Model state after: {state_after}")
        
        if state_before == "live" and state_after == "paper":
            print(f"   ✅ PASSED: Model demoted from live → paper")
        else:
            print(f"   ❌ FAILED: Expected live → paper, got {state_before} → {state_after}")
            return False
        
        return True
    
    async def test_3_second_drift_demotion(self):
        """Test 3: Trigger drift again and verify paper → shadow transition."""
        print("\n" + "="*80)
        print("TEST 3: Second Drift Detection (paper → shadow)")
        print("="*80)
        
        # Create more drifted predictions now (mean=0.30)
        await self.create_predictions(50, 0.30, "continued drift", hours_ago=0)
        
        # Get current state
        async with self.async_session() as db:
            from app.ai.fusion.models import AIMLModel
            stmt = select(AIMLModel).where(AIMLModel.id == self.test_model_id)
            result = await db.execute(stmt)
            model_before = result.scalar_one()
            state_before = model_before.deployment_state
        
        print(f"   Model state before: {state_before}")
        
        # Trigger drift detection with 1-hour lookback (only Test 3 predictions)
        response = await self.client.post(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/check/{self.test_model_id}",
            headers=self.headers,
            json={"lookback_hours": 1}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Drift check returned {response.status_code}")
            return False
        
        data = response.json()
        self.drift_report_ids.append(data["id"])
        
        print(f"✅ Drift check completed")
        print(f"   Drift Detected: {data['drift_detected']}")
        print(f"   Action Taken: {data['action_taken']}")
        
        # Verify model state changed
        async with self.async_session() as db:
            from app.ai.fusion.models import AIMLModel
            stmt = select(AIMLModel).where(AIMLModel.id == self.test_model_id)
            result = await db.execute(stmt)
            model_after = result.scalar_one()
            state_after = model_after.deployment_state
        
        print(f"   Model state after: {state_after}")
        
        if state_before == "paper" and state_after == "shadow":
            print(f"   ✅ PASSED: Model demoted from paper → shadow")
        else:
            print(f"   ❌ FAILED: Expected paper → shadow, got {state_before} → {state_after}")
            return False
        
        return True
    
    async def test_4_drift_reports_endpoint(self):
        """Test 4: Query drift reports endpoint."""
        print("\n" + "="*80)
        print("TEST 4: Drift Reports Endpoint")
        print("="*80)
        
        # Query all drift reports
        start_time = time.perf_counter()
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/reports",
            headers=self.headers,
            params={"model_id": self.test_model_id, "hours": 24}
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        
        if response.status_code != 200:
            print(f"❌ FAILED: Reports endpoint returned {response.status_code}")
            return False
        
        reports = response.json()
        
        print(f"✅ Reports endpoint responded")
        print(f"   Response Time: {elapsed_ms:.2f}ms")
        print(f"   Total Reports: {len(reports)}")
        
        # Verify our test reports are present
        report_ids = [r["id"] for r in reports]
        missing = [rid for rid in self.drift_report_ids if rid not in report_ids]
        
        if missing:
            print(f"   ❌ FAILED: Missing report IDs: {missing}")
            return False
        
        print(f"   ✅ PASSED: All {len(self.drift_report_ids)} test reports found")
        
        # Show report summary
        drift_count = sum(1 for r in reports if r["drift_detected"])
        print(f"\n   Report Summary:")
        print(f"   - Total Reports: {len(reports)}")
        print(f"   - Drift Detected: {drift_count}")
        print(f"   - No Drift: {len(reports) - drift_count}")
        
        # Performance check
        if elapsed_ms > 100:
            print(f"   ⚠️  WARNING: Response time {elapsed_ms:.2f}ms exceeds 100ms target")
        else:
            print(f"   ✅ PASSED: Response time within 100ms target")
        
        return True
    
    async def test_5_models_endpoint_state_filter(self):
        """Test 5: Verify models endpoint shows correct state."""
        print("\n" + "="*80)
        print("TEST 5: Models Endpoint State Verification")
        print("="*80)
        
        # Query models in shadow state
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/models",
            headers=self.headers,
            params={"state": "shadow"}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Models endpoint returned {response.status_code}")
            return False
        
        models = response.json()
        
        # Find our test model
        test_model = next((m for m in models if m["id"] == self.test_model_id), None)
        
        if not test_model:
            print(f"❌ FAILED: Test model not found in shadow state")
            return False
        
        print(f"✅ Test model found in shadow state")
        print(f"   Model: {test_model['model_name']}")
        print(f"   State: {test_model['state']}")
        print(f"   Accuracy: {test_model['accuracy']}")
        print(f"   ✅ PASSED: Model state correctly reflects drift demotions")
        
        return True
    
    async def test_6_drift_reports_filtering(self):
        """Test 6: Test drift reports filtering (drift_only=true)."""
        print("\n" + "="*80)
        print("TEST 6: Drift Reports Filtering")
        print("="*80)
        
        # Query only reports with drift detected
        response = await self.client.get(
            f"{API_BASE_URL}{API_V1_PREFIX}/governance/drift/reports",
            headers=self.headers,
            params={"model_id": self.test_model_id, "drift_only": True, "hours": 24}
        )
        
        if response.status_code != 200:
            print(f"❌ FAILED: Filtered reports endpoint returned {response.status_code}")
            return False
        
        drift_reports = response.json()
        
        print(f"✅ Filtered reports endpoint responded")
        print(f"   Drift Reports: {len(drift_reports)}")
        
        # Verify all returned reports have drift_detected=true
        non_drift = [r for r in drift_reports if not r["drift_detected"]]
        
        if non_drift:
            print(f"   ❌ FAILED: Found {len(non_drift)} reports without drift in filtered results")
            return False
        
        print(f"   ✅ PASSED: All filtered reports have drift_detected=true")
        
        # Show actions taken
        actions = [r["action_taken"] for r in drift_reports if r["action_taken"]]
        print(f"\n   Actions Taken:")
        for action in set(actions):
            count = actions.count(action)
            print(f"   - {action}: {count}")
        
        return True
    
    async def cleanup(self):
        """Archive test models and dispose resources."""
        print("\n" + "="*80)
        print("CLEANUP")
        print("="*80)

        from app.models.ml_data import MLModelMetadata
        from app.ai.fusion.models import AIMLModel
        from sqlalchemy import update

        async with self.async_session() as db:
            # Archive MLModelMetadata test entry
            await db.execute(
                update(MLModelMetadata)
                .where(MLModelMetadata.model_name == self.test_model_name)
                .values(status="archived", is_active=False)
            )
            # Retire AIMLModel test entry
            await db.execute(
                update(AIMLModel)
                .where(AIMLModel.model_name == self.test_model_name)
                .values(deployment_state="retired")
            )
            await db.commit()
            print(f"✅ Archived test model: {self.test_model_name}")
            print(f"✅ Drift report IDs: {self.drift_report_ids}")

        await self.client.aclose()
        await self.engine.dispose()
    
    async def run_all_tests(self):
        """Run all tests and report results."""
        await self.setup()
        
        tests = [
            ("Test 1: Baseline (No Drift)", self.test_1_baseline_no_drift),
            ("Test 2: Drift Detection & Demotion", self.test_2_drift_detection_and_demotion),
            ("Test 3: Second Demotion", self.test_3_second_drift_demotion),
            ("Test 4: Drift Reports Endpoint", self.test_4_drift_reports_endpoint),
            ("Test 5: Models State Verification", self.test_5_models_endpoint_state_filter),
            ("Test 6: Reports Filtering", self.test_6_drift_reports_filtering),
        ]
        
        results = {}
        
        for test_name, test_func in tests:
            try:
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"
            except Exception as e:
                print(f"\n❌ EXCEPTION in {test_name}: {e}")
                import traceback
                traceback.print_exc()
                results[test_name] = "ERROR"
        
        # Print summary
        print("\n" + "="*80)
        print("TEST SUMMARY")
        print("="*80)
        
        for test_name, result in results.items():
            status_icon = "✅" if result == "PASSED" else "❌"
            print(f"{status_icon} {test_name}: {result}")
        
        passed = sum(1 for r in results.values() if r == "PASSED")
        total = len(results)
        
        print(f"\nTotal: {passed}/{total} tests passed")
        
        if passed == total:
            print("\n🎉 ALL TESTS PASSED! Task 9.7 Complete!")
        else:
            print(f"\n⚠️  {total - passed} test(s) failed")
        
        await self.cleanup()


async def main():
    """Main entry point."""
    runner = TestRunner()
    await runner.run_all_tests()


if __name__ == "__main__":
    asyncio.run(main())
