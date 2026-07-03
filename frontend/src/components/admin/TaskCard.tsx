'use client'

import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Info, Loader2, Pause, Play, RefreshCw, RotateCcw } from 'lucide-react'
import { formatDistanceToNow, parseISO } from 'date-fns'
import { useToast } from '@/components/ui/toast'
import {
  extractWorkerError,
  usePauseTask,
  useRestartTask,
  useResumeTask,
  useTriggerTask,
} from '@/hooks/useWorkerControl'
import type { TaskDetail, TaskStatus } from '@/types/worker_control'
import { cn } from '@/lib/utils'

// ── Task display metadata ──────────────────────────────────────────────────────

const TASK_LABELS: Record<string, string> = {
  heartbeat:            'Heartbeat',
  cache_invalidation:   'Cache Invalidation',
  suggestion_expiry:    'Suggestion Expiry',
  correlation_engine:   'Correlation Engine',
  fundamentals_refresh: 'Fundamentals Refresh',
  watchlist_scheduler:  'Watchlist Scheduler',
  rss_ingestion:        'RSS Ingestion',
  event_processing:     'Event Processing',
  regime_detection:     'Regime Detection',
  drift_detection:      'Drift Detection',
  safety_monitoring:    'Safety Monitoring',
  data_ingestion:       'Data Ingestion',
  feature_refresh:      'Feature Refresh',
  rag_cleanup:          'RAG Cleanup',
  pnl_worker:           'P&L Worker',
  sl_tp_worker:         'SL / TP Worker',
}

const WORKER_INFO: Record<string, string> = {
  heartbeat:
    'Periodic liveness pulse that pings the database, Redis, and Upstox endpoints. Keeps system health metrics current and surfaces connectivity issues early — before they cascade into user-visible failures.',
  cache_invalidation:
    'Sweeps Redis for expired entries across market data, analysis results, and watchlist context caches. Ensures all consumers read fresh data without stale-cache artefacts appearing in the UI.',
  suggestion_expiry:
    'Scans open trade suggestions and marks any that have exceeded their validity window as expired. Prevents stale signals from surfacing on the scanner and trade suggestion feed.',
  correlation_engine:
    'Computes cross-instrument correlation matrices for all active watchlist pairs. Powers the correlation heatmap, regime-aware position sizing, and diversification signals.',
  fundamentals_refresh:
    'Fetches updated company fundamentals — P/E, EPS, revenue, debt ratios, and cash flow — from the Upstox API and persists them to the fundamentals tables used by the ML feature pipeline.',
  watchlist_scheduler:
    'Pre-generates AI explanation context (Gemini summaries, regime analysis, pattern snapshots) for every instrument on every user watchlist. Eliminates the 10–15 s cold-start delay when opening an instrument view.',
  rss_ingestion:
    'Polls financial RSS feeds, deduplicates headlines, and enqueues them for the sentiment batch pipeline. Feeds the NLP engine and event classifier with fresh market narrative.',
  event_processing:
    'Classifies incoming market events (earnings, dividends, splits, M&A) extracted from news and corporate actions. Updates event-driven signals and enriches pattern context for the AI explanation layer.',
  regime_detection:
    'Runs the market regime classifier across all tracked instruments to detect transitions between trending, ranging, and volatile states. Results feed the ML ensemble and the regime context card.',
  drift_detection:
    'Monitors the live feature distribution against training baselines for each deployed model. Flags models that have drifted beyond configured thresholds and queues them for operator review.',
  safety_monitoring:
    'Applies circuit-breaker rules to all open paper trading positions: checks max drawdown, position concentration, and daily loss limits. Triggers forced exits when risk limits are breached.',
  data_ingestion:
    'Syncs live OHLCV candles and tick data from Upstox. Handles delisted instruments with soft-delete write-backs, and feeds the feature pipeline with time-series continuity.',
  feature_refresh:
    'Recomputes the full ML feature store — technical indicators, fundamentals features, and regime context — for all active instruments. Keeps prediction inputs fresh for the ensemble.',
  rag_cleanup:
    'Prunes stale or low-quality embeddings from the pgvector RAG store. Maintains retrieval quality by removing outdated context chunks that would dilute AI explanation accuracy.',
  pnl_worker:
    'Tracks real-time P&L for all open paper trading positions using live price feeds. Computes unrealised gains/losses and pushes updates through the paper trading WebSocket.',
  sl_tp_worker:
    'Monitors every open paper position against its stop-loss and take-profit levels. Triggers automatic exits and records trade outcomes when price targets are hit.',
}

