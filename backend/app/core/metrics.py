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

rag_relevance_gate_total = Counter(
    'rag_relevance_gate_total',
    'RAG candidate outcomes at the tiered relevance gate (see retriever.py)',
    ['tier', 'outcome'],  # tier: exact|sector|generic ; outcome: admitted|filtered
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
    # status:   success | error | quota | budget | rate | timeout
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

gemini_effective_rpm = Gauge(
    'gemini_effective_rpm',
    'Effective Gemini requests-per-minute budget after proportional key-dropout scaling',
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

gemini_rpd_budget_remaining = Gauge(
    'gemini_rpd_budget_remaining',
    'Estimated daily Gemini generate requests remaining before the HIGH-priority '
    'reservation is breached (budget guard).  Drops to zero when GEMINI_HIGH_PRIORITY_'
    'RPD_RESERVE is reached; resets to full at midnight Pacific Time.',
)

gemini_all_keys_exhausted = Gauge(
    'gemini_all_keys_exhausted',
    'Whether ALL keys in the Gemini pool have open quota circuits — 1=all exhausted '
    '(explanations dead), 0=at least one key active.  Distinct from gemini_circuit_open '
    'which fires when ANY single key is exhausted.',
    ['op'],  # generate | embed
)

gemini_dlq_requeue_total = Counter(
    'gemini_dlq_requeue_total',
    'Total explanation DLQ entries automatically requeued after a Gemini quota reset',
    ['trigger'],  # boot | quota_reset
)

# ── Forecast Batch Worker Metrics ─────────────────────────────────────────────
# Visibility into the async batch forecaster: batch sizes, call outcomes, and
# queue backlog.  Used by the GeminiForecastBatchLagging alert and the Gemini
# Daily Budget burn-down Grafana panel to detect stalled batch workers.

news_forecast_batch_size_histogram = Histogram(
    'news_forecast_batch_size',
    'Number of symbols per batched Gemini news-forecast call',
    buckets=(1, 2, 3, 4, 5, 6, 7, 8, 10),
)

news_forecast_batch_calls_total = Counter(
    'news_forecast_batch_calls_total',
    'Batched Gemini news-forecast call outcomes',
    # outcome: success | validation_partial | budget_throttled | error
    ['outcome'],
)

news_forecast_queue_depth = Gauge(
    'news_forecast_queue_depth',
    'Number of symbols currently pending in the forecast batch queue '
    '(cortex.forecast.batch Kafka topic, measured as consumer-group lag). '
    'Sustained high values indicate a stalled batch worker or sustained '
    'quota exhaustion; retry republishes briefly inflate it.',
)


# ── AI Processing Queue Control Metrics ───────────────────────────────────────
# Visibility into the demand-driven Tier-2 dispatch layer (sentiment/forecast/
# classification): who triggered a drain and what it did, plus live queue
# depth per category for the Worker Control Panel and Grafana.

ai_processing_dispatch_total = Counter(
    'ai_processing_dispatch_total',
    'Dispatch calls by category/trigger/outcome',
    # category: sentiment | forecast | classification
    # trigger_source: manual | scheduled
    # outcome: success | error
    ['category', 'trigger_source', 'outcome'],
)

ai_processing_pending_gauge = Gauge(
    'ai_processing_pending',
    'Pending queue depth per AI processing category',
    ['category'],  # sentiment | forecast | classification
)

ai_processing_safety_net_runs_total = Counter(
    'ai_processing_safety_net_runs_total',
    'Total AI processing safety-net sweep runs by final status',
    ['status'],  # success | error
)

ai_processing_safety_net_duration_seconds = Histogram(
    'ai_processing_safety_net_duration_seconds',
    'Wall-clock duration of each AI processing safety-net sweep (seconds)',
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

ai_processing_safety_net_last_run_timestamp = Gauge(
    'ai_processing_safety_net_last_run_timestamp',
    'Unix timestamp of the last AI processing safety-net sweep',
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
#
# Two independent timestamps — do not conflate:
#   last_cycle_seconds  — sourced from TaskState.last_cycle_at, set by the
#                         task's own loop body via record_cycle() once per
#                         real work cycle. THE genuine liveness/staleness
#                         signal. Falls back to started_at until the first
#                         cycle completes (see worker_control.py /metrics).
#   started_at_seconds  — sourced from TaskState.last_run_at, set once by
#                         supervised() per process start / crash-restart.
#                         Answers "when did this task last restart," not
#                         "is it still doing work" — useful for
#                         crash-restart-recency, not for staleness alerting.
#
# expected_interval_seconds is static per task (TASK_EXPECTED_INTERVAL_SECONDS
# in workers/registry.py), published once at worker startup (worker_app.py),
# not per scrape. Absent for event-driven tasks with no fixed cadence
# (cache_invalidation, pnl_worker, sl_tp_worker) — PromQL `on(task)` joins
# against it naturally exclude those tasks from ratio-based staleness alerts.

worker_task_last_cycle_seconds = Gauge(
    'worker_task_last_cycle_seconds',
    'Unix timestamp of the last completed real work cycle per worker task (from TaskState.last_cycle_at)',
    ['task'],
)

worker_task_started_at_seconds = Gauge(
    'worker_task_started_at_seconds',
    'Unix timestamp of the current supervised() iteration start (crash-restart recency; NOT cycle activity)',
    ['task'],
)

worker_task_expected_interval_seconds = Gauge(
    'worker_task_expected_interval_seconds',
    'Expected seconds between real work cycles for this task; absent for event-driven tasks with no fixed cadence',
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


# ── RAG Batch Embedding Metrics ───────────────────────────────────────────────

rag_batch_jobs_total = Counter(
    'rag_batch_jobs_total',
    'Total Gemini batch embedding jobs by final status',
    ['status'],  # submitted | succeeded | failed | timeout | cancelled
)

rag_batch_job_duration_seconds = Histogram(
    'rag_batch_job_duration_seconds',
    'Wall-clock duration from batch job submission to terminal state (seconds)',
    buckets=(30, 60, 120, 300, 600, 1200, 1800, 3600, 7200),
)

rag_batch_job_texts_total = Counter(
    'rag_batch_job_texts_total',
    'Total texts submitted across all Gemini batch embedding jobs',
)


# ── RAG Corpus Cleanup Metrics ────────────────────────────────────────────────

rag_cleanup_rows_deleted_total = Counter(
    'rag_cleanup_rows_deleted_total',
    'Total ai_document_embeddings rows deleted by the daily TTL cleanup job',
)

rag_cleanup_runs_total = Counter(
    'rag_cleanup_runs_total',
    'Total RAG corpus cleanup job runs by final status',
    ['status'],  # success | error
)

# ── Fundamentals rate limiter ──────────────────────────────────────────────────

fundamentals_rate_slots_total = Counter(
    'fundamentals_rate_slots_total',
    'Fundamentals API rate-limit slots acquired, by rate-limiting backend',
    ['backend'],  # redis | inprocess
)

fundamentals_rate_circuit_open = Gauge(
    'fundamentals_rate_circuit_open',
    '1 when the Redis GCRA circuit breaker is open (in-process fallback active), 0 otherwise',
)


# ── Explanation Pipeline Reliability Metrics ─────────────────────────────────
# Visibility into the Redis-Streams-backed explanation pipeline (Gap 4, 7 fixes).
# These complement the existing llm_* metrics and are scoped to the delivery
# infrastructure rather than the LLM call itself.

llm_explanation_dlq_total = Counter(
    'llm_explanation_dlq_total',
    'LLM explanation jobs moved to the dead-letter queue after exhausting MAX_ATTEMPTS',
    ['job_type'],  # explanation | context
)

llm_ready_publish_failures_total = Counter(
    'llm_ready_publish_failures_total',
    'Failures publishing the SSE wakeup signal after a successful explanation write '
    '(browser recovers via 30-second poll cycle)',
    ['job_type'],  # explanation | context
)

llm_explanation_dedup_total = Counter(
    'llm_explanation_dedup_total',
    'Duplicate LLM explanation calls intercepted before reaching the Gemini API',
    ['layer'],  # db_idempotency | inflight_key
)

llm_stream_queue_depth = Gauge(
    'llm_stream_queue_depth',
    'Pending (uncommitted) messages per LLM job topic, measured as Kafka '
    'consumer-group lag; retry republishes briefly inflate it',
    ['stream'],  # explanation | context
)

llm_explanation_worker_active = Gauge(
    'llm_explanation_worker_active',
    'Number of explanation worker tasks currently executing an LLM call',
)


# ── Watchlist Context Scheduler Metrics ──────────────────────────────────────

watchlist_scheduler_runs_total = Counter(
    'watchlist_scheduler_runs_total',
    'Total watchlist context scheduler batch runs by final status',
    ['status'],  # success | error | skipped_empty | skipped_on_demand | interrupted
)

watchlist_scheduler_instruments_queued_total = Counter(
    'watchlist_scheduler_instruments_queued_total',
    'Total instrument context jobs enqueued across all watchlist scheduler runs',
)

watchlist_scheduler_duration_seconds = Histogram(
    'watchlist_scheduler_duration_seconds',
    'Wall-clock duration of each watchlist context scheduler batch run (seconds)',
    buckets=(0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)

watchlist_scheduler_last_run_timestamp = Gauge(
    'watchlist_scheduler_last_run_timestamp',
    'Unix timestamp of the last successful watchlist context scheduler run',
)

watchlist_scheduler_snapshot_unavailable_total = Counter(
    'watchlist_scheduler_snapshot_unavailable_total',
    'Instrument context jobs published with a degraded (unavailable) prediction snapshot, by reason',
    ['reason'],  # no_model | insufficient_data | timeout
)

watchlist_scheduler_publish_failed_total = Counter(
    'watchlist_scheduler_publish_failed_total',
    'Instrument context jobs that failed to publish to Kafka (broker/network failure, not snapshot computation)',
)


# ── Event Classification Pipeline Metrics ────────────────────────────────────
# Full visibility across all four classification paths, enabling quota-burn
# attribution and heuristic coverage measurement in Grafana.
#
# Label values (method):
#   cache              — Redis cache hit; zero Gemini cost
#   heuristic          — Keyword pre-filter matched; Gemini call skipped
#   llm                — Live Gemini call made (article fell through to 'general')
#   rule_based_fallback — Gemini failed; deterministic fallback used (error path)

event_classification_total = Counter(
    'event_classification_total',
    'Event classification outcomes by resolution method',
    ['method'],  # cache | heuristic | llm | rule_based_fallback
)


# ── Sentiment Accuracy Metrics (WS4) ─────────────────────────────────────────
# Guardrail observability for the pure-Gemini sentiment pipeline: sign-flip
# coercions, batch misattribution, and rolling drift vs the 30-day baseline.

sentiment_sign_violations_total = Counter(
    'sentiment_sign_violations_total',
    'LLM sentiment responses whose label contradicted the score sign '
    '(label coerced from score, confidence capped at 0.5)',
    ['path'],  # single | batch
)

sentiment_batch_misattribution_total = Counter(
    'sentiment_batch_misattribution_total',
    'Batched sentiment results whose reasoning shares no content token with '
    'the article it claims to describe (confidence halved)',
)

sentiment_drift_label_fraction = Gauge(
    'sentiment_drift_label_fraction',
    'Fraction of ai_nlp_results rows carrying each sentiment label per window',
    ['label', 'window'],  # label: positive|negative|neutral · window: 7d|30d
)

sentiment_drift_mean_score = Gauge(
    'sentiment_drift_mean_score',
    'Mean stored sentiment score (-1..1) per rolling window',
    ['window'],  # 7d | 30d
)

sentiment_drift_distribution_distance = Gauge(
    'sentiment_drift_distribution_distance',
    'Total variation distance (0..1) between the 7d and 30d label distributions',
)

sentiment_drift_runs_total = Counter(
    'sentiment_drift_runs_total',
    'Sentiment drift monitor cycles by outcome',
    ['status'],  # success | error | insufficient_data
)


# ── Forecast Demand Gating Metrics (WS6) ─────────────────────────────────────

forecast_enqueue_gated_total = Counter(
    'forecast_enqueue_gated_total',
    'Forecast batch enqueues skipped because the symbol is not in the '
    'in-demand set (watchlist or active suggestions)',
)

forecast_stale_dropped_total = Counter(
    'forecast_stale_dropped_total',
    'Forecast batch payloads dropped as stale (older than _STALE_AFTER_SECS) '
    'during queue drains',
)


# ── Safety Engine Metrics (WS9) ──────────────────────────────────────────────
# The kill-switch manager owns its own activation/check metrics; these cover
# the trigger engine's live inputs and its anti-latch recovery behavior.

safety_metric_value = Gauge(
    'safety_metric_value',
    'Live safety-engine input metrics (signal_rate: suggestions/hour · '
    'volatility: high-vol regime prevalence vs 30d baseline · '
    'loss_pct: realized session loss fraction)',
    ['metric'],  # signal_rate | volatility | loss_pct
)

safety_triggers_total = Counter(
    'safety_triggers_total',
    'Safety triggers fired, by type (cooldown-suppressed re-fires excluded)',
    ['trigger_type'],  # high_signal_rate | high_volatility | session_loss_limit
)

safety_kill_switch_auto_releases_total = Counter(
    'safety_kill_switch_auto_releases_total',
    'Engine-activated switches auto-released after sustained metric recovery '
    '(manual activations are never auto-released)',
    ['switch_type'],  # global | signal_pause
)


# ── On-Demand Explanation Metrics (WS7) ──────────────────────────────────────

explanation_demand_latency_seconds = Histogram(
    'explanation_demand_latency_seconds',
    'End-to-end generation latency for demand-triggered explanations '
    '(user is watching a generating skeleton; p95 target < 20s)',
    buckets=(1, 2.5, 5, 7.5, 10, 15, 20, 30, 45, 60, 90, 120),
)

explanation_ungrounded_numbers_total = Counter(
    'explanation_ungrounded_numbers_total',
    'Sentences stripped from explanations because they cited a %/RSI figure '
    'absent from the prompt (hallucinated number guardrail)',
)


def init_metrics(app_version: str, environment: str):
    """Initialize application info metrics."""
    app_info.info({
        'version': app_version,
        'environment': environment,
        'name': 'cortex-ai-unified'
    })
