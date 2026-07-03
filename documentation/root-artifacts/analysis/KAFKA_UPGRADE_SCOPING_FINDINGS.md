# Kafka Upgrade Scoping — Findings

Scoped assessment of what introducing Kafka would bring to Cortex, including whether Redis could be demoted to pure cache duty. Grounded in a code-level map of current Redis usage plus 2026 web research on Kafka/Redis Streams/Redpanda tradeoffs.

## Current state: Redis is doing three jobs on one instance

Single Redis instance (`docker-compose.yml`, 512MB, `allkeys-lru`) serving:
1. **Cache** — sentiment cache, Gemini RPD budget counters, per-key circuit-breaker state, SSE event stores, worker heartbeat.
2. **Fire-and-forget broadcast pub/sub** (~20 channels, `redis.py:66-367`, namespaces `cai:*`/`cortex:llm:*`) — market tick fan-out (`MARKET_FEED_LTPC/HEALTH`), SSE broadcast (`SIGNALS_*`, `REGIME_*`, `EVENTS_*`, `SAFETY_*`, `MODELS_*`), trade suggestions, correlation lifecycle, Gemini quota reset.
3. **Durable queueing** — Redis Streams (`EXPLANATION_JOBS`, `CONTEXT_JOBS`, `EXPLANATION_DLQ`, consumer group `cortex-explanation-workers`, all logic in `explanation_worker.py:715-2281`) plus two **ad-hoc plain Lists with no retry/backoff**: `cortex:forecast:batch:queue` (`forecast_batch_worker.py`) and `cortex:event:classifier:pending` (`event_classifier.py`).

All app-level locking is Redis `SET NX EX` (no library) — `explanation_worker.py`, `ai_stream.py`, `backfill_service.py`, `fundamentals_*`. (The instrument-sync "advisory lock" is a **Postgres** `pg_try_advisory_lock`, not Redis — confirmed distinct despite similar naming.)

18 background tasks (`workers/registry.py`) run as supervised asyncio coroutines/polling loops inside **one** worker sidecar container — no horizontal fan-out today.

**The fragility**: durable Streams/DLQ/locks share the 512MB LRU-eviction pool with the cache. No `noeviction` carve-out exists — a cache-heavy day could theoretically evict durable work, and nothing in code guards against it.

## The clean split if Kafka is introduced

Only the durable/queue-shaped traffic should move. Broadcast/ephemeral traffic should stay on Redis.

| Stays on Redis (cache/broadcast) | Moves to Kafka (durable/queue) |
|---|---|
| `MARKET_FEED_LTPC/HEALTH` tick fan-out (250ms throttle, WS delivery) | `EXPLANATION_JOBS` / `CONTEXT_JOBS` / `EXPLANATION_DLQ` Streams |
| `SIGNALS_*`, `REGIME_*`, `EVENTS_*`, `SAFETY_*`, `MODELS_*` SSE broadcast | `cortex:forecast:batch:queue` (no retry today) |
| Sentiment cache, Gemini budget/circuit-breaker state | `cortex:event:classifier:pending` (no retry today) |
| SET-NX locks, dedup guards, worker heartbeat | |

"Demoting Redis to cache" doesn't mean removing Redis — Kafka has no KV/cache semantics. It means removing pub/sub-as-queue and Streams-as-queue duty from it, leaving it as a pure cache + real-time broadcast layer, which is what it's actually good at.

## What Kafka would concretely gain

- **Replay** — reprocess a day's events if a classifier model changes or a Gemini outage corrupted a batch. Impossible today: once a Redis list/pub-sub message is delivered, it's gone.
- **True horizontal scaling of the worker sidecar** — Kafka consumer groups let N worker replicas fan out over `EXPLANATION_JOBS`/`CONTEXT_JOBS`/forecast/classifier topics with automatic partition rebalancing, vs. today's single-container asyncio loop.
- **Durability decoupled from cache memory pressure** — Kafka retention is disk-backed and independent of the 512MB LRU pool.
- **One retry/DLQ pattern instead of three** — today there are three different bespoke queue implementations (Streams+consumer-group, two raw lists with zero retry). Kafka topics with a standard consumer-group + DLQ pattern would unify them.

## What should NOT move, and why

