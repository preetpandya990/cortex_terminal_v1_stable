# Redis → Redpanda Queue Migration (one pass)

## Context

Per `KAFKA_UPGRADE_SCOPE.md`: one 512MB `allkeys-lru` Redis instance currently carries durable queueing (explanation/context Streams + DLQ, two zero-retry lists) alongside cache and pub/sub — durable work is evictable, and retry/DLQ logic is hand-rolled three different ways. Decision: adopt **Redpanda** (Kafka API, single-node production-legitimate, no JVM) and migrate **all durable queueing in one pass**, keeping Redis for cache, pub/sub broadcast, locks, dedup keys, and SSE event stores. **Clean cut** — no feature flags, one-time drain of in-flight Redis jobs at deploy. Redpanda Console included. Sentiment queue is an in-process `asyncio.Queue` (`nlp_engine.py`) — out of scope.

Client: `aiokafka==0.14.0` (PyPI latest, Python 3.11-compatible; verified 2026-07-03).

## What moves / what stays

| Moves to Kafka topics | Stays on Redis |
|---|---|
| `cortex:stream:explanation:jobs` → `cortex.explanation.jobs` (2 partitions, 7d retention) | All pub/sub incl. `GEMINI_QUOTA_RESET` |
| `cortex:stream:context:jobs` → `cortex.context.jobs` (1p, 7d) | SSE event stores `cortex:sse:events:*` |
| `cortex:stream:explanation:dlq` → `cortex.explanation.dlq` (1p, 14d) | inflight/dedup/recent/cooldown keys, all locks + Lua CAS heartbeat |
| `cortex:forecast:batch:queue` → `cortex.forecast.batch` (1p, 1d) | forecast dedup `SET NX EX 600`, classification 30-min cache |
| `cortex:event:classifier:pending` → `cortex.classifier.pending` (1p, 7d) | worker heartbeat, budget counters |

2 partitions on explanation jobs only: preserves today's 2-parallel-worker no-head-of-line-blocking design (key by `suggestion_id`). Everything else 1 partition — volume is ~low-thousands msgs/day.

## Core design: retry semantics replacing Redis PEL

Kafka has no `times_delivered`. Pattern: **manual commit + republish-to-tail with `attempts` / `not_before` headers**. On retryable failure: republish same payload with `attempts+1`, `not_before = now + delay`, commit original. Consumer sleeps (≤60s bounded) if `not_before` is in the future. This maps 1:1 onto the PEL lifecycle and deletes `_drain_pel`, `_pel_housekeeping`, `_context_pel_housekeeping` wholesale — crash-before-commit → Kafka redelivers from committed offset (PEL drain for free). Deliberately NOT building retry-topic ladders, schema registry, or transactions — over-engineering at this scale.

## Implementation steps (ordered)

### 1. Dependencies + core module + config
- `backend/requirements.txt`: add `aiokafka==0.14.0` (own section banner, exact-pin style).
- **New** `backend/app/core/kafka.py`, mirroring `redis.py:457-487` singleton lifecycle:
  - `KafkaTopics` / `KafkaGroups` constants (groups: `cortex-explanation-workers`, `cortex-context-workers`, `cortex-forecast-batch`, `cortex-classifier-flush`, `cortex-dlq-requeue`), `_TOPIC_SPECS` (partitions/retention above).
  - `init_kafka()` — idempotent topic creation via `AIOKafkaAdminClient` (swallow `TopicAlreadyExistsError`, mirroring the BUSYGROUP pattern), start singleton `AIOKafkaProducer(acks="all", enable_idempotence=True, linger_ms=5)`. Called from BOTH `main.py` lifespan and `worker_app.py::worker_lifespan`. `close_kafka()`, `get_kafka_producer()` (RuntimeError if uninitialized).
  - `publish(topic, value, *, key=None, headers=None)` — JSON `default=str`, `send_and_wait`.
  - `new_consumer(*topics, group_id)` — `enable_auto_commit=False`, `auto_offset_reset="earliest"`, JSON deserializer (malformed → None → skip+commit).
  - `pending_count(topic, group_id)` — Σ(end_offset − committed) per partition; never-committed → `end − beginning`.
  - Header helpers: `get_attempts`, `get_not_before`, `retry_headers(attempts, delay_secs)`.
