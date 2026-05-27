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
