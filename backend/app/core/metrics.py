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
    'Connections currently checked out of the SQLAlchemy pool (in use)',
    ['engine'],  # "api" | "worker" — pre-outage visibility into pool exhaustion
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

# ── Trade Suggestions Metrics ──────────────────────────────────────────────────
suggestions_generated_total = Counter(
    'suggestions_generated_total',
    'Total trade suggestions generated',
    ['direction', 'confidence_level', 'status']
)

consensus_score_distribution = Histogram(
    'consensus_score_distribution',
    'Distribution of consensus scores for trade suggestions',
    buckets=(0, 20, 40, 60, 70, 80, 85, 90, 95, 100)
)

suggestion_expiry_total = Counter(
    'suggestion_expiry_total',
    'Total trade suggestions expired',
    ['direction', 'confidence_level']
)

correlation_latency_seconds = Histogram(
    'correlation_latency_seconds',
    'Latency of correlation engine by pathway and agent',
    ['pathway', 'agent'],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
)

suggestions_active = Gauge(
    'suggestions_active',
    'Number of currently active trade suggestions',
    ['direction', 'confidence_level']
)

# ── API Response Caching Metrics ───────────────────────────────────────────────
api_cache_hits_total = Counter(
    'api_cache_hits_total',
    'Total API cache hits',
    ['endpoint', 'method']
)

api_cache_misses_total = Counter(
    'api_cache_misses_total',
    'Total API cache misses',
    ['endpoint', 'method']
)

api_cache_response_time_seconds = Histogram(
    'api_cache_response_time_seconds',
    'API response time with cache',
    ['endpoint', 'cache_status'],
    buckets=(0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0)
)

api_cache_invalidations_total = Counter(
    'api_cache_invalidations_total',
    'Total cache invalidations',
    ['pattern', 'trigger']
)


# ── ML Feedback Metrics ────────────────────────────────────────────────────────
ml_feedback_computations_total = Counter(
    'ml_feedback_computations_total',
    'Total ML feedback computations by final status',
    ['status'],  # success | failure
)

ml_feedback_retry_attempts_total = Counter(
    'ml_feedback_retry_attempts_total',
    'Total ML feedback computation attempts (including retries)',
    ['attempt'],  # 1 | 2 | 3
)

ml_feedback_computation_duration_seconds = Histogram(
    'ml_feedback_computation_duration_seconds',
    'End-to-end duration of a successful ML feedback computation (seconds)',
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

# ── Regime Detection Metrics ───────────────────────────────────────────────────
regime_detection_runs_total = Counter(
    'regime_detection_runs_total',
    'Total daily regime detection batch runs by status',
    ['status'],  # success | failure
)

regime_detection_duration_seconds = Histogram(
    'regime_detection_duration_seconds',
    'Duration of a full regime detection batch run (seconds)',
    buckets=(1.0, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
)

regime_detection_symbols_total = Counter(
    'regime_detection_symbols_total',
    'Total symbols processed during regime detection by outcome',
    ['status'],  # detected | unchanged | skipped | failed
)


# ── LLM Intelligence Metrics ──────────────────────────────────────────────────
# Per CORTEX_LLM_UPGRADE_PLAN.md §8.4 — SLOs: explanation p95 < 5s, success ≥ 95%,
# NIM fallback < 5%, sentiment p95 < 3s, RAG retrieval p95 < 500ms.

llm_explanation_duration_seconds = Histogram(
    'llm_explanation_duration_seconds',
    'End-to-end explanation generation latency (seconds)',
    ['provider'],   # nim | ollama
    buckets=(0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 20.0, 30.0),
)

llm_explanations_total = Counter(
    'llm_explanations_total',
    'Total LLM explanation generation attempts',
    ['status', 'provider'],  # status: success | failure | skipped
)

llm_sentiment_duration_seconds = Histogram(
    'llm_sentiment_duration_seconds',
    'LLM sentiment scoring latency (seconds)',
    ['provider'],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 10.0),
)

llm_sentiments_total = Counter(
    'llm_sentiments_total',
    'Total LLM sentiment scoring calls',
    ['status', 'provider'],  # status: success | failure
)

llm_fallbacks_total = Counter(
    'llm_fallbacks_total',
    'Total LLM provider fallbacks within the provider chain',
    ['provider_from', 'provider_to'],  # e.g. nim→groq, groq→ollama, nim→ollama
)

llm_news_forecast_duration_seconds = Histogram(
    'llm_news_forecast_duration_seconds',
    'Gemini news-forecaster latency (seconds), live LLM calls only',
    ['provider'],
    buckets=(0.25, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 5.0, 8.0),
)

llm_news_forecasts_total = Counter(
    'llm_news_forecasts_total',
    'Gemini news-forecaster outcomes',
    # outcome: success | fallback | cache_hit | breaker_open
    ['outcome'],
)

signal_staleness_abstentions_total = Counter(
    'signal_staleness_abstentions_total',
    "Signals abstained because the symbol's daily OHLCV lags the data frontier",
    ['component'],  # ml | forecaster
)

rag_documents_embedded_total = Counter(
    'rag_documents_embedded_total',
    'Total documents embedded into the RAG corpus (cumulative)',
)

rag_backfill_remaining = Gauge(
    'rag_backfill_remaining',
    'Unembedded documents remaining in the active RAG backfill run',
)

rag_backfill_running = Gauge(
    'rag_backfill_running',
    'Whether a RAG corpus backfill is currently running (1) or not (0)',
)

rag_retrieval_duration_seconds = Histogram(
    'rag_retrieval_duration_seconds',
    'RAG hybrid retrieval latency per call (seconds)',
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 2.0),
)