- `backend/app/core/config.py`: one field only — `KAFKA_BOOTSTRAP_SERVERS: str = Field("localhost:19092", ...)` (compose overrides to `redpanda:9092`). Add to `.env.example`. No other tunables — topology lives in kafka.py constants.

### 2. Infrastructure
- `docker-compose.yml`: add `redpanda` service (`redpandadata/redpanda:v25.3.15`, `--mode=dev-container --smp=1 --memory=1G`, dual listener: internal `redpanda:9092` / external `localhost:19092` for the bare-metal api+worker mode currently in use; healthcheck `rpk cluster health --exit-when-healthy`; volume `redpanda_data`; ports 19092, 9644) + `redpanda-console` (`redpandadata/console:v3.8.0`, port 8080, `KAFKA_BROKERS: redpanda:9092`, depends_on healthy). Match house style: `container_name: cortex-*`, `restart: unless-stopped`, `cortex-network`. Add `KAFKA_BOOTSTRAP_SERVERS: "redpanda:9092"` + `depends_on redpanda: service_healthy` to `worker` and `api`.
- `prometheus.yml`: scrape job `redpanda` → `redpanda:9644`, path `/public_metrics`, 15s.

### 3. Lifecycle wiring
- `backend/app/main.py`: delete `init_explanation_streams()` call (L71-73); `init_kafka()` after `init_redis()`, `close_kafka()` on shutdown. Keep the 2 explanation + 1 context worker task spawns (L219-233) — they stay in the API container.
- `backend/app/worker_app.py::worker_lifespan`: init/close kafka; consumers use `app.core.kafka` accessors directly (same pattern as `get_redis()` today).

### 4. Explanation + context workers (`backend/app/ai/intelligence/explanation_worker.py`, 2315 lines)
Rewrite transport ranges L1350-2315; business logic (prompts, guardrails, LLM calls, DB writes, SSE XADDs L705-750, lock heartbeat L1040-1074, inflight/DB idempotency) untouched.
- Loops become `new_consumer(...)` + `async for msg` → `_process_*_message(consumer, msg)` (commits internally).
- **Explanation** (`MAX_ATTEMPTS=3`): malformed → commit; attempts≥3 → `_send_to_dlq("max_attempts_exceeded")`+commit; `GeminiRateLimitError`/other → republish `attempts+1, delay 60`+commit; `GeminiQuotaExhausted` → DLQ(`gemini_quota_exhausted`) + `_publish_failed_state` (unchanged Redis pub/sub) + commit; success → commit.
- **Context** (`_MAX_CONTEXT_ATTEMPTS=5`, no DLQ — unchanged policy): attempts≥5 → abandon+commit; recent-key → commit; cooldown-key → republish same-attempts with `not_before=ttl`; `LLMTransientExhausted` → set cooldown + republish `attempts+1, 180`; quota → abandon+commit. All Redis guards verbatim.
- `_move_to_dlq` → `_send_to_dlq(reason, fields, attempts)`: publish `{original_topic, reason, fields, attempts, moved_at}` keyed by `suggestion_id`; keep `llm_explanation_dlq_total`.
- **DLQ quota-requeue** (`_requeue_quota_dlq_entries`, triggers unchanged: boot worker-0 + `GEMINI_QUOTA_RESET` listener): on-demand consumer, group `cortex-dlq-requeue`, drain to snapshotted end offsets under a module `asyncio.Lock`. Per entry: non-quota reason → commit (terminal, retention keeps forensics); not-yet-eligible (moved_at ≥ today PT-midnight) → republish back to DLQ tail + commit; eligible → Redis `SET NX EX 172800` dedup (unchanged key), republish original fields to jobs topic with fresh attempts=0, `gemini_dlq_requeue_total`, commit. Publish failure → delete dedup key, don't commit, abort (resume next trigger).
- Delete: `_drain_pel`, `_pel_housekeeping`, `_context_pel_housekeeping`, `_STREAM_BLOCK_MS`, `_PEL_*`, `_STREAM_MAXLEN_*`, all xreadgroup/xack/xclaim/xadd-to-jobs code.
- Producers rewired to `publish()` keeping existing failure contracts: `engine.py:1067-1098` (non-fatal), `ai_stream.py:457-471` (lock-first) + `:1173-1197` (500 `queue_error`), `watchlist_context_scheduler.py:326-339` (`force="1"`, cap 200).
- `redis.py`: delete stream/group constants + `init_explanation_streams()` (L398-455 partial); KEEP `sse_*_key`, `inflight_key` helpers.