const BUTTON_GUIDE = [
  {
    label: 'Pause',
    description:
      'Suspends the task at its next cooperative checkpoint. State is fully preserved — the task resumes exactly where it stopped. Ideal for maintenance without data loss.',
  },
  {
    label: 'Resume',
    description:
      'Lifts the pause. The task continues immediately from its last checkpoint with no loss of in-flight state.',
  },
  {
    label: 'Trigger',
    description:
      'Wakes the task immediately, skipping the remaining sleep interval. Forces one full work cycle right now. Market-hours guards are bypassed when triggered from the admin panel.',
  },
  {
    label: 'Restart',
    description:
      'Cancels the current instance. The supervisor spawns a fresh coroutine within milliseconds. Crash count is not incremented for manual restarts.',
  },
] as const

const STATUS_META: Record<TaskStatus, { label: string; classes: string }> = {
  running:  { label: 'Running',  classes: 'bg-emerald-50 text-emerald-700 border border-emerald-200' },
  paused:   { label: 'Paused',   classes: 'bg-amber-50 text-amber-700 border border-amber-200'       },
  starting: { label: 'Starting', classes: 'bg-blue-50 text-blue-700 border border-blue-200'          },
  crashed:  { label: 'Crashed',  classes: 'bg-rose-50 text-rose-700 border border-rose-200'          },
  stopped:  { label: 'Stopped',  classes: 'bg-slate-100 text-slate-600 border border-slate-200'      },
}

// ── Helpers ────────────────────────────────────────────────────────────────────

function fmtLastRun(iso: string | null): { relative: string; isoTitle: string | undefined } {
  if (!iso) return { relative: 'Never', isoTitle: undefined }
  try {
    return {
      relative: formatDistanceToNow(parseISO(iso), { addSuffix: true }),
      isoTitle: iso,
    }
  } catch {
    return { relative: '—', isoTitle: undefined }
  }
}

// ── Worker info portal ─────────────────────────────────────────────────────────

interface WorkerInfoPortalProps {
  name:   string
  anchor: { top: number; left: number; width: number }
}

function WorkerInfoPortal({ name, anchor }: WorkerInfoPortalProps) {
  const [mounted, setMounted] = useState(false)
  useEffect(() => { setMounted(true) }, [])
  if (!mounted) return null

  const TOOLTIP_WIDTH = 320
  const left = Math.min(
    Math.max(8, anchor.left + anchor.width / 2 - TOOLTIP_WIDTH / 2),
    (typeof window !== 'undefined' ? window.innerWidth : 1200) - TOOLTIP_WIDTH - 8,
  )
  const top = anchor.top + 8

  return createPortal(
    <div
      role="tooltip"
      style={{ position: 'fixed', top, left, width: TOOLTIP_WIDTH, zIndex: 9999 }}
      className="pointer-events-none rounded-xl border border-slate-200 bg-white shadow-2xl"
    >
      {/* Header */}
      <div className="border-b border-slate-100 px-4 py-3">
        <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Worker</p>
        <p className="mt-0.5 text-[13px] font-semibold text-slate-800 leading-snug">
          {TASK_LABELS[name] ?? name}
        </p>
        <p className="mt-0.5 font-mono text-[10px] text-slate-400">{name}</p>
      </div>

      {/* Description */}
      <div className="px-4 pt-3 pb-2">
        <p className="text-[12px] leading-relaxed text-slate-600">
          {WORKER_INFO[name] ?? 'Background worker task.'}
        </p>
      </div>

      {/* Controls guide */}
      <div className="mx-4 mb-3 rounded-lg bg-slate-50 px-3 py-2.5 space-y-2">
        <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">Controls</p>
        {BUTTON_GUIDE.map(({ label, description }) => (
          <div key={label}>
            <span className="text-[11px] font-semibold text-slate-700">{label} — </span>
            <span className="text-[11px] text-slate-500">{description}</span>
          </div>
        ))}
      </div>
    </div>,
    document.body,
  )
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: TaskStatus }) {
  const { label, classes } = STATUS_META[status]
  return (
    <span className={cn('inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold', classes)}>
      {label}
    </span>
  )
}

