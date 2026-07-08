# Post-Cutover Reminders — Redpanda Migration

> Cutover executed: **2026-07-04 ~12:45 UTC** (~18:15 IST).
> Two time-based tasks remain. Delete this file once both are done.

---

## ☐ 0. Commit the migration — suggested message

```
Migrated all durable job queues from Redis to Redpanda (Kafka), cutover deployed and live

- New app/core/kafka.py transport: 5 topics, manual-commit consumers, attempts/not_before retry headers, lag-based pending counts
- explanation_worker rewritten on Kafka (~500 lines of PEL machinery deleted); forecast + classifier queues migrated
- FIXED: LPOP-then-crash item loss in forecast/classifier flush paths (commit-after-success), dead llm_stream_queue_depth gauge now live
- Infra: redpanda v26.1.12 + console in compose (prod flags, dual listener), prometheus scrape, Grafana Redpanda row
- Cutover script (scripts/migrate_redis_queues_to_kafka.py) executed; Redis backups retained 48h
- Tests: 25 broker-backed integration tests (tests/integration/kafka/) + 4 unit files updated, all green
```

Short version if you prefer a one-liner:
`Redis→Redpanda queue migration: all 5 durable queues on Kafka, LPOP item-loss bugs fixed, cutover deployed and live`

⚠ Note before committing: this sits alongside the earlier uncommitted batch on `main` —
split them if you want clean history (the migration's file list is in
`REDPANDA_MIGRATION_IMPLEMENTATION_REPORT.md` §10).

---

## ☐ 1. Watch host RAM — until ~2026-07-05 (24 h after cutover)

Redpanda runs with a hard 1 G allocation (`--memory=1G`, container capped at 1.25 G)
on the same host as the ML-loading API/worker processes. Confirm the box isn't
under memory pressure with everything running together.

**How to check:**

```bash
# Overall host memory
free -h

# Per-container usage (redpanda should sit ≤ ~1.25G)
docker stats --no-stream cortex-redpanda

# Bare-metal API + worker RSS
ps -o pid,rss,cmd -p $(pgrep -f "uvicorn app.main:app") $(pgrep -f "uvicorn app.worker_app:app")
```

**What "bad" looks like:** swap in heavy use, OOM-killer messages in `dmesg`,
or the Grafana "Redpanda — Queue Broker" row showing the broker restarting.

**If it's tight:** Redpanda's `--memory` can go down to 512M at this message
volume (~low-thousands msgs/day) — edit the flag in `docker-compose.yml` and
`docker compose up -d redpanda`.

---

## ☐ 2. Delete the Redis backup keys — on/after **2026-07-06** (48 h clean operation)

The cutover script renamed the legacy queues instead of deleting them. After
48 h of clean operation (no queue-related incidents), remove them:

```bash
docker exec cortex_merge_ai-ml-redis-1 redis-cli --scan --pattern "cortex:migrated:backup:*" | \
  xargs -r docker exec -i cortex_merge_ai-ml-redis-1 redis-cli DEL
```

Keys this removes (verify with `--scan` first — should be exactly these 3):

- `cortex:migrated:backup:cortex:stream:explanation:jobs`
- `cortex:migrated:backup:cortex:stream:context:jobs`
- `cortex:migrated:backup:cortex:stream:explanation:dlq`

**Do NOT delete** `cortex:migration:kafka_cutover:done` — that marker is what
stops the cutover script from ever running twice.

**Before deleting, a 30-second sanity check that the new stack is clean:**

```bash
docker exec cortex-redpanda rpk cluster health          # Healthy: true
docker exec cortex-redpanda rpk group list              # 5 cortex-* groups, no runaway lag
grep -ci error backend/logs/api.log backend/logs/worker.log
```

---

*Context: `REDPANDA_MIGRATION_IMPLEMENTATION_REPORT.md` §11, `REDPANDA_MIGRATION_MASTER_PLAN.md` §7.*