### 5. Forecast batch (`backend/app/ai/fusion/forecast_batch_worker.py` + `signal_assembler.py`)
**Keep drop-with-dedup-TTL semantics — no retry loop.** It's a 5-min-TTL cache warmer; dedup expiry (600s) re-enqueues naturally; retries would burn Gemini quota on stale news.
- Producer `signal_assembler.py:436-473`: dedup SET unchanged → `publish(FORECAST_BATCH, payload, key=symbol)` with added `enqueued_at`. Delete the `LLEN>batch_size*5 → RPOP` trim; replace with consumer-side staleness skip (`now − enqueued_at > 3600` → skip+commit).
- New `ForecastQueueConsumer` (module-level, one consumer + one `asyncio.Lock` shared by loop and flush — same process, no rebalance churn): `drain_batch(max_items, timeout_ms)` via `getmany`, **commit only after `_flush_batch` returns** (fixes crash-loses-popped-items; deliberate batch-drop on Gemini failure still commits).
- `FORECAST_AUTO_DISPATCH=False`: don't poll; update depth gauge from `pending_count()` every 2s tick.
- `flush_pending_forecasts(session_factory)` → `drain_all()`: drain to snapshot, batch by `NEWS_FORECAST_BATCH_SIZE`, commit per group, `RuntimeError` on bad outcome exactly as today (uncommitted remainder stays pending — better than today's loss).
- `pending_forecast_count()` → `pending_count(...)`. `registry.py:236-240` registration shape unchanged (redis arg stays for cache writes only).

### 6. Event classifier (`backend/app/ai/intelligence/event_classifier.py`)
- Producer L759-787: LPUSH → `publish(CLASSIFIER_PENDING, payload, key=str(nlp_result_id))`.
- `flush_pending_classifications(db_factory)`: on-demand consumer (no standing loop today), drain to snapshot, **commit only after successful persist** — fixes today's item-loss bug (LPOP'd then failed = gone). `confidence==0.0` → `RuntimeError` without commit → item redelivered next dispatch; `UNIQUE(nlp_result_id)` (migration 0049) makes redelivery a harmless upgrade. Module `asyncio.Lock` guards concurrent dispatches.
- `pending_classification_count()` → `pending_count(...)`. 30-min cache stays Redis.

### 7. Status/safety-net/metrics
- `backend/app/api/worker_ai_processing.py` L48-82: swap LLEN-based counts for the new lag-based functions; response shape `{pending:int, auto_flush:bool}` unchanged → `admin_ai_processing.py`, `WorkerClient`, frontend types all untouched. Dispatch 502 contract unchanged.
- `backend/app/workers/ai_processing_safety_net.py`: same substitution; thresholds unchanged.
- Finally populate `llm_stream_queue_depth{stream}` (metrics.py:638 — defined, never set): 30s ticker in each worker loop from `pending_count()`. `news_forecast_queue_depth` fed from lag (keeps `GeminiForecastBatchLagging` alert expression working; fix its annotation text).
- No custom lag exporter — Redpanda `/public_metrics` covers broker-side.