interface ActionButtonProps {
  icon:      React.ElementType
  label:     string
  isPending: boolean
  disabled:  boolean
  variant?:  'default' | 'danger'
  onClick:   () => void
}

function ActionButton({ icon: Icon, label, isPending, disabled, variant = 'default', onClick }: ActionButtonProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || isPending}
      title={label}
      aria-label={label}
      className={cn(
        'flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold',
        'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1',
        'disabled:cursor-not-allowed disabled:opacity-40',
        variant === 'danger'
          ? 'border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-100 focus-visible:ring-rose-300'
          : 'border-slate-200 bg-white text-slate-700 hover:bg-slate-50 focus-visible:ring-slate-400',
      )}
    >
      {isPending
        ? <Loader2 className="h-3 w-3 animate-spin" />
        : <Icon className="h-3 w-3" />
      }
      {label}
    </button>
  )
}

// ── TaskCard ───────────────────────────────────────────────────────────────────

interface Props {
  task:       TaskDetail
  isDegraded: boolean
}

export function TaskCard({ task, isDegraded }: Props) {
  const { success, error: toastError } = useToast()

  const pause   = usePauseTask()
  const resume  = useResumeTask()
  const trigger = useTriggerTask()
  const restart = useRestartTask()

  // Info portal state — mirrors the MLModelsPanel pattern exactly
  const infoRef = useRef<HTMLButtonElement>(null)
  const [infoHovered, setInfoHovered] = useState(false)
  const [infoAnchor,  setInfoAnchor]  = useState({ top: 0, left: 0, width: 0 })

  // Pending action state for the indeterminate progress bar
  const [pendingAction, setPendingAction] = useState<{ label: string; color: string } | null>(null)
  const clearTimer     = useRef<ReturnType<typeof setTimeout> | null>(null)
  const prevStatusRef  = useRef<TaskStatus>(task.status)

  // Clear bar when the backend confirms the action (status changes on next poll)
  useEffect(() => {
    if (task.status !== prevStatusRef.current) {
      if (clearTimer.current) clearTimeout(clearTimer.current)
      setPendingAction(null)
      prevStatusRef.current = task.status
    }
  }, [task.status])

  // Guaranteed timer cleanup on unmount
  useEffect(() => () => {
    if (clearTimer.current) clearTimeout(clearTimer.current)
  }, [])

  const handleInfoEnter = () => {
    if (infoRef.current) {
      const r = infoRef.current.getBoundingClientRect()
      setInfoAnchor({ top: r.bottom, left: r.left, width: r.width })
    }
    setInfoHovered(true)
  }

  const isWatchlist = task.name === 'watchlist_scheduler'
  const allDisabled = isDegraded || task.status === 'starting'
  const { relative, isoTitle } = fmtLastRun(task.last_run_at)

  async function run(
    action:       { mutateAsync: (name: string) => Promise<unknown> },
    actionLabel:  string,
    successTitle: string,
    pendingLabel: string,
    pendingColor: string,
  ) {
    if (clearTimer.current) clearTimeout(clearTimer.current)
    setPendingAction({ label: pendingLabel, color: pendingColor })
    try {
      await action.mutateAsync(task.name)
      success(successTitle, TASK_LABELS[task.name] ?? task.name)
    } catch (err) {
      toastError(`${actionLabel} failed`, extractWorkerError(err))
    } finally {
      // Fallback: clears bar for Trigger (status unchanged) and slow poll cycles
      clearTimer.current = setTimeout(() => setPendingAction(null), 6_000)
    }
  }

  return (
    <div
      className={cn(
        'flex flex-col gap-3 rounded-xl border bg-white p-4 shadow-sm',
        isWatchlist ? 'border-blue-200 ring-1 ring-blue-100' : 'border-slate-200',
      )}
    >
      {/* Header: name + info icon + status badge */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-1.5">
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold text-slate-900">
              {TASK_LABELS[task.name] ?? task.name}
            </p>
            <p className="mt-0.5 font-mono text-[10px] text-slate-400">{task.name}</p>
          </div>
          {/* Info icon — triggers the portal tooltip */}
          <button
            ref={infoRef}
            type="button"
            aria-label={`About ${TASK_LABELS[task.name] ?? task.name}`}
            onMouseEnter={handleInfoEnter}
            onMouseLeave={() => setInfoHovered(false)}
            onFocus={handleInfoEnter}
            onBlur={() => setInfoHovered(false)}
            className="mt-0.5 flex-shrink-0 rounded p-0.5 text-slate-300 transition-colors hover:text-slate-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
          >
            <Info className="h-3 w-3" />
          </button>
        </div>
        <StatusBadge status={task.status} />
      </div>

      {/* Meta: last run + crash count */}
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-slate-500" title={isoTitle}>
          {relative}
        </span>
        {task.crash_count > 0 && (
          <span className="text-[11px] font-semibold text-rose-600">
            {task.crash_count} crash{task.crash_count !== 1 ? 'es' : ''}
          </span>
        )}
      </div>

      {/* Actions */}
      <div className="flex flex-wrap gap-1.5">
        {task.status === 'running' && (
          <>
            <ActionButton
              icon={Pause}
              label="Pause"
              isPending={pause.isPending}
              disabled={allDisabled}
              onClick={() => run(pause, 'Pause', 'Task paused', 'Pausing task…', 'bg-amber-400')}
            />
            <ActionButton
              icon={RefreshCw}
              label={isWatchlist ? 'Warm All Watchlist Context' : 'Trigger'}
              isPending={trigger.isPending}
              disabled={allDisabled}
              onClick={() => run(
                trigger,
                'Trigger',
                'Task triggered',
                isWatchlist ? 'Warming watchlist context…' : 'Triggering cycle…',
                'bg-blue-400',
              )}
            />
          </>
        )}

        {task.status === 'paused' && (
          <ActionButton
            icon={Play}
            label="Resume"
            isPending={resume.isPending}
            disabled={allDisabled}
            onClick={() => run(resume, 'Resume', 'Task resumed', 'Resuming task…', 'bg-emerald-400')}
          />
        )}

        {task.status !== 'starting' && (
          <ActionButton
            icon={RotateCcw}
            label="Restart"
            isPending={restart.isPending}
            disabled={allDisabled}
            variant={task.status === 'crashed' ? 'danger' : 'default'}
            onClick={() => run(restart, 'Restart', 'Task restarting', 'Restarting task…', 'bg-slate-400')}
          />
        )}
      </div>

      {/* Indeterminate progress bar — appears while an action is in-flight */}
      {pendingAction && (
        <div className="space-y-1">
          <p className="text-[11px] font-medium text-slate-500">{pendingAction.label}</p>
          <div
            className="h-1 w-full overflow-hidden rounded-full bg-slate-100"
            role="progressbar"
            aria-label={pendingAction.label}
          >
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

      {/* Portal tooltip — fixed-positioned to escape grid clipping */}
      {infoHovered && <WorkerInfoPortal name={task.name} anchor={infoAnchor} />}
    </div>
  )
}
