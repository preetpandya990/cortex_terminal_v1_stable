# Suggestion Expiry Worker - Quick Reference Card

**Version:** 1.0.0 | **Updated:** April 22, 2026

---

## At a Glance

| Property | Value |
|----------|-------|
| **Function** | `expiry_loop()` in `backend/app/worker.py` |
| **Cadence** | 60 seconds |
| **Batch Size** | 100 suggestions per cycle |
| **Throughput** | 100 suggestions/min (default) |
| **Resource Usage** | <1% CPU, ~10MB memory |
| **Dependencies** | PostgreSQL, Redis |

---

## Quick Commands

### Check Worker Status

```bash
# Is worker running?
ps aux | grep "python.*worker.py"

# Check worker logs
tail -f /var/log/cortex/worker.log | grep Expiry

# Check heartbeat
redis-cli GET worker:heartbeat
```

### Check Expiry Backlog

```sql
-- How many suggestions need expiry?
SELECT COUNT(*) FROM trade_suggestions
WHERE status = 'active' AND expires_at <= NOW();

-- Show oldest expired suggestions
SELECT suggestion_id, symbol, expires_at, 
       NOW() - expires_at AS overdue
FROM trade_suggestions
WHERE status = 'active' AND expires_at <= NOW()
ORDER BY expires_at ASC
LIMIT 10;
```

### Check Metrics

```bash
# Expiry rate (last 5 minutes)
curl -s http://localhost:8000/metrics | grep suggestions_expired_total

# Worker loop iterations
curl -s http://localhost:8000/metrics | grep worker_loop_iterations_total | grep suggestion_expiry
```

### Manual Expiry (Emergency)

```sql
-- Manually expire all stale suggestions
UPDATE trade_suggestions
SET status = 'expired', updated_at = NOW()
WHERE status = 'active' AND expires_at <= NOW();
```

---

## Monitoring Checklist

### Daily Checks

- [ ] Worker heartbeat age < 60s
- [ ] Expiry backlog < 100 suggestions
- [ ] No errors in worker logs
- [ ] Prometheus metrics updating

### Weekly Checks

- [ ] Review expiry rate trends in Grafana
- [ ] Check average cycle duration (<100ms)
- [ ] Verify Redis pub/sub delivery rate
- [ ] Review error logs for patterns

---

## Troubleshooting

### Problem: Suggestions Not Expiring

**Quick Fix:**
```bash
# Restart worker
systemctl restart cortex-worker

# Check logs
journalctl -u cortex-worker -f | grep Expiry
```

### Problem: High Backlog (>1000)

**Quick Fix:**
```python
# Temporarily increase batch size
# Edit backend/app/worker.py, line ~XXX:
.limit(500)  # Change from 100 to 500

# Restart worker
systemctl restart cortex-worker
```

### Problem: Redis Pub/Sub Failures

**Quick Fix:**
```bash
# Check Redis
redis-cli PING

# Verify connection
redis-cli INFO clients

# Non-blocking - metrics still work!
```

---

## Prometheus Queries

```promql
# Expiry rate (per second)
rate(suggestions_expired_total[5m])

# Expiry by direction
sum by (direction) (rate(suggestions_expired_total[5m]))

# High confidence expiry rate
rate(suggestions_expired_total{confidence_level="HIGH"}[5m])

# Worker loop duration
worker_loop_duration_seconds{loop_name="suggestion_expiry"}
```

---

## Alert Rules

```yaml
# High expiry rate
- alert: HighExpiryRate
  expr: rate(suggestions_expired_total[5m]) > 10
  for: 5m

# Worker not running
- alert: WorkerDown
  expr: worker_heartbeat_age_seconds > 120
  for: 2m

# High backlog
- alert: HighExpiryBacklog
  expr: suggestions_active{status="active"} > 1000
  for: 10m
```

---

## Configuration

**No environment variables needed!** Uses existing settings:

```bash
ENABLE_BACKGROUND_TASKS=true
WORKER_SHUTDOWN_TIMEOUT=30
DATABASE_URL=postgresql+asyncpg://...
REDIS_URL=redis://...
```

---

## Performance Tuning

| Scenario | Cycle Interval | Batch Size | Throughput |
|----------|----------------|------------|------------|
| Low load | 60s | 100 | 100/min |
| Medium load | 60s | 100 | 100/min |
| High load | 30s | 200 | 400/min |
| Very high load | 30s | 500 | 1000/min |

**To change cycle interval:**
```python
# In expiry_loop(), modify timeout:
await asyncio.wait_for(shutdown_event.wait(), timeout=30.0)
```

**To change batch size:**
```python
# In expiry_loop(), modify LIMIT:
.limit(200)
```

---

## Key Metrics

| Metric | Target | Alert Threshold |
|--------|--------|-----------------|
| Expiry rate | <5/sec | >10/sec |
| Cycle duration | <100ms | >500ms |
| Backlog size | <100 | >1000 |
| Worker heartbeat | <60s | >120s |

---

## Emergency Contacts

**On-Call Engineer:** [Your Team]  
**Escalation:** [Platform Team Lead]  
**Documentation:** `backend/SUGGESTION_EXPIRY_WORKER_GUIDE.md`

---

**Print this card and keep it handy!** 📋
