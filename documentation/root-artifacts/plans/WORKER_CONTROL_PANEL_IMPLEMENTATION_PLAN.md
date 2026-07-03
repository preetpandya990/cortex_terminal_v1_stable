# Worker Control Panel — Admin UI Implementation Plan

**Date:** 2026-06-30  
**Status:** Planning  
**Scope:** New `/admin/workers` section in the existing admin panel

---

## Context

The backend worker control API is fully implemented (`/api/v1/admin/worker/*`).  
The admin panel has no UI for it. This plan covers the frontend-only work required  
to expose all worker operations to admins without touching the backend.

---

## What Gets Built

A new **Worker Control** admin page at `/admin/workers` that shows all 16 registered  
background tasks with live status, last-run timestamps, crash counts, and per-task  
action buttons (Pause / Resume / Trigger / Restart). A top-level health indicator  
shows whether the worker sidecar is reachable.

---

## Backend API — Already Exists (No Changes Needed)

All routes live under prefix `/api/v1/admin/worker`. Auth: `AdminUserID` (JWT + admin role).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Worker sidecar liveness + uptime |
| GET | `/tasks` | All 16 task states (name, status, last_run_at, crash_count) |
| GET | `/tasks/{name}` | Single task detail |
| POST | `/tasks/{name}/pause` | Pause a task at its next cooperative checkpoint |
| POST | `/tasks/{name}/resume` | Resume a paused task |
| POST | `/tasks/{name}/trigger` | Fire an immediate cycle (bypasses market-hours guards) |
| POST | `/tasks/{name}/restart` | Hard restart a crashed or hung task |

**Task status values:** `starting` · `running` · `paused` · `crashed` · `stopped`

**Degraded mode:** When the worker sidecar is unreachable, all endpoints return  
`503 {"detail": "worker_unavailable", "degraded": true}` — the UI must handle this gracefully.

---

## Registered Tasks (16 total)

**Scheduler / Native loops** (support pause + trigger):
- `heartbeat` · `cache_invalidation` · `suggestion_expiry` · `correlation_engine`
- `fundamentals_refresh` · `watchlist_scheduler`

**Imported loops** (CancelledError-only shutdown, trigger still fires):
- `rss_ingestion` · `event_processing` · `regime_detection` · `drift_detection`
- `safety_monitoring` · `data_ingestion` · `feature_refresh` · `rag_cleanup`

**FSM workers:**
- `pnl_worker` · `sl_tp_worker`

---

## Files to Create

```
frontend/src/
├── types/
│   └── worker_control.ts              # TypeScript types for all API shapes
├── hooks/
│   └── useWorkerControl.ts            # React Query hooks (health + task CRUD)
├── components/admin/
│   ├── WorkerHealthBanner.tsx         # Sidecar reachability + uptime strip
│   ├── TaskCard.tsx                   # Single task: status badge + action buttons
│   └── TasksGrid.tsx                  # Responsive grid of all TaskCards
└── app/admin/workers/
    └── page.tsx                       # Admin page (wires hooks → components)
```

**File to modify:**

```
frontend/src/app/admin/layout.tsx      # Add Workers nav item
```

---

## Implementation Details

### Phase 1 — Types (`worker_control.ts`)

```typescript
export type TaskStatus = 'starting' | 'running' | 'paused' | 'crashed' | 'stopped'

export interface TaskDetail {
  name: string
  status: TaskStatus
  last_run_at: string | null   // ISO-8601 UTC
  crash_count: number
}

export interface WorkerTasksResponse {
  tasks: Record<string, TaskDetail>
}

export interface WorkerHealthResponse {
  status: string
  uptime_seconds: number
  task_count: number
  degraded?: boolean
}
```

---

### Phase 2 — Hooks (`useWorkerControl.ts`)

**Pattern:** React Query v5. Matches existing `useAdminUsers.ts` exactly.

```
useWorkerHealth()         GET /health          refetchInterval: 15_000 (live liveness)
useWorkerTasks()          GET /tasks           refetchInterval: 10_000 (live status)
usePauseTask()            POST /tasks/{n}/pause    mutation → invalidate tasks
useResumeTask()           POST /tasks/{n}/resume   mutation → invalidate tasks
useTriggerTask()          POST /tasks/{n}/trigger  mutation → invalidate tasks
useRestartTask()          POST /tasks/{n}/restart  mutation → invalidate tasks
```

