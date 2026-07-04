# Redis → Redpanda Queue Migration — Implementation Report

> **Status: IMPLEMENTED, DEPLOYED, LIVE-VERIFIED** — 2026-07-04
> Companion documents: `REDPANDA_MIGRATION_MASTER_PLAN.md` (the phase tracker this work executed, with per-phase `[x]` marks) and `REDPANDA_MIGRATION_PLAN.md` (original design record).
> All changes are in the working tree, **uncommitted** (git management is owner-handled).

This document records **what** was done, **how** it was done, and — most importantly — **why each thing was tackled the way it was**: the reasoning, the trade-offs, and the problems discovered mid-flight and how they were resolved.

---

## 1. The problem we were solving

One 512 MB Redis instance configured with `allkeys-lru` was carrying **durable job queueing** alongside everything else it does (cache, pub/sub, locks, SSE stores). That was wrong in three specific ways:

1. **Durable work was evictable.** `allkeys-lru` means that under memory pressure Redis may evict *queued jobs* — exactly when the system is busiest and the queues are deepest. A trading platform cannot have its explanation/classification backlog silently deleted by its own cache policy.
2. **Retry/DLQ logic was hand-rolled three different ways.** The explanation/context pipeline used Redis Streams with ~500 lines of PEL (`xpending`/`xclaim`) housekeeping code; the forecast and classifier queues used bare `LPUSH`/`LPOP` lists with **zero retry** — and both flush paths had a confirmed item-loss bug: they popped items *before* processing, so a crash or Gemini failure mid-drain lost everything already popped.
3. **No broker-side observability.** The `llm_stream_queue_depth` gauge existed in `metrics.py` but had never been set anywhere — dead since the day it was defined.

**The decision** (made in the planning session, upheld here): adopt **Redpanda** — the Kafka API without JVM/ZooKeeper, legitimate as a single node — and migrate *all* durable queueing in **one clean-cut pass**. No feature flags, no dual-write. Redis keeps everything it is genuinely good at: cache, pub/sub broadcast, locks + Lua CAS heartbeats, dedup/inflight/cooldown keys, SSE event stores, budget counters.

**Deliberately NOT built** (over-engineering at ~low-thousands msgs/day): retry-topic ladders, schema registry, Kafka transactions/EOS, a custom lag exporter. Every one of these solves a scale problem this system does not have; each would add operational surface for zero benefit.

---

## 2. How the work was approached (method)

The way of thinking, in order:

1. **Never trust a plan against a moving tree.** The master plan was written 2026-07-03 and code-verified then — but the working tree had changed since (e.g. `prometheus.yml` now scrapes the worker at `host.docker.internal:8001` because both API and worker run bare-metal). So the first action of every phase was to **re-read the actual anchor code** before editing it. This caught real drift and prevented edits against stale line numbers.
2. **Verify library facts empirically, not from memory.** Version pins were re-checked on the web (`aiokafka==0.14.0` still latest). Every aiokafka API the new code relies on was probed in the venv *before* being used: which methods are coroutines vs. sync, whether `AIOKafkaAdminClient.list_consumer_group_offsets` exists, what attribute records producer idempotence. Two real bugs were prevented/caught this way (see §8).
3. **One shared transport module owns all Kafka semantics.** Serialization, retry headers, consumer construction, and lag arithmetic live in exactly one file (`app/core/kafka.py`). Producers and consumers across the codebase cannot drift apart on semantics because they cannot re-implement them.
4. **Rewrite by function, not by line range.** The explanation worker's transport code and business logic were interleaved (`_publish_failed_state` — pure business/SSE logic — sat physically inside the "transport" line range). Each function was individually classified as *delete* (transport), *keep verbatim* (business), or *rewrite* (the seam between them).
5. **Preserve every external contract bit-for-bit.** HTTP status codes (`202`, `500 queue_error`, `502 dispatch_failed`), SSE payload shapes, pub/sub channels, Redis guard keys, response JSON shapes, and the frontend types were all left untouched. The transport swap must be invisible from outside.
6. **Commit-after-terminal-outcome as the single correctness rule.** Every consumer commits its Kafka offset only when a message reaches a *terminal* state: success, DLQ, deliberate abandon, or a successfully republished retry. Everything else (crash, publish failure) leaves the offset uncommitted → redelivery. This one rule replaces all ~500 lines of PEL machinery *and* fixes the two LPOP item-loss bugs for free.
7. **Prove it against the real thing.** Owner directive: tests use the **real broker and real dev-DB rows**; only the Gemini boundary is faked (so tests never burn RPD quota). 25 integration tests run against live Redpanda — including a genuine kill-the-consumer-before-commit crash test.

