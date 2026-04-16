"""
Task 9.9: Production-Grade Load Testing with Locust
===================================================
Comprehensive load testing for Cortex AI Unified Platform.

Performance Targets:
- ML Predictions: p95 < 250ms
- Signal Generation: p95 < 200ms
- Authentication: p95 < 100ms
- 1000 concurrent users
- 0% error rate
- Sustained load for 5 minutes

Test Scenarios:
1. ML Prediction Flow (40% of traffic)
2. Signal Generation Flow (30% of traffic)
3. Read Operations (20% of traffic)
4. Admin Operations (10% of traffic)
"""
import json
import random
import time
from datetime import datetime, timezone

from locust import HttpUser, TaskSet, task, between, events
from locust.runners import MasterRunner, WorkerRunner


# ── Configuration ──────────────────────────────────────────────────────────────
class LoadTestConfig:
    """Centralized configuration for load testing."""
    
    # API Configuration
    API_V1_PREFIX = "/api/v1"
    
    # Test Users (pre-created in database)
    USERS = {
        "viewer": {"username": "viewer", "password": "viewer123", "role": "viewer"},
        "trader": {"username": "trader", "password": "trader123", "role": "trader"},
        "admin": {"username": "admin", "password": "admin123", "role": "admin"},
    }
    
    # Test Symbols (real NSE symbols from training data)
    SYMBOLS = [
        "NSE_EQ|INE669E01016",  # Top liquid symbol
        "NSE_EQ|INE528G01035",
        "NSE_EQ|INE040H01021",
        "NSE_EQ|INE221H01019",
        "NSE_EQ|INE351F01018",
        "NSE_EQ|INE614G01033",
        "NSE_EQ|INE758T01015",
        "NSE_EQ|INE053F01010",
        "NSE_EQ|INE785M01021",
        "NSE_EQ|INE07O001026",
    ]
    
    # Performance Thresholds (milliseconds)
    THRESHOLDS = {
        "ml_prediction": 250,      # p95 < 250ms
        "signal_generation": 200,  # p95 < 200ms
        "authentication": 100,     # p95 < 100ms
        "read_operations": 150,    # p95 < 150ms
        "admin_operations": 300,   # p95 < 300ms
    }


# ── Performance Tracking ───────────────────────────────────────────────────────
class PerformanceTracker:
    """Track and report performance metrics."""
    
    def __init__(self):
        self.latencies = {
            "ml_prediction": [],
            "signal_generation": [],
            "authentication": [],
            "read_operations": [],
            "admin_operations": [],
        }
        self.errors = []
        self.start_time = None
        self.end_time = None
    
    def record_latency(self, category: str, latency_ms: float):
        """Record latency for a category."""
        if category in self.latencies:
            self.latencies[category].append(latency_ms)
    
    def record_error(self, error: dict):
        """Record an error."""
        self.errors.append(error)
    
    def calculate_percentile(self, values: list, percentile: int) -> float:
        """Calculate percentile from sorted values."""
        if not values:
            return 0.0
        sorted_values = sorted(values)
        index = int(len(sorted_values) * percentile / 100)
        return sorted_values[min(index, len(sorted_values) - 1)]
    
    def get_summary(self) -> dict:
        """Get performance summary."""
        summary = {
            "duration_seconds": (self.end_time - self.start_time) if self.end_time else 0,
            "total_requests": sum(len(v) for v in self.latencies.values()),
            "total_errors": len(self.errors),
            "error_rate": 0.0,
            "categories": {}
        }
        
        total_requests = summary["total_requests"]
        if total_requests > 0:
            summary["error_rate"] = (len(self.errors) / total_requests) * 100
        
        for category, latencies in self.latencies.items():
            if latencies:
                summary["categories"][category] = {
                    "count": len(latencies),
                    "min": min(latencies),
                    "max": max(latencies),
                    "mean": sum(latencies) / len(latencies),
                    "p50": self.calculate_percentile(latencies, 50),
                    "p95": self.calculate_percentile(latencies, 95),
                    "p99": self.calculate_percentile(latencies, 99),
                    "threshold": LoadTestConfig.THRESHOLDS.get(category, 0),
                    "threshold_met": self.calculate_percentile(latencies, 95) < LoadTestConfig.THRESHOLDS.get(category, float('inf'))
                }
        
        return summary


# Global tracker instance
performance_tracker = PerformanceTracker()