All mutations invalidate the `['worker-tasks']` query key on settle so the grid  
reflects the new state within one poll cycle.

Degraded-mode handling: when the API returns `{ degraded: true }`, surface this  
as a distinct error state (not a standard error) so the UI renders a "Worker  
sidecar unreachable" banner instead of a generic error.

---

### Phase 3 — Components

#### `WorkerHealthBanner`
- Strip at the top of the page
- Green dot + "Worker online · uptime Xh Ym" when healthy
- Red dot + "Worker sidecar unreachable — control operations unavailable" when degraded
- All action buttons in `TaskCard` are disabled when degraded

#### `TaskCard`
- Name (human-readable label, not raw snake_case)
- Status badge with colour coding:
  - `running` → emerald
  - `paused` → amber
  - `starting` → blue
  - `crashed` → rose
  - `stopped` → slate
- Last run: relative time ("3 min ago") with ISO tooltip on hover
- Crash count: shown only when > 0 (rose text)
- Action buttons: context-aware
  - Running → Pause + Trigger + Restart
  - Paused → Resume + Restart
  - Crashed / Stopped → Restart only
  - Starting → all disabled
- Each button shows a spinner on its own pending state (not the whole card)
- `watchlist_scheduler` card gets a highlighted border and a "Warm All Watchlist  
  Context" label on the Trigger button for discoverability

#### `TasksGrid`
- Responsive grid: `grid-cols-1 sm:grid-cols-2 lg:grid-cols-3`
- Grouped sections: "Schedulers & Native" / "Pipeline Workers" / "FSM Workers"
- Last-refreshed timestamp + manual Refresh button in section header

#### `page.tsx`
- Standard admin page shell (matches ML Governance pattern)
- `WorkerHealthBanner` pinned at top
- `TasksGrid` below
- Toast notifications on mutation success/failure (use existing toast pattern)

---

### Phase 4 — Nav Registration (`layout.tsx`)

Add to `NAV_ITEMS`:

```typescript
{ href: '/admin/workers', label: 'Worker Control', icon: Cpu }
```

`Cpu` is available in `lucide-react` (already installed).

---

## Styling Constraints

Matches existing admin panel exactly — no new libraries:

- **Tailwind 4** for all styling (no shadcn/ui)
- **lucide-react** for icons (`Cpu`, `Play`, `Pause`, `RefreshCw`, `RotateCcw`, `CheckCircle2`, `AlertCircle`, `Loader2`)
- **Color palette:** `slate-*` / `emerald-*` / `rose-*` / `amber-*` / `blue-*`
- **Card pattern:** `rounded-xl border border-slate-200 bg-white shadow-sm p-4`
- **Badge pattern:** `inline-flex rounded-full px-2 py-0.5 text-[11px] font-semibold`
- **Button pattern:** `rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-semibold hover:bg-slate-50 transition disabled:opacity-50`

---

## Edge Cases to Handle

| Scenario | Behaviour |
|----------|-----------|
| Worker sidecar down | All action buttons disabled; health banner shows degraded state |
| Mutation in-flight | Only the clicked button shows spinner; others remain interactive |
| Task in `starting` state | All buttons disabled for that card until status resolves |
| Trigger on `watchlist_scheduler` outside market hours | Backend bypasses guard (admin-triggered); UI shows success toast |
| Crash count > 0 | Shown in rose; cleared when task is manually restarted |
| `last_run_at` null | Shows "Never" |

---

## Implementation Order

1. `worker_control.ts` — types first, everything else depends on them
2. `useWorkerControl.ts` — hooks next, before any component touches the API
3. `WorkerHealthBanner.tsx` — simplest component, good smoke test
4. `TaskCard.tsx` — core UI, most logic lives here
5. `TasksGrid.tsx` — composition only, trivial once TaskCard works
6. `page.tsx` — wire everything together
7. `layout.tsx` — add nav item last (one line)

---

## Out of Scope

- Backend changes (all 7 endpoints already exist)
- Worker log streaming / live stdout tailing
- Task scheduling configuration (changing run times from UI)
- Multi-worker support (single sidecar only)
