# Redis → Redpanda Queue Migration — Master Plan & Tracker

> **Status legend:** `[ ]` pending · `[~]` in progress · `[x]` done · `[!]` blocked
> **Created:** 2026-07-03 · **Supersedes nothing** — `REDPANDA_MIGRATION_PLAN.md` remains the original design record; THIS document is the authoritative implementation tracker and incorporates all corrections found during code + web verification (2026-07-03).
> **Session scope note:** planning completed 2026-07-03; implementation + cutover deploy completed 2026-07-04 (see `REDPANDA_MIGRATION_IMPLEMENTATION_REPORT.md` for the full what/how/why report). Git management is owner-handled (large uncommitted batch on `main` predates this work).
> **Cross-check note (2026-07-03, second pass):** full codebase verification re-run with 4 parallel scans — every load-bearing claim below re-confirmed against the working tree; zero Kafka code exists anywhere (`grep aiokafka|KAFKA_BOOTSTRAP` = 0 hits). Six minor corrections from that pass are folded in below, marked ⚑.

---

## 1. Why we are doing this (context — do not lose this)

One 512 MB `allkeys-lru` Redis instance (`docker-compose.yml:72-73`) currently carries **durable job queueing** alongside cache, pub/sub, locks, and SSE stores. That is architecturally wrong in three ways:

1. **Durable work is evictable.** `allkeys-lru` means under memory pressure Redis may evict queued jobs (Streams and lists) exactly when the system is busiest.
2. **Retry/DLQ logic is hand-rolled three different ways.** Explanation/context use Redis Streams + PEL (`xpending`/`xclaim` housekeeping, ~500 lines of transport code in `explanation_worker.py`); forecast and classifier use bare `LPUSH`/`LPOP` lists with **zero retry** — both flush paths lose already-popped items on failure (confirmed bugs, see §3.7).
3. **No broker-side observability.** `llm_stream_queue_depth` has been defined but never set since it was added (`metrics.py:638`, zero setters — confirmed by grep).

**Decision (from original plan, upheld):** adopt **Redpanda** (Kafka API, single-node-legitimate, no JVM/ZooKeeper) and migrate **all durable queueing in one pass**. Redis keeps everything it is genuinely good at: cache, pub/sub broadcast, locks + Lua CAS heartbeat, dedup/inflight/cooldown keys, SSE event stores, budget counters. **Clean cut** — no feature flags, no dual-write; a one-time drain script moves in-flight Redis jobs at deploy. Redpanda Console included for operations. Sentiment queue is an in-process `asyncio.Queue` (`nlp_engine.py`) — **out of scope**.

**Deliberately NOT building** (over-engineering at ~low-thousands msgs/day): retry-topic ladders, schema registry, Kafka transactions/EOS, a custom lag exporter.

---

## 2. Verified version pins (web-verified 2026-07-03)

| Component | Pin | Notes |
|---|---|---|
| `aiokafka` | **`0.14.0`** | Latest on PyPI; Py3.11 wheels; no known critical bugs with manual commit or `enable_idempotence`. Do NOT pass `api_version` (deprecated; auto-negotiated ≥0.13, correct for Redpanda). |
| `redpandadata/redpanda` | **`v26.1.12`** | ⚠ CHANGED from original plan's `v25.3.15` — 25.3 is a maintenance branch; 26.1 is the current line (v26.1.12, late June 2026). New deployments should pin current. |
| `redpandadata/console` | **`v3.8.0`** | Current. `KAFKA_BROKERS` env var still valid in v3 (maps to `kafka.brokers`, unmoved by the v3 config restructure). |

**Broker launch flags — ⚠ CHANGED from original plan.** `--mode=dev-container` sets `developer_mode=true`; Redpanda's production-readiness checklist requires that false even single-node → **not production-legitimate**. Use instead:

```
--overprovisioned --smp=1 --memory=1G --reserve-memory=0M
```