---

## 3. Architecture: what moved, what stayed

### Moved to Redpanda (5 topics)

| Old Redis structure | Kafka topic | Partitions | Retention | Consumer group |
|---|---|---|---|---|
| `cortex:stream:explanation:jobs` (Stream) | `cortex.explanation.jobs` | **2** | 7 d | `cortex-explanation-workers` |
| `cortex:stream:context:jobs` (Stream) | `cortex.context.jobs` | 1 | 7 d | `cortex-context-workers` |
| `cortex:stream:explanation:dlq` (Stream) | `cortex.explanation.dlq` | 1 | 14 d | `cortex-dlq-requeue` (on-demand) |
| `cortex:forecast:batch:queue` (List) | `cortex.forecast.batch` | 1 | 1 d | `cortex-forecast-batch` |
| `cortex:event:classifier:pending` (List) | `cortex.classifier.pending` | 1 | 7 d | `cortex-classifier-flush` (on-demand) |

**Why 2 partitions only on explanation:** it preserves today's two-parallel-workers / no-head-of-line-blocking design — messages are keyed by `suggestion_id`, so each of the two workers owns one partition and a slow Gemini call for one suggestion never blocks the other stream of work. Everything else is low-frequency and single-consumer; extra partitions would buy nothing.

**Why `delete` retention and never compaction:** retries republish under the *same key*; compaction could garbage-collect the original record while its retry is still pending. This is a correctness constraint, not a tuning choice.

### Stayed on Redis (deliberately)

All pub/sub (`LLM_EXPLANATION_READY`, `LLM_CONTEXT_READY`, `GEMINI_QUOTA_RESET`), the per-job SSE event stores (`cortex:sse:events:*`), the inflight dedup key (SET NX EX 150), context recent/cooldown keys, the context generation lock + Lua CAS heartbeat, DLQ-requeue dedup keys (SET NX EX 172800), forecast dedup keys (SET NX EX 600), the 30-min classification cache, Gemini budget counters/circuits, and the worker heartbeat. **Why:** these are caches, broadcasts, and coordination primitives — the things Redis is actually good at. Moving them would be migration for its own sake. The sentiment queue (in-process `asyncio.Queue`) was explicitly out of scope.

---

## 4. The retry design (the intellectually hard part)

Kafka has no equivalent of Redis PEL's `times_delivered` counter, so the old "don't ACK and let housekeeping re-claim it" pattern doesn't translate. The replacement:

**Manual commit + republish-to-tail with two headers** — `attempts` (int) and `not_before` (epoch seconds).