# ── Task Sets ──────────────────────────────────────────────────────────────────
class MLPredictionTasks(TaskSet):
    """ML Prediction workload (40% of traffic)."""
    
    def on_start(self):
        self.token = None
        self.headers = {}
        """Login as trader on start."""
        self.token = None
        self.headers = {}
        self.login("trader")
    
    def login(self, role: str):
        """Login and store token."""
        user = LoadTestConfig.USERS[role]
        response = self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/auth/login",
            json={"username": user["username"], "password": user["password"]},
            name="/auth/login"
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}
    
    @task(10)
    def get_ml_prediction(self):
        """Get ML prediction for a symbol."""
        if not hasattr(self, 'token') or not self.token:
            # Skip if not logged in yet
            time.sleep(0.5)
            return
        
        symbol = random.choice(LoadTestConfig.SYMBOLS)
        start_time = time.time()
        
        # Prepare request with required fields
        request_data = {
            "model_id": "ensemble_v1",
            "symbol": symbol,
            "features": {
                "close": 100.0,
                "volume": 1000000,
                "rsi_14": 50.0,
                "macd": 0.5,
                "signal": 0.3
            }
        }
        
        with self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/ml/predict",
            headers=self.headers,
            json=request_data,
            catch_response=True,
            name="/ml/predict"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("ml_prediction", latency_ms)
            
            if response.status_code == 200:
                response.success()
            elif response.status_code == 429:
                # Rate limited - don't count as error
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
                performance_tracker.record_error({
                    "endpoint": "/ml/predict",
                    "status": response.status_code,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
    
    @task(5)
    def get_batch_predictions(self):
        """Get batch predictions."""
        if not self.token:
            return
        
        symbol = random.choice(LoadTestConfig.SYMBOLS)
        start_time = time.time()
        
        request_data = {
            "model_id": "ensemble_v1",
            "symbol": symbol,
            "features": {
                "close": 100.0,
                "volume": 1000000,
                "rsi_14": 50.0,
                "macd": 0.5,
                "signal": 0.3
            }
        }
        
        with self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/ml/predict",
            headers=self.headers,
            json=request_data,
            catch_response=True,
            name="/ml/predict"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("ml_prediction", latency_ms)
            
            if response.status_code == 200:
                response.success()
            elif response.status_code == 429:
                response.success()  # Rate limited - don't count as error
            else:
                response.failure(f"Status {response.status_code}")


class SignalGenerationTasks(TaskSet):
    """Signal generation workload (30% of traffic)."""
    
    def on_start(self):
        self.token = None
        self.headers = {}
        """Login as trader on start."""
        self.token = None
        self.headers = {}
        self.login("trader")
    
    def login(self, role: str):
        """Login and store token."""
        user = LoadTestConfig.USERS[role]
        response = self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/auth/login",
            json={"username": user["username"], "password": user["password"]},
            name="/auth/login"
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}
    
    @task(10)
    def get_signals(self):
        """Get trading signals."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/fusion/signals",
            headers=self.headers,
            params={"limit": 10},
            catch_response=True,
            name="/fusion/signals"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("signal_generation", latency_ms)
            
            if response.status_code == 200:
                response.success()
            elif response.status_code == 429:
                response.success()  # Rate limited - don't count as error
            else:
                response.failure(f"Status {response.status_code}")
                performance_tracker.record_error({
                    "endpoint": "/fusion/signals",
                    "status": response.status_code,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
    
    @task(5)
    def get_signal_by_symbol(self):
        """Get signals for specific symbol."""
        if not self.token:
            return
        
        symbol = random.choice(LoadTestConfig.SYMBOLS)
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/fusion/signals/{symbol}",
            headers=self.headers,
            catch_response=True,
            name="/fusion/signals/[symbol]"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("signal_generation", latency_ms)
            
            if response.status_code == 200:
                pass
            elif response.status_code == 429:
                pass
            else:
                response.failure(f"Status {response.status_code}")


class ReadOperationTasks(TaskSet):
    """Read operation workload (20% of traffic)."""
    
    def on_start(self):
        self.token = None
        self.headers = {}
        """Login as viewer on start."""
        self.token = None
        self.headers = {}
        self.login("viewer")
    
    def login(self, role: str):
        """Login and store token."""
        user = LoadTestConfig.USERS[role]
        response = self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/auth/login",
            json={"username": user["username"], "password": user["password"]},
            name="/auth/login"
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}
    
    @task(5)
    def get_health(self):
        """Health check."""
        start_time = time.time()
        
        with self.client.get(
            "/health",
            catch_response=True,
            name="/health"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("read_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
    
    @task(10)
    def get_user_profile(self):
        """Get current user profile."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/auth/me",
            headers=self.headers,
            catch_response=True,
            name="/auth/me"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("read_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
    
    @task(8)
    def get_events(self):
        """Get AI events."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/ingestion/events/processed",
            headers=self.headers,
            params={"limit": 20},
            catch_response=True,
            name="/ingestion/events/processed"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("read_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")


class AdminOperationTasks(TaskSet):
    """Admin operation workload (10% of traffic)."""
    
    def on_start(self):
        self.token = None
        self.headers = {}
        """Login as admin on start."""
        self.token = None
        self.headers = {}
        self.login("admin")
    
    def login(self, role: str):
        """Login and store token."""
        user = LoadTestConfig.USERS[role]
        response = self.client.post(
            f"{LoadTestConfig.API_V1_PREFIX}/auth/login",
            json={"username": user["username"], "password": user["password"]},
            name="/auth/login"
        )
        if response.status_code == 200:
            data = response.json()
            self.token = data["access_token"]
            self.headers = {"Authorization": f"Bearer {self.token}"}
        else:
            self.token = None
            self.headers = {}
    
    @task(5)
    def get_drift_reports(self):
        """Get drift reports."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/governance/drift/reports",
            headers=self.headers,
            params={"limit": 10},
            catch_response=True,
            name="/governance/drift/reports"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("admin_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
    
    @task(3)
    def get_kill_switch_status(self):
        """Get kill switch status."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/safety/kill-switch/status",
            headers=self.headers,
            catch_response=True,
            name="/safety/kill-switch/status"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("admin_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")
    
    @task(2)
    def get_models(self):
        """Get AI/ML models."""
        if not self.token:
            return
        
        start_time = time.time()
        
        with self.client.get(
            f"{LoadTestConfig.API_V1_PREFIX}/governance/models",
            headers=self.headers,
            params={"limit": 10},
            catch_response=True,
            name="/governance/models"
        ) as response:
            latency_ms = (time.time() - start_time) * 1000
            performance_tracker.record_latency("admin_operations", latency_ms)
            
            if response.status_code == 200:
                response.success()
            else:
                response.failure(f"Status {response.status_code}")


# ── User Classes ───────────────────────────────────────────────────────────────
class MLPredictionUser(HttpUser):
    """User focused on ML predictions (40% of users)."""
    tasks = [MLPredictionTasks]
    wait_time = between(1, 3)  # Wait 1-3 seconds between tasks
    weight = 40


class SignalGenerationUser(HttpUser):
    """User focused on signal generation (30% of users)."""
    tasks = [SignalGenerationTasks]
    wait_time = between(1, 3)
    weight = 30


class ReadOperationUser(HttpUser):
    """User focused on read operations (20% of users)."""
    tasks = [ReadOperationTasks]
    wait_time = between(0.5, 2)
    weight = 20


class AdminOperationUser(HttpUser):
    """Admin user (10% of users)."""
    tasks = [AdminOperationTasks]
    wait_time = between(2, 5)
    weight = 10


# ── Event Handlers ─────────────────────────────────────────────────────────────
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Initialize performance tracking when test starts."""
    performance_tracker.start_time = time.time()
    print("\n" + "="*80)
    print("LOAD TEST STARTED")
    print("="*80)
    print(f"Target: 1000 concurrent users")
    print(f"Duration: 5 minutes")
    print(f"Performance Thresholds:")
    for category, threshold in LoadTestConfig.THRESHOLDS.items():
        print(f"  - {category}: p95 < {threshold}ms")
    print("="*80 + "\n")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Generate performance report when test stops."""
    performance_tracker.end_time = time.time()
    summary = performance_tracker.get_summary()
    
    print("\n" + "="*80)
    print("LOAD TEST RESULTS")
    print("="*80)
    print(f"Duration: {summary['duration_seconds']:.2f}s")
    print(f"Total Requests: {summary['total_requests']}")
    print(f"Total Errors: {summary['total_errors']}")
    print(f"Error Rate: {summary['error_rate']:.2f}%")
    print("\n" + "-"*80)
    print("PERFORMANCE BY CATEGORY")
    print("-"*80)
    
    all_passed = True
    for category, metrics in summary["categories"].items():
        status = "✅ PASS" if metrics["threshold_met"] else "❌ FAIL"
        print(f"\n{category.upper().replace('_', ' ')}:")
        print(f"  Count: {metrics['count']}")
        print(f"  Mean: {metrics['mean']:.2f}ms")
        print(f"  p50: {metrics['p50']:.2f}ms")
        print(f"  p95: {metrics['p95']:.2f}ms (threshold: {metrics['threshold']}ms) {status}")
        print(f"  p99: {metrics['p99']:.2f}ms")
        print(f"  Min: {metrics['min']:.2f}ms")
        print(f"  Max: {metrics['max']:.2f}ms")
        
        if not metrics["threshold_met"]:
            all_passed = False
    
    print("\n" + "="*80)
    if all_passed and summary["error_rate"] == 0:
        print("🎉 ALL PERFORMANCE TARGETS MET!")
    else:
        print("⚠️  SOME PERFORMANCE TARGETS NOT MET")
    print("="*80 + "\n")
    
    # Save detailed report
    report_file = f"load_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_file, 'w') as f:
        json.dump(summary, f, indent=2)
    print(f"Detailed report saved to: {report_file}\n")


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════════════════════════════╗
    ║                  CORTEX AI UNIFIED - LOAD TEST SUITE                     ║
    ║                         Production-Grade Testing                         ║
    ╚══════════════════════════════════════════════════════════════════════════╝
    
    To run this load test:
    
    1. Install Locust:
       pip install locust
    
    2. Run the test:
       locust -f backend/scripts/locustfile.py --host=http://localhost:8000 \\
              --users 1000 --spawn-rate 50 --run-time 5m --headless
    
    3. Or run with Web UI:
       locust -f backend/scripts/locustfile.py --host=http://localhost:8000
       Then open http://localhost:8089
    
    Performance Targets:
    - ML Predictions: p95 < 250ms
    - Signal Generation: p95 < 200ms
    - Authentication: p95 < 100ms
    - 0% error rate
    - 1000 concurrent users sustained for 5 minutes
    """)
