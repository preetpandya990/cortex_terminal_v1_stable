# Cortex AI — Deployment Units

Self-contained systemd units for off-market scheduled operations on the dev box.

## C1 — Scheduled ML challenger retrain

A weekly timer fires the production training orchestrator with `--fresh` and
captures the run as a "challenger" — **no promotion happens automatically**.
An operator reviews the run and promotes manually via
`scripts/promote_model.py`.

### Files

| File | Role |
|---|---|
| `cortex-retrain.timer` | When to run (`OnCalendar=Sat 20:00 Asia/Kolkata`, `Persistent=true`) |
| `cortex-retrain.service` | What to run (`scripts/scheduled_retrain.py --once`, `Type=oneshot`) |

Cadence + paths come from `backend/app/ml/config.py::SCHEDULED_RETRAIN` —
keep the timer's `OnCalendar` line in sync with that dict.

### Install (one-time, requires sudo)

```bash
# 1. Copy the units into systemd's search path.
sudo cp deploy/cortex-retrain.{service,timer} /etc/systemd/system/

# 2. Reload systemd so it picks up the new files.
sudo systemctl daemon-reload

# 3. Enable + start the timer (does NOT trigger the service immediately).
sudo systemctl enable --now cortex-retrain.timer
```

### Verify

```bash
# Confirm the timer is armed and shows the next fire time.
systemctl list-timers cortex-retrain.timer

# Inspect the unit definitions systemd actually loaded.
systemctl cat cortex-retrain.timer cortex-retrain.service
```

### Run a one-shot challenger NOW (without waiting for the cron)

```bash
# Two equivalent options:
sudo systemctl start cortex-retrain.service        # systemd path (logs to journal)
cd backend && .venv/bin/python scripts/scheduled_retrain.py --once   # direct path (logs to stderr + file)
```

Both acquire the same `fcntl.flock` lock — concurrent runs are impossible.

### Dry-run (no orchestrator subprocess, no lock acquired)

```bash
cd backend && .venv/bin/python scripts/scheduled_retrain.py --dry-run
```

Prints the plan + paths and exits.  Safe to run any time, even while a real
retrain is in flight.

### Monitor a run in progress

```bash
# Live tail of the systemd journal for both units.
journalctl -u cortex-retrain.service -u cortex-retrain.timer -f

# Or read the per-run log written by the wrapper itself.
ls -la backend/logs/scheduled_retrain/
tail -f backend/logs/scheduled_retrain/scheduled_retrain_<latest>.log
```

### Pause / disable

```bash
# Stop firing on the timer (in-flight runs continue uninterrupted).
sudo systemctl disable --now cortex-retrain.timer

# Kill an in-flight run (only if absolutely necessary — leaves a partial
# checkpoint that the next --fresh invocation will discard).
sudo systemctl stop cortex-retrain.service
```

### Non-systemd hosts (macOS dev / Docker / etc.)

The same `scheduled_retrain.py` script also has a built-in long-running mode
using APScheduler — no systemd required:

```bash
cd backend && .venv/bin/python scripts/scheduled_retrain.py --schedule
```

Fires per the same `SCHEDULED_RETRAIN` cron expression.  Use this only when
systemd is unavailable; the systemd path is preferred on Linux (survives
reboots, integrates with the journal, doesn't need a babysitter process).

### What this wrapper does NOT do

- It never **promotes**.  Promotion is the operator's call after reviewing
  the challenger run (`scripts/promote_model.py production --version …`).
- It does **not** retry on failure.  A failed retrain is visible in the
  journal + per-run log; the next scheduled fire is independent.
- It does **not** auto-clean old `models/production/` runs.  Future C2 work
  will rotate them as part of the Promotion Report lifecycle.


## Instrument master sync

`instrument_master` is reconciled daily against Upstox's begin-of-day (BOD)
`NSE.json.gz` file.  Unlike the retrain (a systemd timer), this runs **in-process**
inside the API via `InstrumentSyncService` (registered in the FastAPI lifespan),
so there is no systemd unit to install — it starts and stops with the app.

### How it runs

- **Cadence:** daily at `INSTRUMENT_SYNC_HOUR_IST` (default **08:00 IST**) on NSE
  trading days only — after Upstox's ~06:00 refresh, before the 09:15 open.
- **Startup catch-up:** on boot, if the table is empty or its newest watermark is
  older than `INSTRUMENT_SYNC_STALE_HOURS` (default 24h), one sync runs
  immediately (fire-and-forget, so a slow upstream never blocks startup).
- **Conditional GET:** each run sends the last `ETag`/`Last-Modified`; the CDN
  answers `304` when unchanged, so most days do zero database work.
- **Single-runner:** the sync holds a PostgreSQL advisory lock, so concurrent
  workers/replicas never run it twice — only one performs the sync, the rest
  no-op.

### Semantics

- **Soft-delete:** instruments that drop out of the file (delisted / expired) are
  marked `is_active = false` with `delisted_at` set — never hard-deleted, so
  history and foreign references are preserved.  A relisted instrument that
  reappears is reactivated (`is_active = true`, `delisted_at = NULL`).
- **Sanity guard (atomic):** the whole sync is one transaction.  If the file
  holds fewer than `max(INSTRUMENT_SYNC_MIN_INSTRUMENTS, 90% of current active)`
  in-scope instruments, it **rolls back** rather than mass-delist the universe
  from a truncated/corrupt file.
- **Active universe:** consumers that need the live tradeable set filter
  `WHERE is_active`.  Lookups that resolve a *specific* delisted instrument for
  an existing position/suggestion pass `include_inactive=True`.

### Operator commands (manual / on-demand)

The manual entrypoint shares the **exact** fetch + reconcile path as the
scheduler (and takes the same advisory lock, so it cannot race it):

```bash
cd backend
.venv/bin/python scripts/sync_instruments.py              # conditional GET; sync if changed
.venv/bin/python scripts/sync_instruments.py --force      # ignore the 304 cache; full re-sync
.venv/bin/python scripts/sync_instruments.py --dry-run    # report planned changes, write nothing
```

Exit codes: `0` success / nothing to do · `1` advisory lock held elsewhere ·
`2` sanity guard aborted or unexpected error.

### Observability

Prometheus metrics (scraped at `/metrics`):

- `instrument_sync_total{result=success|skipped_304|aborted_sanity|error}`
- `instrument_sync_duration_seconds`
- `instrument_active_count` / `instrument_delisted_count`
- `instrument_sync_last_success_timestamp` — **alert if this falls more than ~26h
  behind now**, which means the daily sync has stopped succeeding.