rag_retrievals_total = Counter(
    'rag_retrievals_total',
    'Total RAG retrieval calls',
    ['status'],  # success | empty | error
)

llm_guardrail_events_total = Counter(
    'llm_guardrail_events_total',
    'Total LLM output guardrail events fired',
    ['guardrail'],  # price_prediction_filter | citation_missing
)

llm_audit_log_writes_total = Counter(
    'llm_audit_log_writes_total',
    'Total ai_llm_audit_log write attempts',
    ['status'],  # success | failure
)

llm_sse_explanation_pushes_total = Counter(
    'llm_sse_explanation_pushes_total',
    'Total LLM explanation deliveries via SSE',
    ['path'],  # push (real-time Redis) | poll (periodic DB refresh)
)


# ── Gemini Request Manager Metrics ────────────────────────────────────────────
# Visibility into per-minute quota utilisation, priority queue depth, and
# circuit-breaker state across the six Gemini callers (explanation worker,
# news forecaster, sentiment, event classifier, RAG embedder, health check).
# All metrics use label values produced by GeminiRequestManager — keeping the
# label space in sync here avoids stale Prometheus series.

gemini_requests_total = Counter(
    'gemini_requests_total',
    'Total Gemini API permit outcomes by operation, priority tier, and status',
    # op:       generate | embed
    # priority: critical | high | medium | low | background
    # status:   success | error | quota | rate | timeout
    ['op', 'priority', 'status'],
)

gemini_rpm_utilisation = Gauge(
    'gemini_rpm_utilisation',
    'Fraction of per-minute Gemini request budget consumed (0–1) per operation',
    ['op'],  # generate | embed
)

gemini_tpm_utilisation = Gauge(
    'gemini_tpm_utilisation',
    'Fraction of per-minute Gemini token budget consumed (0–1) — generate only',
)

gemini_queue_depth = Gauge(
    'gemini_queue_depth',
    'Number of Gemini API permits waiting in the priority queue per priority tier',
    ['priority'],  # critical | high | medium | low | background
)

gemini_circuit_open = Gauge(
    'gemini_circuit_open',
    'Whether the Gemini daily quota circuit breaker is open — 1=open, 0=closed',
    ['op'],  # generate | embed
)

gemini_permit_wait_seconds = Histogram(
    'gemini_permit_wait_seconds',
    'Time from GeminiRequestManager.acquire() call to permit granted (seconds)',
    ['op', 'priority'],
    buckets=(
        0.001, 0.005, 0.01, 0.025, 0.05,
        0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0,
    ),
)


# ── Instrument Sync Metrics ─────────────────────────────────────────────────────
instrument_sync_total = Counter(
    'instrument_sync_total',
    'Total instrument-master sync attempts by outcome',
    ['result'],  # success | skipped_304 | aborted_sanity | error
)

instrument_sync_duration_seconds = Histogram(
    'instrument_sync_duration_seconds',
    'Instrument-master sync wall-clock duration',
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
)

instrument_active_count = Gauge(
    'instrument_active_count',
    'Number of active (live, tradeable) instruments after the last sync'
)

instrument_delisted_count = Gauge(
    'instrument_delisted_count',
    'Number of soft-deleted (delisted) instruments in instrument_master'
)

instrument_sync_last_success_timestamp = Gauge(
    'instrument_sync_last_success_timestamp',
    'Unix timestamp of the last successful instrument-master sync (for staleness alerting)'
)


# ── Worker Sidecar Task Metrics ────────────────────────────────────────────────
# Per-task gauges updated by worker_control.py at scrape time (pull model).
# Label `task` matches TASK_NAMES in workers/registry.py.

worker_task_last_cycle_seconds = Gauge(
    'worker_task_last_cycle_seconds',
    'Unix timestamp of the last completed cycle per worker task (from supervised() state)',
    ['task'],
)

worker_task_crash_count_gauge = Gauge(
    'worker_task_crash_count',
    'Consecutive crash count per worker task; reset to 0 on a clean recovery',
    ['task'],
)

worker_task_status_gauge = Gauge(
    'worker_task_status',
    'Worker task lifecycle status: 0=starting 1=running 2=paused 3=crashed 4=stopped',
    ['task'],
)


def init_metrics(app_version: str, environment: str):
    """Initialize application info metrics."""
    app_info.info({
        'version': app_version,
        'environment': environment,
        'name': 'cortex-ai-unified'
    })