Market tick fan-out (`MARKET_FEED_LTPC`) should stay on Redis pub/sub. Benchmarks: Redis Streams/pub-sub ~0.8ms p99 latency vs Kafka's ~12.5ms. Ticks are at-most-once/latest-value-wins by nature — a dropped tick doesn't need replay, the next one supersedes it. Durability here is the wrong property to pay 10-15x latency for.

Cache duty (sentiment, budget counters, circuit-breaker state, locks) stays on Redis — no equivalent in Kafka.

## Ops cost — the honest tradeoff

This runs today as a single docker-compose stack. Self-hosted Kafka is a real new stateful service to operate (Kafka 4.0 removed ZooKeeper via KRaft, but it's still a JVM broker to run). Two ways to reduce that cost:

- **Redpanda** — Kafka-API-compatible single C++ binary, no JVM, ~40% lower memory, much simpler single-node operation alongside the existing compose stack.
- **Managed** (Confluent Cloud / Upstash Kafka / AWS MSK Serverless) — costs money, removes ops burden entirely.

At current scale (a handful of Gemini-bound topics, not hundreds of millions of msgs/day), Redis Streams' own ceiling isn't the actual bottleneck — the bottleneck is that retry/DLQ/consumer-group logic has been hand-rolled three different ways instead of once. That's an argument for standardizing, not necessarily for Kafka specifically. Redpanda gets the same durability/replay/consumer-group model with meaningfully less ops surface than real Kafka.

## Suggested phasing, if pursued

1. Stand up Redpanda (or Kafka) alongside Redis. Don't touch market-tick pub/sub.
2. Migrate the two weakest links first — `forecast_batch_worker` and `event_classifier` list queues — since they have zero retry today. Biggest reliability win for least migration risk.
3. Migrate `EXPLANATION_JOBS`/`CONTEXT_JOBS`/DLQ last — most mature existing implementation, lowest urgency, highest migration effort (consumer-group rebalancing logic in `explanation_worker.py` would need a rewrite).
4. Leave pub/sub broadcast and all cache/lock/budget-counter duty on Redis permanently.

## Redpanda vs Kafka — decision (2026-07-02)

**If a broker is introduced, use Redpanda (Community Edition), not Apache Kafka.** Grounded in a code-level volume/deployment scan plus 2026 web research.

### Measured scale (code-level scan)

- **Total queue volume: hundreds to low-thousands of msgs/day** — three orders of magnitude below where Kafka's strengths appear.
  - `CONTEXT_JOBS`: ≤ ~800/day (watchlist scheduler 4×/NSE day × `WATCHLIST_SCHEDULER_BATCH_CAP=200`, `config.py:323,342`) + on-demand.
  - `EXPLANATION_JOBS`: event-driven, consensus-gated ≥75 (`config.py:306`), stream capped at 5,000.
  - `cortex:forecast:batch:queue`: signal-assembly cadence, batched 5:1 into Gemini (`NEWS_FORECAST_BATCH_SIZE=5`).
  - `cortex:event:classifier:pending`: normally near-empty (auto-dispatch inline by default); fed by RSS polls every 5–15 min.
  - All bounded by NSE market hours (~6.25h) and Gemini quota.
- **Deployment: single-host docker-compose**, 7 services, no resource limits, no k8s/swarm manifests anywhere (compose's "Production: use Kubernetes" comment is aspirational). Worker and API each run `--workers 1`. No Kafka client library in the codebase (`redis[hiredis]==5.2.0` is the sole broker client; Python 3.11).

### Why Redpanda wins the head-to-head at this scale

- **Ops footprint**: single C++ binary + built-in `rpk`, no JVM tuning; designed to run well single-node. A production-ish single Kafka broker wants ~2GB heap + page-cache headroom (~4GB container), and Kafka's own guidance says single-node KRaft is for testing — production wants a 3-controller quorum, which a compose stack would ignore.
- **Performance profile matches**: Redpanda leads on low-partition, latency-sensitive workloads; Kafka only pulls ahead at 6+ partitions and hundreds of thousands of records/sec — irrelevant here.
- **No lock-in**: Kafka-API-compatible, so `aiokafka` + standard consumer-group/DLQ patterns work unchanged; outgrowing it means swapping the broker, not rewriting consumers.
- **Licensing fine**: Community Edition (BSL) is free for internal production use — only reselling it as a managed streaming service is barred; BSL code converts to Apache 2.0 after 4 years. Paid Enterprise features (tiered storage, RBAC, SSO) not needed.

### The no-over-engineering caveat

At ~1,000 msgs/day the defect is not Redis's ceiling — it's (a) three bespoke queue implementations and (b) durable data sharing a 512MB `allkeys-lru` pool. Both fixable with zero new infrastructure:

1. **Split Redis into two instances**: `noeviction` for Streams/locks/DLQ, `allkeys-lru` for cache. Kills the cache-evicts-durable-work fragility outright.
2. **Unify the two raw-list queues onto Redis Streams + consumer groups** using the retry/DLQ pattern already proven in `explanation_worker.py` — one standard pattern instead of three.

What this path cannot provide: **replay** (reprocess history after a model change) and **multi-replica worker fan-out** — those genuinely require a log-based broker.

### Container footprint (either choice = new compose service(s))

Both options mean spinning up new container(s) in `docker-compose.yml` alongside `redis`/`db`; the `api`/`worker` containers don't change count — they just gain an `aiokafka` dependency and a broker-URL env var.

- **Redpanda**: **+1 container** (`redpandadata/redpanda`). One process contains the broker, its Raft controller, Schema Registry, and HTTP proxy. A single node is a first-class, production-legitimate topology (data durable on disk; the only thing a single node can't give is high availability — same HA posture as the current single Redis/Postgres containers). Optional +1 for Redpanda Console (web UI); `rpk` CLI otherwise covers admin.
- **Kafka**: **+1 container minimum** (`apache/kafka`, KRaft mode, broker+controller roles combined in one JVM — no ZooKeeper since 4.0), but upstream guidance treats single-node KRaft as testing-tier; the by-the-book production shape is a **3-controller quorum, i.e. +3 containers**, plus ~2GB JVM heap + page-cache headroom per broker.

This asymmetry — one honest container vs. one compromised container or three proper ones — is a large part of the Redpanda recommendation for a single-host compose stack.

### Decision rule

- Replay + horizontal worker scaling are near-term roadmap → adopt **Redpanda single-node now**, migrate the two no-retry list queues first (per phasing above).
- Not near-term → do the Redis split + Streams unification now; adopt Redpanda later. Nothing is throwaway — the unified consumer pattern maps 1:1 onto Kafka-API consumers.

**Open question before committing**: is production genuinely headed to k8s/multi-node, and when? If a managed platform (MSK / Confluent Cloud) is the eventual home, that timing decides whether self-hosting Redpanda in compose is a stepping stone or a detour.

## Sources

- [Redis Streams vs Kafka: A Detailed Comparison](https://oneuptime.com/blog/post/2026-03-31-redis-streams-vs-kafka-detailed-comparison/view)
- [Redis vs Apache Kafka: How to Choose in 2026 — Better Stack](https://betterstack.com/community/comparisons/redis-vs-kafka/)
- [Apache Kafka Limitations — Redpanda](https://www.redpanda.com/guides/kafka-alternatives-kafka-limitations)
- [Kafka vs Redpanda 2026: The Enterprise Decision Matrix](https://datacouch.io/blog/kafka-vs-redpanda-2026-enterprise-decision-matrix/)
- [Redpanda vs Kafka overview — Redpanda](https://www.redpanda.com/compare/redpanda-vs-kafka)
- [Kafka vs Redpanda: Real Benchmarks on Identical Hardware — ComputingForGeeks](https://computingforgeeks.com/kafka-vs-redpanda-benchmarks/)
- [Redpanda Licenses and Enterprise Features](https://docs.redpanda.com/current/get-started/licensing/overview/)
- [Redpanda is now free and Source Available (BSL)](https://www.redpanda.com/blog/bsl-source-available-license)
- [Redpanda Sizing Guidelines](https://docs.redpanda.com/current/deploy/redpanda/manual/sizing/)
- [Running Apache Kafka KRaft on Docker — Instaclustr](https://www.instaclustr.com/education/apache-spark/running-apache-kafka-kraft-on-docker-tutorial-and-best-practices/)
- [Deploy a Production Kafka Cluster with KRaft — OneUptime](https://oneuptime.com/blog/post/2026-01-25-deploy-production-kafka-cluster-kraft/view)