### 8. Frontend + Grafana copy
- `frontend/src/components/admin/AIProcessingQueueCard.tsx` L24-31, L72-77: "durable Redis list" → "durable Kafka topic (Redpanda) — survives worker restarts". No type changes.
- Grafana: update panel descriptions mentioning Redis list/stream; optionally one small Redpanda row (broker up + consumer lag from public_metrics).

### 9. Cutover script + deploy
**New** `backend/scripts/migrate_redis_queues_to_kafka.py` (idempotence marker `cortex:migration:kafka_cutover:done`):
1. Streams: republish PEL entries (`xpending_range` all consumers → `XRANGE id id`, header `attempts=times_delivered−1`) + never-delivered (`XINFO GROUPS` last-delivered-id → `XRANGE (id +`). Skip ACKed history.
2. DLQ: `XRANGE - +` → map to new DLQ JSON shape → publish. Leave `cortex:gemini:dlq:requeued:*` dedup keys alone.
3. Lists: LPOP-until-empty → publish (forecast gets fresh `enqueued_at`).
4. `RENAME` drained keys to `cortex:migrated:backup:<name>` (delete manually after 48h); print counts; set marker.

Deploy order: (1) `compose up -d redpanda redpanda-console`, verify `rpk cluster health`; (2) stop api+worker (containers AND bare-metal); (3) run script; (4) `compose up -d --build worker api frontend prometheus`; (5) verify; (6) delete backups after 48h.

### 10. Tests
New: `tests/core/test_kafka.py` (topic-init idempotence, serialization, header round-trip, `pending_count` arithmetic incl. never-committed group); `tests/ai/intelligence/test_explanation_worker_kafka.py` (attempts header increments, DLQ at cap, quota→DLQ+failed-state, `not_before` sleep, context cooldown/abandon paths); `tests/ai/intelligence/test_dlq_requeue_kafka.py` (eligibility, dedup NX skip, publish-failure rollback).
Updated: `tests/ai/fusion/test_forecast_batch_worker.py` (drain mocks, commit-after-flush, staleness skip, AUTO_DISPATCH=False gauge), `tests/api/test_worker_ai_processing.py` (patch `pending_count`, classifier commit-only-after-success), `tests/workers/test_ai_processing_safety_net.py` (signatures).
House style: AsyncMock/MagicMock, scripted `getmany` side_effects, `ensure_future + sleep + shutdown.set` loop pattern — no real broker in unit tests.

## Verification (end-to-end)

1. `docker exec cortex-redpanda rpk cluster health`; after api boot, `rpk topic list` shows 5 topics with expected partitions.
2. `rpk topic produce cortex.classifier.pending` smoke → admin panel pending=1 → dispatch → `rpk group describe cortex-classifier-flush` offset advances only on success.
3. Full `pytest backend/tests` green.
4. On-demand explanation via UI → message visible in Console (localhost:8080) → SSE completes. Kill api mid-processing, restart → redelivery confirmed (offset-based PEL-drain replacement).
5. Prometheus target `redpanda` up; `ai_processing_pending`, `news_forecast_queue_depth`, `llm_stream_queue_depth` populated in Grafana.
6. `graphify update .` after code changes (per CLAUDE.md).

## Accepted risks

- `not_before` sleep head-of-line-blocks a partition ≤60s during rate-limit storms — acceptable at this volume; 2 partitions halve exposure; escalation path (retry topic) documented, not built.
- Retried jobs lose strict FIFO (tail republish) — PEL redelivery was also out-of-order; no consumer is order-sensitive.
- At-least-once (crash between Gemini success and commit) — absorbed by existing DB idempotency + inflight keys, same as Redis Streams today.
- Redpanda `--memory=1G` next to ML-loading workers on one host — watch host RAM.
