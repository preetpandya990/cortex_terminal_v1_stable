# Worker Control Panel — Action Progress Bar Plan

**Date:** 2026-06-30  
**Status:** Planning (interrupted, not started)  
**Scope:** Enhancement to `TaskCard.tsx` + one line in `globals.css`

---

## What Gets Built

After clicking any action button (Pause / Resume / Trigger / Restart), each `TaskCard` shows:
- A **colored indeterminate progress bar** at the bottom of the card
- A **small action label** above it ("Pausing task…", "Restarting task…", etc.)

The bar auto-clears when either:
1. The task's `status` changes on the next poll (confirms the action landed)
2. A 6-second timeout elapses (covers Trigger, which doesn't change status)

No backend changes. No new files. Pure frontend.

---

## Research Already Done

### Animation
- `globals.css` already has `@keyframes shimmer` (translateX -100% → 100%) used by skeleton screens
- Need to add one new keyframe: `@keyframes progress-slide` — a narrower inner bar (40% width) sliding left to right inside an `overflow-hidden` container
- Available via Tailwind's arbitrary animation: `animate-[progress-slide_1.5s_ease-in-out_infinite]`

```css
/* Add to globals.css after existing shimmer keyframe */
@keyframes progress-slide {
  0%   { transform: translateX(-250%); }
  100% { transform: translateX(500%);  }
}
```

### Color coding per action
| Action  | Bar color         | Label              |
|---------|-------------------|--------------------|
| pause   | `bg-amber-400`    | Pausing task…      |
| resume  | `bg-emerald-400`  | Resuming task…     |
| trigger | `bg-blue-400`     | Triggering cycle…  |
| restart | `bg-slate-400`    | Restarting task…   |

### State management in `TaskCard`
```typescript
const [pendingAction, setPendingAction] = useState<{
  label: string
  color: string
} | null>(null)

const clearTimer = useRef<NodeJS.Timeout | null>(null)
const prevStatusRef = useRef(task.status)

// Clear on status change (action confirmed by next poll)
useEffect(() => {
  if (pendingAction && task.status !== prevStatusRef.current) {
    if (clearTimer.current) clearTimeout(clearTimer.current)
    setPendingAction(null)
  }
  prevStatusRef.current = task.status
}, [task.status])

// Cleanup timer on unmount
useEffect(() => () => { if (clearTimer.current) clearTimeout(clearTimer.current) }, [])
```

### Setting pending state in `run()`
```typescript
async function run(action, actionLabel, successTitle, pendingLabel, pendingColor) {
  if (clearTimer.current) clearTimeout(clearTimer.current)
  setPendingAction({ label: pendingLabel, color: pendingColor })
  try {
    await action.mutateAsync(task.name)
    success(successTitle, TASK_LABELS[task.name] ?? task.name)
  } catch (err) {
    toastError(`${actionLabel} failed`, extractWorkerError(err))
  } finally {
    // 6s fallback — covers Trigger (status doesn't change) and slow poll cycles
    clearTimer.current = setTimeout(() => setPendingAction(null), 6_000)
  }
}
```

### Progress bar UI (added below actions div in TaskCard)
```tsx
{pendingAction && (
  <div className="space-y-1">
    <p className="text-[11px] font-medium text-slate-500">{pendingAction.label}</p>
    <div className="h-1 w-full overflow-hidden rounded-full bg-slate-100" role="progressbar" aria-label={pendingAction.label}>
      <div
        className={cn(
          'h-full w-2/5 rounded-full',
          'animate-[progress-slide_1.5s_ease-in-out_infinite]',
          pendingAction.color,
        )}
      />
    </div>
  </div>
)}
```

---

## Files to Change (2 total)

| File | Change |
|------|--------|
| `frontend/src/app/globals.css` | Add `@keyframes progress-slide` after existing `shimmer` keyframe |
| `frontend/src/components/admin/TaskCard.tsx` | Add `pendingAction` state, `clearTimer` ref, `prevStatusRef`, two `useEffect`s, update `run()` signature, add progress bar JSX below actions |

---

## Edge Cases Handled

| Scenario | Behaviour |
|----------|-----------|
| Trigger (no status change) | 6s timeout clears bar |
| Mutation fails | Bar still shows briefly (6s), toast shows error |
| Rapid double-click | `clearTimer` is reset on each click, preventing stale clear |
| Component unmount mid-pending | Cleanup `useEffect` cancels the timer |
| Degraded / all-disabled | Buttons are disabled so `run()` never fires, bar never appears |