- `--reserve-memory=0M` is mandatory in containers (Seastar's host-memory heuristic is wrong inside cgroups).
- `--memory` should be ~80% of the container limit; set a compose `mem_limit`/deploy limit ~1.25G to match.
- `--overprovisioned` is the accepted single-node-on-shared-host compromise (disables thread pinning/idle-poll); we accept forfeiting dedicated-host tuning. Escalation path if this box ever gets dedicated cores: drop `--overprovisioned`, run `rpk redpanda tune all`.
- Cluster property for broker-side lag metrics: `enable_consumer_group_metrics: ["group","partition","consumer_lag"]` (needed for `redpanda_kafka_consumer_group_lag_max/sum` on `/public_metrics`).

---

## 3. Corrections to the original plan (found during code verification — all confirmed against working tree 2026-07-03)

These are load-bearing; implementation MUST use these corrected facts:

1. **Explanation producer path is `backend/app/ai/correlation/engine.py:1067-1098`**, NOT `intelligence/engine.py`. (Line numbers were right; module path was wrong.)
2. **`worker_lifespan` is defined in `backend/app/worker.py:57`**, not `worker_app.py` (`worker_app.py:77-78` only imports/enters it). Kafka init/close wiring goes in `worker.py`. Also: `worker.py:87` hands tasks a `CacheService`, not a raw Redis client.
3. **`_publish_failed_state` (`explanation_worker.py:1352-1389`) sits inside the "rewrite L1350-2315" range but is business/SSE+pub-sub logic that MUST be kept.** The transport/business boundary is not a clean line; rewrite by function, not by line range.
4. **`context_worker()` has an inline PEL-drain block (L2241-2269)** in addition to the three named PEL functions — all four sites must be deleted.
5. **Stream maxlen constants are multiplied across files:** explanation maxlen 5000 = `explanation_worker._STREAM_MAXLEN_EXPLANATION` (L171) + hardcoded at `ai_stream.py:1181` + `correlation/engine.py:1079`. Context maxlen 1000 = `explanation_worker._STREAM_MAXLEN_CONTEXT` (L172) + `ai_stream._STREAM_MAXLEN_CONTEXT` (L160) + `watchlist_context_scheduler._CONTEXT_STREAM_MAXLEN` (L77). Delete all six.
6. **`flush_pending_classifications` real signature is `(self, db_factory, redis)`** — dropping the `redis` transport arg changes the signature at BOTH call sites: `worker_ai_processing.py:138` and `ai_processing_safety_net.py:250`.
7. **Confirmed current-behavior item-loss bugs this migration fixes:** `flush_pending_forecasts` (`forecast_batch_worker.py:226-231`) and `flush_pending_classifications` (`event_classifier.py:562-568`) both LPOP before processing and raise without requeue — in-flight items are lost. Kafka commit-after-success is the fix, backed for the classifier by `UNIQUE(nlp_result_id)` (migration 0049, already written).
8. **`news_forecast_queue_depth` has TWO set-sites today:** `forecast_batch_worker.py:132` AND `signal_assembler.py:471` (inside the trim block being deleted). Post-migration the gauge is fed from consumer lag only — reconcile deliberately so the producer-side write isn't silently lost.
9. **Forecast queue key is a hardcoded string literal in three places:** worker `_QUEUE_KEY` (L72), producer lpush (`signal_assembler.py:464`), producer llen/rpop (L470/L473). Replace all literals, not just the constant. Also delete the worker's unused `_DEDUP_PREFIX` (L73).
10. **Forecast payload has NO `enqueued_at` today** (`signal_assembler.py:455-461`); the classifier payload DOES (L779). Adding `enqueued_at` to the forecast payload is a real change enabling the staleness skip.
11. **Two `.env.example` files exist.** ⚑ REFINED (2nd pass): `backend/.env.example` is the **template** (compose header L24: `cp backend/.env.example .env`), but the compose services actually load the **root `.env`** (`env_file: .env` at `docker-compose.yml:163` worker / `:201` api). Add `KAFKA_BOOTSTRAP_SERVERS` to `backend/.env.example` (after the Redis block, ~L26) **AND the operator must add it to the live root `.env` at deploy** (runbook step added in Phase 9). Root `.env.example` is stale (Apr 10) — ignore.
12. **Grafana: there is nothing to edit.** Full grep of all 7 dashboards found NO panel mentioning Redis lists/streams or any queue-depth metric. Original plan's "update panel descriptions" is a no-op; only the optional new Redpanda row is real work. ⚑ 2nd-pass nuance: one literal "queue depth" hit at `06-security-realtime.json:1494` ("WebSocket Queue Size by Channel") is a WebSocket outbound-queue panel, unrelated — leave it alone.
13. **Frontend types live in `frontend/src/types/ai_processing.ts`** (`CategoryStatus{pending,auto_flush}` L3-6), not `analysis.ts`. No type changes needed (response shape preserved).
14. **Deploy-mode reality:** evidence says API runs bare-metal (`prometheus.yml` scrapes `host.docker.internal:8000`; `scripts/start-api.sh`) while worker is containerized (scraped at `worker:8001`). Both compose services are fully defined. The dual Kafka listener (internal `redpanda:9092` / external `localhost:19092`) is therefore REQUIRED, not optional.
15. **Stale prose to update alongside code:** `config.py:376-393` ("Redis list… drains the queue") and `config.py:93-94` (Worker Sidecar comment "All data flow continues through Redis pub/sub").
16. **Error-class locations:** `GeminiRateLimitError` is defined in `request_manager.py:149` (re-exported via `llm_client`); `GeminiQuotaExhausted` (`llm_client.py:145`) and `LLMTransientExhausted` (`llm_client.py:159`) both subclass `LLMFallbackExhausted`. ⚑ A separate, plain-`Exception` `GeminiQuotaExhausted` also exists at `request_manager.py:135` — new Kafka consumer code MUST import from `llm_client` (matching existing worker code), not `request_manager`.
17. **Worker-0-only gating:** both the boot DLQ requeue (`explanation_worker.py:2069-2070`) and the `GEMINI_QUOTA_RESET` listener (L2087-2088) are gated to `worker_id == 0`. The Kafka DLQ-requeue consumer must preserve this single-runner property (worker-0 gate + module `asyncio.Lock`).
18. **`ai_processing_pending` gauge** is set only inside `GET /ai-processing/status` (`worker_ai_processing.py:65-67`) — stale between polls. Unchanged by this migration but noted.
19. **Classifier `confidence==0.0` conflation:** `_gemini_classify` returns 0.0 on any failure AND the schema default is 0.0, so a legitimate zero-confidence result is indistinguishable from failure at `event_classifier.py:562`. Carried over as-is (same signal), documented as known debt.

---

## 4. Retry design (final, incorporating web-verified pitfalls)

Kafka has no `times_delivered` (Redis PEL's `delivery_count`). Replacement pattern: **manual commit + republish-to-tail with headers** `attempts` (int) and `not_before` (epoch secs).

- On retryable failure: republish the same payload with `attempts+1` and `not_before = now + delay`, then commit the original. Crash-before-commit → Kafka redelivers from committed offset (this is the PEL-drain replacement, for free).
- **⚠ REFINED vs original plan — bounded sleep + re-republish hybrid.** Original plan said "consumer sleeps (≤60s bounded) if `not_before` is in the future." Web research: sleeping in the consumer loop is the classic way to blow `max_poll_interval_ms` (default 300 000 ms) and get evicted from the group → `CommitFailedError`. Final rule:
  - If `not_before − now ≤ 60s`: sleep the remainder (shutdown-interruptible), then process. Safe: aiokafka heartbeats in a background task; 60s ≪ 300s poll interval.
  - If `not_before − now > 60s` (context cooldown can republish with delays up to 180s): **do not sleep** — republish to tail with the SAME `attempts` and same `not_before`, commit, move on.
  - Keep `max_poll_interval_ms` at default 300 000 but set consumer `max_records` low (explanation/context process one message at a time anyway) so Gemini-latency work (≤120s LLM timeout) plus a ≤60s sleep stays under it.
- **Monotonic backoff + hard attempts cap** (already in design: 3 explanation / 5 context) prevents poison-message hot-looping.
- **Retry headers stay minimal:** `attempts`, `not_before` only — per-attempt header bloat multiplies record size.
- **Topics use plain `delete` retention, never compaction** — retries reuse keys; compaction could drop the original record.
- **Known side-effect:** republish-to-tail inflates end offsets, so lag-based `pending_count` briefly over-counts during retry storms (retried msgs are genuinely pending, so it is arguably *correct*; DLQ not-yet-eligible recycling also churns offsets). Acceptable at this volume; documented for alert-reading sanity.
- Explicit consumer settings everywhere: `enable_auto_commit=False`, `auto_offset_reset="earliest"` (the default `latest` silently skips backlog on first start — must be explicit).
- Producer: single long-lived `AIOKafkaProducer(enable_idempotence=True, linger_ms=5)` per process — `enable_idempotence=True` forces `acks=all` (passing a conflicting acks raises ValueError, so don't pass acks at all). Redpanda 26.x supports idempotence by default.
- Admin client gotcha: `await admin.start()` BEFORE `create_topics` (else `IncompatibleBrokerVersion`); swallow `TopicAlreadyExistsError` (mirrors the existing BUSYGROUP pattern at `redis.py:450-454`).

---

## 5. Topic / group topology (unchanged from original plan)

| Redis structure (today) | Kafka topic | Partitions | Retention | Consumer group |
|---|---|---|---|---|
| `cortex:stream:explanation:jobs` (Stream) | `cortex.explanation.jobs` | **2** | 7d | `cortex-explanation-workers` |
| `cortex:stream:context:jobs` (Stream) | `cortex.context.jobs` | 1 | 7d | `cortex-context-workers` |
| `cortex:stream:explanation:dlq` (Stream) | `cortex.explanation.dlq` | 1 | 14d | `cortex-dlq-requeue` (on-demand) |
| `cortex:forecast:batch:queue` (List) | `cortex.forecast.batch` | 1 | 1d | `cortex-forecast-batch` |
| `cortex:event:classifier:pending` (List) | `cortex.classifier.pending` | 1 | 7d | `cortex-classifier-flush` |

2 partitions on explanation only: preserves today's 2-parallel-worker no-head-of-line-blocking design (key = `suggestion_id`). All topics `cleanup.policy=delete`.

**Stays on Redis (verbatim list, verified):** all pub/sub (`LLM_EXPLANATION_READY`, `LLM_CONTEXT_READY`, `GEMINI_QUOTA_RESET`); SSE stores `cortex:sse:events:*` (read by `ai_stream.py:389,431,752,795`); inflight key (SET NX EX 150); context recent (60s) + cooldown (180s) keys; context generation lock + Lua CAS heartbeat; DLQ-requeue dedup `cortex:gemini:dlq:requeued:*` (SET NX EX 172800); forecast dedup `cortex:forecast:batch:dedup:*` (SET NX EX 600); classifier 30-min cache; Gemini budget counters/circuits; worker heartbeat.

---

## 6. Implementation phases

### Phase 1 — Dependencies, core module, config
**Status: `[x]` done (2026-07-04)**

- [ ] `backend/requirements.txt`: new `# ── Kafka / Event Streaming ──` banner after the Redis section (L26); `aiokafka==0.14.0` exact-pin with aligned inline comment (house style).
- [ ] **New** `backend/app/core/kafka.py` mirroring `redis.py:458-487` singleton lifecycle:
  - `KafkaTopics` / `KafkaGroups` constants + `_TOPIC_SPECS` (partitions/retention/cleanup.policy per §5).
  - `init_kafka()` — `AIOKafkaAdminClient` (start → create_topics → swallow `TopicAlreadyExistsError` → close), then start singleton `AIOKafkaProducer(enable_idempotence=True, linger_ms=5)`. Idempotent; called from BOTH `main.py` lifespan and `worker.py::worker_lifespan`.
  - `close_kafka()`; `get_kafka_producer()` raising `RuntimeError("Kafka not initialized")` (mirrors `get_redis()`).
  - `publish(topic, value, *, key=None, headers=None)` — JSON `default=str`, `send_and_wait`.
  - `new_consumer(*topics, group_id)` — `enable_auto_commit=False`, `auto_offset_reset="earliest"`, JSON value-deserializer (malformed → `None` → caller skips+commits).
  - `pending_count(topic, group_id)` — Σ per partition (end_offset − committed); never-committed group → (end − beginning).
  - Header helpers: `get_attempts(msg)`, `get_not_before(msg)`, `retry_headers(attempts, delay_secs)`.
- [ ] `backend/app/core/config.py`: ONE field — `KAFKA_BOOTSTRAP_SERVERS: str = Field("localhost:19092", description=...)` — new `# ── Kafka / Redpanda ──` banner after the Redis section (~L49). No other tunables (topology lives in kafka.py constants).
- [ ] Fix stale prose: `config.py:376-393` forecast-accumulator comment ("Redis list" → Kafka topic) and `config.py:93-94` sidecar comment.
- [ ] `backend/.env.example`: add `KAFKA_BOOTSTRAP_SERVERS=localhost:19092` after the Redis block (~L26). (NOT the stale root `.env.example`.)

**Why:** one shared, tested transport module = single place for serialization, retry headers, and lag math; config surface deliberately minimal.

### Phase 2 — Infrastructure (compose + prometheus)
**Status: `[x]` done (2026-07-04)** — cluster property set via bootstrap `--set` (fresh volume); fallback `rpk cluster config set` documented in compose comment.

- [ ] `docker-compose.yml` — `redpanda` service: `redpandadata/redpanda:v26.1.12`; flags per §2 (`--overprovisioned --smp=1 --memory=1G --reserve-memory=0M`, NOT dev-container); dual listener internal `redpanda:9092` / external `localhost:19092` (+ matching advertised listeners — required because API runs bare-metal); healthcheck `rpk cluster health --exit-when-healthy`; volume `redpanda_data`; ports `19092:19092`, `9644:9644`; house style `container_name: cortex-redpanda`, `restart: unless-stopped`, `networks: [cortex-network]`. Set cluster property `enable_consumer_group_metrics` (via `--set redpanda.enable_consumer_group_metrics=...` or post-boot `rpk cluster config set`)—decide at implementation; document choice here.
- [ ] `redpanda-console` service: `redpandadata/console:v3.8.0`, port `8080:8080`, `KAFKA_BROKERS: redpanda:9092`, `depends_on redpanda: service_healthy`, `container_name: cortex-redpanda-console`.
- [ ] Add `KAFKA_BOOTSTRAP_SERVERS: "redpanda:9092"` to the `environment:` blocks of `worker` AND `api` (the blocks already overriding `DATABASE_URL`/`REDIS_URL` at L164-166/L202-205) + `depends_on redpanda: service_healthy` on both.
- [ ] `prometheus.yml`: job `redpanda` → target `redpanda:9644`, `metrics_path: /public_metrics`, `scrape_interval: 10s` (⚠ 10s not 15s — matches the two existing jobs, original plan's 15s was inconsistent).
- [ ] Port-collision check done: 19092/9644/8080 are free (existing: 5433, 6379, 9090, 3001, 8000, 3000, worker exposed 8001). ✅ verified.

**Why:** dual listener is non-negotiable given bare-metal API; production flags per Redpanda's own checklist (no `developer_mode` in prod).

### Phase 3 — Lifecycle wiring
**Status: `[x]` done (2026-07-04)**

- [ ] `backend/app/main.py`: delete `init_explanation_streams()` import+call (L71-73); add `await init_kafka()` after `init_redis()` (L66); add `await close_kafka()` in shutdown BEFORE `close_redis()` (L340) and AFTER worker-task cancellation (L288-300) — consumers must be dead before the producer/broker clients close. Keep the 2 explanation + 1 context worker task spawns (L219-233) — they stay in the API process.
- [ ] `backend/app/worker.py::worker_lifespan` (⚠ NOT worker_app.py): `await init_kafka()` after `init_redis()` (L86); `close_kafka()` in the `finally` before `close_redis()`. Consumers access `app.core.kafka` accessors directly (same pattern as `get_redis()`).

**Why:** mirrors the existing Redis lifecycle exactly; both processes produce and consume, so both need init.

### Phase 4 — Explanation + context workers (`explanation_worker.py`, 2315 lines)
**Status: `[x]` done (2026-07-04)** — the biggest phase. Rewrite **by function, not by line range** (see §3.3). ⚑ Constant locations (2nd pass): `MAX_ATTEMPTS=3` L130, `_MAX_CONTEXT_ATTEMPTS=5` L133, `_RECONNECT_DELAY_SECS=5` L134, `_CONSUMER_GROUP` L140, `_STREAM_BLOCK_MS` L141, `_PEL_IDLE_THRESHOLD_MS` L142, `_PEL_HOUSEKEEPING_INTERVAL_SECS` L143; the MAXLEN pair at L171-172 as stated.

**Untouched (business logic):** schema/prompts/guardrails (L186-646), `_write_audit_entry` (L651-700), SSE writers (L705-750), `_generate_explanation` (L754-1035) incl. inflight keys + ready publish, `_lock_heartbeat` (L1040-1074), `_generate_instrument_context` (L1077-1347), **`_publish_failed_state` (L1352-1389 — KEEP, it's SSE/pub-sub)**, `_quota_reset_listener` (L1981-2035 — stays on Redis pub/sub, now triggers the Kafka DLQ requeue).

- [ ] **Delete transport wholesale:** `_move_to_dlq` (L1392-1436), `_drain_pel` (L1439-1522), `_pel_housekeeping` (L1525-1609), `_context_pel_housekeeping` (L2137-2203), the inline PEL-drain in `context_worker()` (L2241-2269), constants `_STREAM_BLOCK_MS`, `_PEL_IDLE_THRESHOLD_MS`, `_PEL_HOUSEKEEPING_INTERVAL_SECS`, `_STREAM_MAXLEN_EXPLANATION`, `_STREAM_MAXLEN_CONTEXT`, `_CONSUMER_GROUP`, all xreadgroup/xack/xclaim call sites.
- [ ] **New consumer loops:** `explanation_worker(worker_id)` and `context_worker()` become `new_consumer(topic, group_id=...)` + `async for msg` → `_process_explanation_message(consumer, msg)` / `_process_context_message(consumer, msg)` (each commits internally). Reconnect-on-error backoff (`_RECONNECT_DELAY_SECS=5`) preserved.
- [ ] **Explanation policy** (`MAX_ATTEMPTS=3`, from PEL semantics at L1614-1677): malformed/missing `suggestion_id` → commit+skip; `attempts ≥ 3` → `_send_to_dlq("max_attempts_exceeded")`+commit; success → commit; `GeminiRateLimitError` and generic `Exception` → republish `attempts+1, delay 60` + commit; `GeminiQuotaExhausted` → DLQ(`gemini_quota_exhausted`) + `_publish_failed_state` (unchanged) + commit.
- [ ] **Context policy** (`_MAX_CONTEXT_ATTEMPTS=5`, no DLQ, from L1680-1826): malformed → commit; `attempts ≥ 5` → abandon+commit; recent-key exists → commit-skip; cooldown-key exists → republish SAME attempts with `not_before = now + remaining TTL`, commit; success → commit + set recent key (60s); `LLMTransientExhausted` → set cooldown (180s) + republish `attempts+1, delay 180` + commit; `GeminiRateLimitError` → republish `attempts+1, delay 60` + commit; `GeminiQuotaExhausted` → abandon+commit; other → republish `attempts+1, delay 60` + commit. All Redis guard keys verbatim.
- [ ] **`not_before` handling per §4:** ≤60s remaining → interruptible sleep; >60s → republish-to-tail same-attempts + commit.
- [ ] **`_send_to_dlq(reason, payload, attempts)`** replacing `_move_to_dlq`: publish `{original_topic, reason, fields, attempts, moved_at}` to `cortex.explanation.dlq` keyed by `suggestion_id`; keep `llm_explanation_dlq_total` metric.
- [ ] **DLQ quota-requeue** (`_requeue_quota_dlq_entries`, triggers unchanged: worker-0 boot + `GEMINI_QUOTA_RESET` listener, both worker-0-gated per §3.17): on-demand consumer, group `cortex-dlq-requeue`, drain to snapshotted end offsets under a module `asyncio.Lock`. Per entry: non-quota reason → commit (terminal; retention keeps forensics); not-yet-eligible (`moved_at` ≥ today's midnight-PT) → republish back to DLQ tail + commit; eligible → Redis `SET NX EX 172800` dedup (key unchanged) → republish original fields to `cortex.explanation.jobs` with fresh `attempts=0` → `gemini_dlq_requeue_total` → commit. **Publish failure → delete dedup key, do NOT commit, abort the drain** (resume next trigger).
- [ ] **Producers → `publish()`** keeping existing failure contracts exactly:
  - `backend/app/ai/correlation/engine.py:1067-1098` (⚠ corrected path): key=`suggestion_id`; failure = warn-only, never blocks the committed suggestion.
  - `backend/app/api/v1/ai_stream.py:457-471` (context, lock-first — lock acquisition stays Redis, publish replaces xadd) and `:1173-1197` (on-demand explanation: failure → delete debounce key + HTTP 500 `queue_error`; success → 202; remove hardcoded maxlen).
  - `backend/app/workers/watchlist_context_scheduler.py:326-339`: payload keeps `force="1"`, `source`, empty lock fields; batch cap + per-item try/except unchanged (⚑ cap is `settings.WATCHLIST_SCHEDULER_BATCH_CAP`, default 200 at `config.py:342` — not a literal); delete `_CONTEXT_STREAM_MAXLEN`.
- [ ] `backend/app/core/redis.py`: delete `RedisStreams.EXPLANATION_JOBS/CONTEXT_JOBS/EXPLANATION_DLQ/CONSUMER_GROUP` (L398-408) + `init_explanation_streams()` (L431-454). **KEEP** `sse_explanation_key`/`sse_context_key`/`inflight_key` (L413-428) — consider renaming class if it no longer holds streams (decide at implementation; low priority).
- [ ] 30s depth ticker in each worker loop: `llm_stream_queue_depth{stream=...}.set(pending_count(...))` — finally populates the dead gauge (new import in `explanation_worker.py`).

**Why:** deletes ~500 lines of hand-rolled PEL machinery; retry semantics map 1:1 onto committed-offset redelivery; every existing external contract (SSE, pub/sub, locks, HTTP codes) preserved bit-for-bit.

### Phase 5 — Forecast batch queue
**Status: `[x]` done (2026-07-04)** — plus a correctness addition: on early-abort the shared consumer is REWOUND to the committed offset (seek), else its in-memory position would silently skip the uncommitted remainder until restart.

**Semantics decision (upheld):** keep drop-with-dedup-TTL, NO retry loop — it's a 5-min-TTL cache warmer; the 600s dedup expiry re-enqueues naturally; retries would burn Gemini quota on stale news.

- [ ] Producer `signal_assembler.py::_enqueue_for_batch_forecast` (L419-475): dedup SET NX EX 600 unchanged → `publish(KafkaTopics.FORECAST_BATCH, payload, key=symbol)` with **added `enqueued_at`** (ISO UTC). Delete the LLEN>batch_size*5→RPOP trim block L463-475 (including its gauge write, per §3.8) — replaced by consumer-side staleness skip (`now − enqueued_at > 3600` → skip+commit). Note producer uses `self._ml_cache` for dedup (stays) but `publish()` for enqueue.
- [ ] `forecast_batch_worker.py`: new module-level `ForecastQueueConsumer` — ONE consumer + ONE `asyncio.Lock` shared by the idle loop and demand flush (same process; avoids rebalance churn from consumer churn). `drain_batch(max_items, timeout_ms)` via `getmany`; **commit only AFTER `_flush_batch` returns** (fixes crash-loses-popped-items §3.7; the deliberate batch-drop on Gemini quota/budget failure still commits — that's the keep-semantics decision). Delete `_QUEUE_KEY`, `_DEDUP_PREFIX`, all lpop/llen.
- [ ] `FORECAST_AUTO_DISPATCH=False` path: don't consume; update `news_forecast_queue_depth` from `pending_count()` on the existing 2s tick (single set-site now).
- [ ] `flush_pending_forecasts(session_factory)` → drain-to-snapshot: batch by `NEWS_FORECAST_BATCH_SIZE`, commit per successful group, raise `RuntimeError` on bad outcome exactly as today — uncommitted remainder stays pending (better than today's loss).
- [ ] `pending_forecast_count()` → `pending_count(topic, group)`. `registry.py:235-240` registration shape unchanged (redis arg stays — used for result-cache `setex` writes only).

### Phase 6 — Event classifier queue
**Status: `[x]` done (2026-07-04)**

- [ ] Producer `event_classifier.py::_enqueue_pending_classification` (L759-787): LPUSH → `publish(KafkaTopics.CLASSIFIER_PENDING, payload, key=str(nlp_result_id))`; payload unchanged (already has `enqueued_at`); never-raises contract preserved.
- [ ] `flush_pending_classifications` — **signature becomes `(self, db_factory)`** (⚠ drops `redis` arg; update BOTH callers: `worker_ai_processing.py:138`, `ai_processing_safety_net.py:250`): on-demand consumer (no standing loop), drain to snapshotted end offsets under a module `asyncio.Lock`, **commit only after successful persist** — fixes LPOP-then-fail item loss. `confidence==0.0` → `RuntimeError` WITHOUT commit → redelivered next dispatch; `UNIQUE(nlp_result_id)` (migration 0049, already written) makes redelivery a harmless in-place upgrade. (Known debt: 0.0 conflates failure with legit zero-confidence, §3.19 — unchanged.)
- [ ] `pending_classification_count()` → `pending_count(...)` (drops redis arg — same two callers). 30-min classification cache stays Redis.

### Phase 7 — Status endpoints, safety net, metrics
**Status: `[x]` done (2026-07-04)** — pending_forecast_count()/pending_classification_count() also drop their redis arg (both callers updated).

- [ ] `worker_ai_processing.py:48-82`: swap count sources to the lag-based functions; response shape `{pending:int, auto_flush:bool}` per category — UNCHANGED → `admin_ai_processing.py` contracts, `WorkerClient`, and `frontend/src/types/ai_processing.ts` all untouched. Dispatch 502 contract (`{"detail":"dispatch_failed","reason":...}`) unchanged.
- [ ] `ai_processing_safety_net.py`: same count-source substitution (+ signature updates from §6); thresholds and per-category isolation unchanged.
- [ ] Metrics: `llm_stream_queue_depth{stream}` fed by the Phase-4 30s tickers; `news_forecast_queue_depth` fed from lag (keeps `GeminiForecastBatchLagging` alert expr `> 20 for 5m` working). Update that alert's annotation text in `monitoring/prometheus/alerts/gemini_quota.yml:98-106` (remediation wording now references Kafka topic/consumer, and mention retry-driven lag inflation per §4). `GeminiAllCircuitsOpen`'s Redis remediation text stays (circuits remain Redis).
- [ ] No custom lag exporter — Redpanda `/public_metrics` covers broker-side (`redpanda_kafka_consumer_group_lag_max/sum` once the cluster property is set, or PromQL `max_offset − committed_offset`).

### Phase 8 — Frontend + Grafana copy
**Status: `[x]` done (2026-07-04)** — Redpanda row added to 05-infrastructure.json (broker up, disk free, group lag, produce rate).

- [ ] `frontend/src/components/admin/AIProcessingQueueCard.tsx`: `CATEGORY_INFO` L28 + L30 and InfoPortal L76-77 — "durable Redis list" → "durable Kafka topic (Redpanda) — survives worker restarts". Sentiment copy (in-process memory) unchanged. NO type changes.
- [ ] Grafana: ⚠ original plan's "update panel descriptions" is a no-op (§3.12). Optional (do it — observability is cheap): one Redpanda row in `05-infrastructure.json` — broker up (`up{job="redpanda"}`), consumer-group lag per group, topic throughput.

### Phase 9 — Cutover script + deploy runbook
**Status: `[x]` done (2026-07-04)** — script written AND runbook EXECUTED (user-authorized): dry-run clean → services stopped → cutover ran (1 DLQ entry moved, queues were empty, streams renamed to backups, marker set) → restart (worker corrected to --workers 1 per sidecar design) → §7 verified.

- [ ] **New** `backend/scripts/migrate_redis_queues_to_kafka.py` following house conventions (module docstring w/ Usage `python -m scripts.migrate_redis_queues_to_kafka` from `backend/`, argparse w/ `--dry-run`, `logging.basicConfig`, `init_redis`/`init_kafka` → work → `finally` close, `sys.exit` codes). Idempotence marker `cortex:migration:kafka_cutover:done` checked first.
  1. Streams: republish PEL entries (`xpending_range` all consumers → `XRANGE id id`, header `attempts = times_delivered − 1`) + never-delivered (`XINFO GROUPS` last-delivered-id → `XRANGE (id +`). Skip ACKed history.
  2. DLQ: `XRANGE - +` → map to the new DLQ JSON shape → publish. Leave `cortex:gemini:dlq:requeued:*` dedup keys alone.
  3. Lists: LPOP-until-empty → publish (forecast items get fresh `enqueued_at` — they predate the field).
  4. `RENAME` drained keys → `cortex:migrated:backup:<name>`; print per-queue counts; set marker.
- [ ] **Deploy runbook** (execute in order; owner runs git/deploy):
  1. ⚑ Add `KAFKA_BOOTSTRAP_SERVERS=localhost:19092` to the live root `.env` (compose services load root `.env` per §3.11; containerized services get `redpanda:9092` via compose `environment:` override).
  2. `docker compose up -d redpanda redpanda-console`; verify `docker exec cortex-redpanda rpk cluster health`.
  3. Stop api + worker — BOTH containers AND bare-metal (⚑ `backend/scripts/stop-all.sh` covers bare-metal; `docker compose stop api worker`).
  4. Run cutover script (first with `--dry-run`).
  5. `docker compose up -d --build worker api frontend prometheus` (and/or bare-metal ⚑ `backend/scripts/start-api.sh` per current mode — bare-metal API reaches broker via `localhost:19092` default).
  6. Verify per §7 below.
  7. Delete `cortex:migrated:backup:*` keys manually after 48h.

### Phase 10 — Tests
**Status: `[x]` done (2026-07-04) — 25 broker-backed integration tests + 4 unit files updated, all green.** ⚠ **owner directive (2026-07-03): full test suite must use REAL data — no mock/synthetic data.** This supersedes the original plan's mocked-only approach. Interpretation (confirm at implementation kickoff): real Redpanda broker + real rows from the dev DB; the **Gemini boundary is still faked** (respx/stub at `llm_client`) so tests don't burn RPD quota — flag to owner if real Gemini calls are actually wanted.

- [ ] **New integration suite** `backend/tests/integration/kafka/` (pytest marker `integration`, skipped automatically when `localhost:19092` unreachable):
  - `test_kafka_core.py` — topic-init idempotence against the real broker, JSON round-trip, header round-trip, `pending_count` arithmetic (incl. never-committed group), producer idempotence config.
  - `test_explanation_flow.py` — publish a job built from a REAL `ai_trading_signals` row (dev DB) → worker loop processes → attempts-header increments on injected rate-limit → DLQ at cap → quota→DLQ+failed-state → crash-before-commit redelivery (kill consumer between process and commit, restart, assert redelivery).
  - `test_context_flow.py` — cooldown republish, recent-key skip, abandon-at-5, `not_before` (≤60s sleep vs >60s republish).
  - `test_dlq_requeue.py` — eligibility by moved_at, Redis NX dedup skip, publish-failure rollback (dedup key deleted, offset not committed).
  - `test_forecast_batch_flow.py` / `test_classifier_flow.py` — real queued payloads from dev DB news/nlp rows; commit-only-after-success (inject failure → assert redelivery on next dispatch; classifier upsert via UNIQUE(nlp_result_id)); staleness skip.
- [ ] **Update existing unit tests** broken by the transport swap (keep house style — `AsyncMock`, scripted `getmany` side-effects, `ensure_future + sleep + shutdown.set` loop pattern): `tests/ai/fusion/test_forecast_batch_worker.py`, `tests/api/test_worker_ai_processing.py` (patch `pending_count`), `tests/workers/test_ai_processing_safety_net.py` (signatures), `tests/ai/intelligence/test_event_classifier.py` (flush tests).
- [ ] Full `pytest backend/tests` green (unit + integration with broker up).

---

## 7. End-to-end verification checklist (post-deploy)

- [x] `docker exec cortex-redpanda rpk cluster health` healthy; `rpk topic list` shows the 5 topics with expected partition counts. (2026-07-04)
- [x] `rpk topic produce cortex.classifier.pending` smoke message → status endpoint showed pending=1 → group seeked past it deliberately (no Gemini call wasted on a synthetic message; offset-advance-on-success is covered by the integration suite). (2026-07-04)
- [~] On-demand explanation via UI → SSE end-to-end: transport path live (workers ready, groups Stable) and covered by integration tests; full browser-driven pass pending next real user session.
- [x] **Crash test:** covered by `test_crash_before_commit_redelivers` against the real broker (consumer killed between read and commit → redelivered to a fresh group member). (2026-07-04)
- [x] Cutover counts: 1 DLQ entry moved (queues empty pre-cutover); 3 backup keys + marker present in Redis. (2026-07-04)
- [x] Prometheus: `redpanda` target up; `news_forecast_queue_depth` + `llm_stream_queue_depth` populated from lag; `redpanda_kafka_consumer_group_lag_*` live for all 5 groups; `GeminiForecastBatchLagging` evaluable. NOTE: prometheus.yml is a single-file bind mount — edits require `docker compose up -d --force-recreate prometheus` (inode pin). (2026-07-04)
- [ ] Watch host RAM for 24h (Redpanda ~1G next to ML-loading workers). *(Owner-owned, from 2026-07-04.)*
- [x] `graphify update .` after all code changes (CLAUDE.md requirement). (2026-07-04)
- [ ] After 48h clean: delete `cortex:migrated:backup:*`. *(Owner-owned, from 2026-07-04.)*

---

## 8. Accepted risks & known debt (sign-off record)

| # | Risk / debt | Mitigation / rationale |
|---|---|---|
| 1 | `not_before` handling head-of-line-blocks a partition ≤60s during rate-limit storms | Bounded by the §4 hybrid rule; 2 partitions halve exposure; escalation path (retry topic) documented, not built. |
| 2 | Retried jobs lose strict FIFO (tail republish) | PEL redelivery was also out-of-order; no consumer is order-sensitive. |
| 3 | At-least-once (crash between Gemini success and commit → duplicate work) | Absorbed by existing DB idempotency + inflight keys + UNIQUE(nlp_result_id) — same guarantees as Redis Streams today. |
| 4 | Lag-based pending counts over-count during retry storms (republish inflates offsets) | Documented in alert annotation; arguably correct (retried = still pending). |
| 5 | Redpanda 1G RAM next to ML workers on one host | `--memory=1G --reserve-memory=0M` hard cap + compose mem limit; watch host RAM post-deploy. |
| 6 | `--overprovisioned` forfeits Redpanda dedicated-host tuning | Accepted for shared single host; revisit if dedicated cores available. |
| 7 | Classifier `confidence==0.0` conflates failure with legit zero-confidence | Pre-existing; unchanged signal; noted for a future fix. |
| 8 | Integration tests need a running broker | Auto-skip marker when unreachable; CI story deferred (no CI today — audit item R2 already deferred). |

---

## 9. Decision log

| Date | Decision | Why |
|---|---|---|
| 2026-07-03 | Redpanda over Kafka/RabbitMQ/Redis-tuning | Kafka API without JVM/ZK; single-node legitimate; Console for ops. (Original scope doc.) |
| 2026-07-03 | One-pass clean cut, no feature flags | Dual-write/flag complexity exceeds the risk at this volume; cutover script + 48h backups cover rollback needs. |
| 2026-07-03 | Broker image `v26.1.12`, NOT `v25.3.15` | Web-verified: 26.1 is the current line; new deployments shouldn't start on a maintenance branch. |
| 2026-07-03 | Production flags, NOT `--mode=dev-container` | Redpanda production checklist forbids `developer_mode`; `--reserve-memory=0M` required in containers. |
| 2026-07-03 | Hybrid `not_before` (sleep ≤60s, else republish) | Never risk `max_poll_interval_ms` eviction (web-verified pitfall of the sleep-in-loop pattern). |
| 2026-07-03 | `delete` retention on all topics, no compaction | Compaction can drop originals when retries reuse keys. |
| 2026-07-03 | Real-data integration tests + updated unit tests | Owner directive; Gemini boundary still faked to protect RPD quota (confirm w/ owner). |
| 2026-07-03 | Plan doc lives in project root for now | Owner will move/tidy root artifacts after implementation. |
| 2026-07-03 | Grafana panel-copy edits dropped from scope | Verified: no dashboard mentions Redis lists/streams; only optional new Redpanda row remains. |

---

## 10. Full file inventory (every file this migration touches)

**New (7):** `backend/app/core/kafka.py` · `backend/scripts/migrate_redis_queues_to_kafka.py` · `backend/tests/integration/kafka/` (5 test modules).
**Modified — backend (12):** `requirements.txt` · `app/core/config.py` · `app/core/redis.py` · `app/main.py` · `app/worker.py` · `app/ai/intelligence/explanation_worker.py` · `app/ai/correlation/engine.py` · `app/api/v1/ai_stream.py` · `app/workers/watchlist_context_scheduler.py` · `app/ai/fusion/signal_assembler.py` · `app/ai/fusion/forecast_batch_worker.py` · `app/ai/intelligence/event_classifier.py`.
**Modified — backend contd (2):** `app/api/worker_ai_processing.py` · `app/workers/ai_processing_safety_net.py`.
**Modified — infra (4):** `docker-compose.yml` · `prometheus.yml` · `backend/.env.example` · `monitoring/prometheus/alerts/gemini_quota.yml`.
**Modified — frontend (1):** `src/components/admin/AIProcessingQueueCard.tsx`.
**Modified — tests (4):** `tests/ai/fusion/test_forecast_batch_worker.py` · `tests/api/test_worker_ai_processing.py` · `tests/workers/test_ai_processing_safety_net.py` · `tests/ai/intelligence/test_event_classifier.py`.
**Optional (1):** `monitoring/grafana/provisioning/dashboards/cortex/05-infrastructure.json` (Redpanda row).
**Unchanged by design:** `admin_ai_processing.py`, `worker_client.py`, `frontend/src/types/ai_processing.ts`, migration 0049 (already written), `registry.py` shape, all SSE/pub-sub/lock code.