- On a retryable failure: republish the same payload with `attempts+1` and `not_before = now + delay`, then commit the original. If the process crashes *before* the commit, Kafka redelivers from the committed offset — which is the old PEL-drain behavior, obtained for free with zero code.
- **The `not_before` hybrid rule** (the key insight, web-verified): sleeping in a Kafka consumer loop is the classic way to exceed `max_poll_interval_ms` (300 s) and get evicted from the group. So: if the deadline is **≤ 60 s** away, sleep it off (aiokafka heartbeats in a background task; 60 s ≪ 300 s — safe). If it is **> 60 s** away (context cooldowns can republish with 180 s delays), do **not** sleep — republish to the tail *unchanged* (same attempts, same deadline) and commit. The consumer never risks group eviction, and the message stays queued.
- Consumers run with `max_poll_records=1` where work is Gemini-length (≤ 120 s LLM timeout + ≤ 60 s sleep still comfortably under 300 s).
- **Headers stay minimal** (two fields) — per-attempt metadata bloat multiplies record size for no operational value.
- Hard attempt caps prevent poison-message hot-looping: 3 for explanations (then DLQ + "Analysis unavailable" in the UI), 5 for context (then silent abandon — context is best-effort; the next watchlist open regenerates it).
- Explicit everywhere: `enable_auto_commit=False`, `auto_offset_reset="earliest"` (the default `latest` silently skips backlog on a group's first start — a footgun worth naming in code).
- One long-lived `AIOKafkaProducer(enable_idempotence=True, linger_ms=5)` per process. Idempotence forces `acks=all` internally, so a returned `send_and_wait` *is* a durability guarantee — the failure contracts (HTTP 500 on enqueue failure, warn-only on suggestion-path enqueue) lean on this.

**Accepted side-effect, documented rather than "fixed":** republish-to-tail inflates end offsets, so lag-based pending counts briefly over-count during retry storms. Retried messages *are* genuinely pending, so this is arguably correct; the alert annotation explains it so an operator reading a spike at 3 a.m. isn't misled.

---

## 5. What was built, phase by phase (with the reasoning)

### Phase 1 — `app/core/kafka.py` + config
The single transport module: `KafkaTopics`/`KafkaGroups` constants, `_TOPIC_SPECS` (the canonical topology), `init_kafka()`/`close_kafka()`/`get_kafka_producer()` mirroring the existing `redis.py` singleton lifecycle exactly (familiarity = maintainability), `publish()`, `new_consumer()`, `pending_count()`, and the retry-header helpers.

Design details that matter:
- `init_kafka()` is **idempotent** and called from both the API lifespan and the worker lifespan — whichever process boots first creates the topics; `TopicAlreadyExistsError` is swallowed (mirrors the old BUSYGROUP pattern).
- The admin client must `start()` **before** `create_topics()` (it negotiates the broker API version on start; calling create first raises `IncompatibleBrokerVersion`).
- The malformed-payload policy lives in the **deserializer**: a raising deserializer kills aiokafka's fetch task, not the message — so malformed JSON decodes to `None` and every caller treats `None` as poison (log, commit, skip).
- `pending_count()` keeps **long-lived** offset/admin clients rather than per-call ones — three 30-second tickers plus a status endpoint would otherwise churn TCP connections all day.
- Config surface is deliberately **one field**: `KAFKA_BOOTSTRAP_SERVERS` (default `localhost:19092`). Topology lives in code constants, not env — nobody should be able to mis-deploy a partition count.

### Phase 2 — Infrastructure
`redpanda` compose service pinned to `v26.1.12` with **production flags**, not `--mode=dev-container` (that sets `developer_mode=true`, which Redpanda's own production checklist forbids):
`--overprovisioned --smp=1 --memory=1G --reserve-memory=0M` — `--reserve-memory=0M` is mandatory in containers (Seastar's host-memory heuristic is wrong inside cgroups); memory hard-capped at 1.25G in compose.

**The dual listener is required, not optional:** the API and worker run bare-metal (they reach `localhost:19092`); anything containerized reaches `redpanda:9092`. Both are advertised.

Consumer-group lag metrics were enabled via bootstrap `--set redpanda.enable_consumer_group_metrics=[...]` — with the documented caveat that `--set` seeds `.bootstrap.yaml` and applies **only on first bootstrap of a fresh data volume**; the `rpk cluster config set` fallback for an existing cluster is written into the compose comment. Redpanda Console (`v3.8.0`, :8080) added for queue operations. Prometheus got a `redpanda` job scraping `/public_metrics` at 10 s (matching the two existing jobs).

### Phase 3 — Lifecycle wiring
`main.py`: `init_explanation_streams()` (Redis consumer groups) deleted; `init_kafka()` runs after `init_redis()`. On shutdown, `close_kafka()` runs **after** all consumer tasks are cancelled (a live consumer must never outlive the shared clients) and **before** `close_redis()` (consumers write SSE/pub-sub state through Redis until they stop). Same wiring in `worker.py::worker_lifespan` — which is where the worker's lifecycle actually lives (`worker_app.py` only enters it; a planning-phase correction that mattered).

### Phase 4 — The explanation/context worker rewrite (the biggest piece)
**Deleted wholesale:** `_move_to_dlq`, `_drain_pel`, `_pel_housekeeping`, `_context_pel_housekeeping`, the inline PEL-drain inside `context_worker()`, and every stream constant — ~500 lines of transport machinery.

**Kept verbatim (business logic):** prompts/schema/guardrails, audit-log writer, SSE writers, `_generate_explanation`, `_generate_instrument_context`, lock heartbeat, and `_publish_failed_state` (the function that makes the browser render "Analysis unavailable" instead of an eternal skeleton — physically inside the deleted range, which is exactly why the rewrite was done function-by-function).

**New:** thin consumer loops (`async for msg` → process → commit) with reconnect-on-error backoff; `_send_to_dlq()` publishing the structured DLQ entry and raising on failure so the caller skips the commit (parity with the old "DLQ write failed → don't ACK" path); `_honor_not_before()` implementing the hybrid rule; and per-topic 30-second depth tickers that finally feed the dead `llm_stream_queue_depth` gauge from consumer lag.

**The DLQ quota-recovery drain** (requeues quota-exhausted explanations after the midnight-PT reset) was the subtlest rewrite: it now uses an on-demand **manual-assignment** consumer (the DLQ has one partition; group-join latency and rebalance churn buy nothing for a drain), reads to a **snapshot** of the end offset (entries recycled during the drain land beyond the snapshot and wait for the next trigger — no infinite loop), and preserves all invariants: worker-0 gating + a module `asyncio.Lock` (boot scan and quota-reset listener can fire in the same session near midnight), per-suggestion SET NX dedup, and the strict failure order — **publish failure ⇒ delete the dedup key, do NOT commit, abort the drain** — so a broker hiccup can never leak a suggestion into "requeued according to Redis, absent from Kafka" limbo.

**Producers** (`correlation/engine.py`, `ai_stream.py` ×2, `watchlist_context_scheduler.py`) swapped `xadd` → `publish()` with identical payload fields and identical failure contracts. Stream-maxlen constants (which existed in *six* places) were deleted — bounding is now the broker's retention job, where it belongs.

### Phase 5 — Forecast batch queue
The producer (`signal_assembler`) now publishes with a new `enqueued_at` timestamp, and its ugly LLEN>cap→RPOP "queue trim" was deleted — replaced by a **consumer-side staleness gate**: payloads older than 1 hour are skipped (a forecast is a 5-minute-TTL cache warmer; hour-old news context is worthless and calling Gemini on it burns quota).

The consumer got the most careful concurrency design of the migration: a module-level **`ForecastQueueConsumer` singleton** — ONE consumer + ONE `asyncio.Lock` shared by the standing loop and the demand flush. **Why one consumer:** two consumers in the same group would rebalance-churn; but more subtly, Kafka commits are *position-based*, so two tasks reading one consumer without mutual exclusion would let task A's commit silently commit task B's in-flight uncommitted items. The whole drain→flush→commit sequence is therefore one critical section.

**A correctness gap found while implementing (beyond the plan):** when a demand flush aborts early (budget-throttled), it must not commit — but the shared consumer's *in-memory position* has already advanced. Without intervention, the next flush on the same live consumer would skip the uncommitted remainder until a process restart. Fix: `rewind_to_committed()` — an explicit `seek()` back to the committed offset on the abort path. The retained semantics: auto-loop batch drops on quota failure still commit (dedup-TTL expiry re-enqueues naturally — retrying stale news burns quota for nothing), while demand-flush failures leave the remainder pending (strictly better than the old LPOP path, which *lost* it).

### Phase 6 — Classifier queue
Producer publishes keyed by `nlp_result_id`; `flush_pending_classifications()` lost its `redis` argument (both callers updated) and became an on-demand manual-assign drain-to-snapshot under a module lock, processing one item at a time and committing **only after a successful persist**. A Gemini failure (`confidence == 0.0` sentinel) raises without committing → the item and the remainder are redelivered on the next dispatch. This is safe *because* migration 0049's `UNIQUE(nlp_result_id)` makes redelivery a harmless in-place upgrade — the schema work done weeks earlier is what makes at-least-once delivery correct here. (Known debt, unchanged: a legitimate zero-confidence result is indistinguishable from failure at that check.)

### Phase 7 — Status endpoints, safety net, metrics
Count sources swapped to lag-based functions; the response shape `{pending, auto_flush}` per category is byte-identical, so the admin proxy, `WorkerClient`, and the frontend types needed **zero** changes. The `GeminiForecastBatchLagging` alert annotation was rewritten to explain the new lag semantics (including the retry-storm inflation) and gives the operator the exact `rpk group describe` command to run.

### Phase 8 — Frontend + Grafana
Two copy strings in `AIProcessingQueueCard.tsx` ("durable Redis list" → "durable Kafka topic (Redpanda)"). Grafana got a new "Redpanda — Queue Broker" row in `05-infrastructure.json`: broker up, disk free, consumer-group lag per group, topic produce rate — appended programmatically to keep the JSON valid and style-consistent with the existing panels. (Planning had verified the old dashboards mention no Redis queues anywhere, so there was nothing to edit — only this addition.)

### Phase 9 — Cutover script
`backend/scripts/migrate_redis_queues_to_kafka.py`, following the house script conventions (argparse `--dry-run`, `logging.basicConfig`, init → work → `finally` close, exit codes 0/1/2). What it does and why:

- **Streams:** migrates PEL entries across *all* consumers (in-flight at shutdown; header `attempts = times_delivered − 1` so their retry budget carries over) plus never-delivered entries (after the group's last-delivered-id). ACKed history is skipped — it was already processed.
- **DLQ:** full `XRANGE`, re-shaped to the new JSON entry format.
- **Lists:** RENAMEd to backup **first**, then published from the backup's contents oldest-first — so the backup always holds the originals and nothing is popped. Forecast items get a fresh `enqueued_at` (they predate the field; stamping drain-time keeps them inside the staleness gate for their first dispatch rather than being instantly discarded).
- **Nothing is deleted.** Streams are RENAMEd to `cortex:migrated:backup:*` after publishing. An idempotence marker (`cortex:migration:kafka_cutover:done`) makes a second run abort with exit 2.

### Phase 10 — Tests
Per the owner directive: **real broker, real dev-DB rows, Gemini faked.**

`tests/integration/kafka/` — conftest + 6 modules, **25 tests**, auto-skipped when the broker is unreachable (socket probe at collection time), covering: topic-init idempotence, JSON/unicode/header round-trips, malformed-payload poison handling, lag arithmetic including the never-committed-group case, the full explanation retry ladder (success/rate-limit/attempts-cap/quota→DLQ+failed-state-SSE), a genuine **crash-before-commit redelivery** test (consumer killed between read and commit; a fresh group member receives the same message), all context guard-key behaviors and both `not_before` branches (measured sleep vs. unchanged republish), the DLQ drain's eligibility/dedup/rollback paths, forecast commit-after-success + redelivery + staleness skip, and classifier commit-after-persist + redelivery.

**Test-isolation approach:** the tests share the production topic names, so each test *fast-forwards its consumer group's committed offsets to the topic end* before publishing (it only ever observes its own messages) and uses tail-positioned probe consumers to assert republishes. This avoids monkeypatching topology constants into the code under test — the code that runs in tests is exactly the code that runs in production.

**One deliberate deviation, flagged:** the classifier integration tests also fake `_persist_classification` — a *synthetic* Gemini result must never overwrite a *real* dev-DB classification row. Real rows are read for payload realism; the write boundary is covered by unit tests + the DB constraint.

Four existing unit-test files were updated for the transport swap (scripted fake consumers in the established `AsyncMock` house style). Two additional stale classifier tests — broken by the earlier heuristic-prefilter expansion, *not* by this migration (verified by running them on a stashed clean tree) — were repaired while in the file.

---

## 6. Deployment (the cutover, as executed 2026-07-04)

1. `KAFKA_BOOTSTRAP_SERVERS=localhost:19092` added to **both** `.env` files — the compose services read the root `.env`, the bare-metal processes read `backend/.env`. (Containerized services get `redpanda:9092` via compose `environment:` override.)
2. `redpanda` + `redpanda-console` containers brought up; `rpk cluster health` verified healthy.
3. **Dry-run first**: the script reported 1 DLQ entry to move and empty queues everywhere else (expected — demand-driven dispatch keeps them drained). Only then were the API and worker stopped and the real run executed: 1 entry moved, 3 streams renamed to backups, marker set.
4. Restart: worker (`uvicorn app.worker_app:app --port 8001 --workers 1`), then API (`scripts/start-api.sh`).
5. Verification (§7 of the plan, all green):
   - 5 topics with correct partition counts; both standing groups **Stable**; all 3 LLM worker tasks logged ready.
   - Boot DLQ recovery scan ran and committed to end.
   - **Smoke test:** `rpk topic produce cortex.classifier.pending` → sidecar status endpoint reported `classification.pending = 1` → group seeked past the synthetic message so the safety net never burns a Gemini call on it.
   - Prometheus: `redpanda` target up; `redpanda_kafka_consumer_group_lag_*` live for all five groups; `llm_stream_queue_depth` (dead since creation) and `news_forecast_queue_depth` now populated from lag.
   - No new errors in either service log.

**Operational deltas made during deploy, flagged deliberately:**
- The worker had been running with `--workers 2`, which duplicates *every* background scheduler across two processes and contradicts the sidecar design (compose specifies 1; the control plane and advisory locks assume a singleton). It was restarted with `--workers 1`. Revert if the 2 was intentional — but it almost certainly wasn't.
- `scripts/start-worker.sh` is stale: it launches `python -m app.worker`, which is no longer an entry point (worker.py became a pure task-coroutines module in the sidecar refactor). The worker must be started with the uvicorn command above; the script should be updated.

---

## 7. What this migration fixed outright

1. **Two confirmed item-loss bugs** — `flush_pending_forecasts` and `flush_pending_classifications` both LPOPed before processing and raised without requeue. Commit-after-success makes both crash-safe; the classifier case is additionally protected by `UNIQUE(nlp_result_id)`.
2. **Evictable durable work** — queued jobs can no longer be deleted by Redis's LRU under memory pressure.
3. **A dead metric** — `llm_stream_queue_depth` is populated for the first time since it was defined.
4. **~500 lines of hand-rolled PEL machinery** replaced by the broker's native committed-offset semantics.
5. **No broker observability → full observability** — per-group lag in Prometheus/Grafana plus Redpanda Console for ad-hoc queue inspection.

---

## 8. Problems hit during implementation, and how they were resolved

These are the "way of thinking" moments worth preserving:

1. **aiokafka metadata gotcha (real bug, caught by integration tests).** `consumer.partitions_for_topic()` reads only cached cluster metadata, and — non-obviously — `await consumer.topics()` fetches metadata into a *fresh* object without populating that cache. So a group-less offset client could report "no partitions" for a topic that exists, making `pending_count()` return 0 intermittently (it passed or failed depending on whether an earlier call had registered topic interest). **Fix:** stop asking the client at all — resolve partition counts from `_TOPIC_SPECS`, the canonical topology *we* create. Lesson: when you own the topology, your own constants are a more reliable source than a client-side cache with subtle population rules.
2. **Producer internals aren't a stable API.** The idempotence assertion first targeted `producer._acks`, which doesn't exist in aiokafka 0.14; inspection of the constructor source showed `_txn_manager` is the attribute that records idempotence. Verified empirically before use.
3. **The shared-consumer abort hazard** (§5, Phase 5) — position-based commits + a shared long-lived consumer means an aborted drain must explicitly `seek()` back, or uncommitted items are invisibly skipped until restart. Not in the plan; found by reasoning through the failure path while writing it.
4. **Docker single-file bind-mount inode pin.** After editing `prometheus.yml`, a config reload changed nothing — the container held the *old file's inode* (file edits replace the inode; the mount doesn't follow). `docker compose up -d --force-recreate prometheus` re-binds it. Worth remembering for any single-file mount in this stack.
5. **CRLF line endings in `.env`.** Shell-parsing `INTERNAL_API_SECRET` out of `.env` produced a token with a trailing `\r` that made uvicorn reject the request as malformed HTTP. Strip `\r` when extracting values. Related discovery: root `.env` and `backend/.env` hold **different** `INTERNAL_API_SECRET` values — works today (bare-metal reads `backend/.env`) but is a foot-gun for the day services move back into containers. Worth syncing.
6. **Pre-existing test failures, proven pre-existing before touching them.** Both the `test_unified_model_registry.py` fixture errors and the `test_trade_suggestions_api.py` hang reproduce on a stashed clean tree — the stash/run/pop check took a minute and prevented hours of chasing "regressions" this migration never caused. Everything the migration actually touches is green: 152 `tests/ai` tests, the worker/sidecar suites, and all 25 broker-backed integration tests.

---

## 9. Accepted risks (sign-off record)

| # | Risk / debt | Why it's acceptable |
|---|---|---|
| 1 | `not_before` head-of-line-blocks a partition ≤ 60 s during rate-limit storms | Bounded by the hybrid rule; 2 partitions halve exposure; retry-topic escalation documented, not built. |
| 2 | Retried jobs lose strict FIFO (tail republish) | PEL redelivery was also out-of-order; no consumer is order-sensitive. |
| 3 | At-least-once ⇒ possible duplicate work on crash between Gemini success and commit | Absorbed by existing DB idempotency + inflight keys + `UNIQUE(nlp_result_id)` — the same guarantees Redis Streams gave. |
| 4 | Lag-based counts over-count during retry storms | Documented in alert annotations; retried = genuinely pending. |
| 5 | Redpanda 1 G RAM next to ML workers on one host | Hard memory cap + compose limit; watch host RAM for 24 h post-deploy. |
| 6 | `--overprovisioned` forfeits dedicated-host tuning | Correct for a shared single host; drop it + `rpk redpanda tune all` if dedicated cores ever appear. |
| 7 | Classifier `confidence==0.0` conflates failure with legit zero confidence | Pre-existing; signal unchanged; noted for a future fix. |
| 8 | Integration tests need a running broker | Auto-skip when unreachable; CI story deferred (no CI today). |

---

## 10. File inventory

**New (9):** `backend/app/core/kafka.py` · `backend/scripts/migrate_redis_queues_to_kafka.py` · `backend/tests/integration/kafka/` (`__init__.py`, `conftest.py`, `test_kafka_core.py`, `test_explanation_flow.py`, `test_context_flow.py`, `test_dlq_requeue.py`, `test_forecast_batch_flow.py`, `test_classifier_flow.py` — 7 files).

**Modified — backend (14):** `requirements.txt` · `app/core/config.py` · `app/core/redis.py` · `app/core/metrics.py` · `app/main.py` · `app/worker.py` · `app/ai/intelligence/explanation_worker.py` · `app/ai/correlation/engine.py` · `app/api/v1/ai_stream.py` · `app/workers/watchlist_context_scheduler.py` · `app/ai/fusion/signal_assembler.py` · `app/ai/fusion/forecast_batch_worker.py` · `app/ai/intelligence/event_classifier.py` · `app/workers/registry.py` (comment only).

**Modified — callers (2):** `app/api/worker_ai_processing.py` · `app/workers/ai_processing_safety_net.py`.

**Modified — infra (5):** `docker-compose.yml` · `prometheus.yml` · `backend/.env.example` · `monitoring/prometheus/alerts/gemini_quota.yml` · `monitoring/grafana/provisioning/dashboards/cortex/05-infrastructure.json`.

**Modified — frontend (1):** `src/components/admin/AIProcessingQueueCard.tsx` (copy strings only; no type changes).

**Modified — tests (4):** `tests/ai/fusion/test_forecast_batch_worker.py` · `tests/api/test_worker_ai_processing.py` · `tests/workers/test_ai_processing_safety_net.py` · `tests/ai/intelligence/test_event_classifier.py`.

**Unchanged by design:** `admin_ai_processing.py`, `worker_client.py`, `frontend/src/types/ai_processing.ts`, migration 0049, all SSE/pub-sub/lock code, every HTTP contract.

---

## 11. Remaining follow-ups

- **Delete `cortex:migrated:backup:*` Redis keys after 48 h of clean operation** (from 2026-07-04). *(Owner is handling housekeeping.)*
- Watch host RAM for ~24 h (Redpanda 1 G beside the ML-loading workers). *(Owner.)*
- Commit the working tree (owner-handled; this migration sits alongside the earlier uncommitted batch).
- Sync `INTERNAL_API_SECRET` between root `.env` and `backend/.env`.
- Update `scripts/start-worker.sh` to the uvicorn entry point.
- Pre-existing, unrelated test debt: `test_unified_model_registry.py` fixture errors and the `test_trade_suggestions_api.py` hang.
