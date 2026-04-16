"""
Prometheus Metrics for Cortex AI
=================================
Production-grade metrics collection for observability.

Required Metrics (from requirements.md):
- request_latency_histogram
- prediction_latency_histogram  
- signal_generation_counter
- kill_switch_activation_counter
- drift_detection_gauge
- worker_heartbeat_age_gauge
"""
from prometheus_client import Counter, Histogram, Gauge, Info

# ── Application Info ───────────────────────────────────────────────────────────
app_info = Info('cortex_app', 'Cortex AI application information')

# ── HTTP Request Metrics ───────────────────────────────────────────────────────
http_requests_total = Counter(
    'http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

http_request_duration_seconds = Histogram(
    'http_request_duration_seconds',
    'HTTP request latency in seconds',
    ['method', 'endpoint'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0)
)

http_requests_in_progress = Gauge(
    'http_requests_in_progress',
    'HTTP requests currently being processed',
    ['method', 'endpoint']
)

# ── ML Prediction Metrics ──────────────────────────────────────────────────────
ml_predictions_total = Counter(
    'ml_predictions_total',
    'Total ML predictions made',
    ['model_id', 'symbol', 'status']
)

ml_prediction_duration_seconds = Histogram(
    'ml_prediction_duration_seconds',
    'ML prediction latency in seconds',
    ['model_id'],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
)

ml_prediction_confidence = Histogram(
    'ml_prediction_confidence',
    'ML prediction confidence scores',
    ['model_id'],
    buckets=(0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)
)

# ── Signal Generation Metrics ──────────────────────────────────────────────────
signal_generation_total = Counter(
    'signal_generation_total',
    'Total trading signals generated',
    ['signal_type', 'action']
)

signal_generation_duration_seconds = Histogram(
    'signal_generation_duration_seconds',
    'Signal generation latency in seconds',
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
)

# ── AI Governance Metrics ──────────────────────────────────────────────────────
drift_detection_score = Gauge(
    'drift_detection_score',
    'Current drift detection score for models',
    ['model_id', 'model_name']
)

drift_detections_total = Counter(
    'drift_detections_total',
    'Total drift detections',
    ['model_id', 'action_taken']
)

model_state = Gauge(
    'model_deployment_state',
    'Model deployment state (0=retired, 1=shadow, 2=paper, 3=live)',
    ['model_id', 'model_name']
)

# ── Safety Metrics ─────────────────────────────────────────────────────────────
# Note: Kill switch metrics are defined in app.ai.safety.kill_switch_manager
# They will be automatically included in /metrics endpoint

# ── Worker Metrics ─────────────────────────────────────────────────────────────
worker_heartbeat_age_seconds = Gauge(
    'worker_heartbeat_age_seconds',
    'Age of last worker heartbeat in seconds'
)

worker_loop_iterations_total = Counter(
    'worker_loop_iterations_total',
    'Total worker loop iterations',
    ['loop_name']
)

worker_loop_duration_seconds = Histogram(
    'worker_loop_duration_seconds',
    'Worker loop execution duration',
    ['loop_name'],
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0)
)

# ── Authentication Metrics ─────────────────────────────────────────────────────
auth_attempts_total = Counter(
    'auth_attempts_total',
    'Total authentication attempts',
    ['method', 'status']
)

auth_token_refreshes_total = Counter(
    'auth_token_refreshes_total',
    'Total token refresh attempts',
    ['status']
)

auth_token_reuse_detections_total = Counter(
    'auth_token_reuse_detections_total',
    'Total token reuse detections (security events)'
)

# ── Database Metrics ───────────────────────────────────────────────────────────
db_query_duration_seconds = Histogram(
    'db_query_duration_seconds',
    'Database query duration',
    ['operation'],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
)

db_connections_active = Gauge(
    'db_connections_active',
    'Active database connections'
)

# ── Redis Metrics ──────────────────────────────────────────────────────────────
redis_operations_total = Counter(
    'redis_operations_total',
    'Total Redis operations',
    ['operation', 'status']
)

redis_operation_duration_seconds = Histogram(
    'redis_operation_duration_seconds',
    'Redis operation duration',
    ['operation'],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1)
)

# ── Event Processing Metrics ───────────────────────────────────────────────────
events_processed_total = Counter(
    'events_processed_total',
    'Total events processed',
    ['event_type', 'status']
)

events_processing_duration_seconds = Histogram(
    'events_processing_duration_seconds',
    'Event processing duration',
    ['event_type'],
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0)
)


def init_metrics(app_version: str, environment: str):
    """Initialize application info metrics."""
    app_info.info({
        'version': app_version,
        'environment': environment,
        'name': 'cortex-ai-unified'
    })
