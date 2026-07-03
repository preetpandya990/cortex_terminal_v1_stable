'use client'

import { AlertCircle, Loader2, RefreshCw } from 'lucide-react'
import { formatDistanceToNow } from 'date-fns'
import { TaskCard } from './TaskCard'
import type { TaskDetail, WorkerTasksResponse } from '@/types/worker_control'
import { cn } from '@/lib/utils'

// ── Task groupings ─────────────────────────────────────────────────────────────

const TASK_GROUPS = [
  {
    label:       'Schedulers & Native',
    description: 'Cooperative pause and trigger supported',
    names: [
      'heartbeat',
      'cache_invalidation',
      'suggestion_expiry',
      'correlation_engine',
      'fundamentals_refresh',
      'watchlist_scheduler',
    ],
  },
  {
    label:       'Pipeline Workers',
    description: 'CancelledError shutdown; trigger fires on the next natural cycle',
    names: [
      'rss_ingestion',
      'event_processing',
      'regime_detection',
      'drift_detection',
      'safety_monitoring',
      'data_ingestion',
      'feature_refresh',
      'rag_cleanup',
    ],
  },
  {
    label:       'FSM Workers',
    description: 'Financial state machine workers',
    names: [
      'pnl_worker',
      'sl_tp_worker',
    ],
  },
] as const

// ── Component ──────────────────────────────────────────────────────────────────

interface Props {
  data:          WorkerTasksResponse | undefined
  isLoading:     boolean
  isError:       boolean
  isRefetching:  boolean
  dataUpdatedAt: number
  isDegraded:    boolean
  onRefresh:     () => void
}

export function TasksGrid({
  data,
  isLoading,
  isError,
  isRefetching,
  dataUpdatedAt,
  isDegraded,
  onRefresh,
}: Props) {
  const tasks = data?.tasks ?? {}

  const lastRefreshed = dataUpdatedAt
    ? formatDistanceToNow(new Date(dataUpdatedAt), { addSuffix: true })
    : null

  return (
    <div className="space-y-8">
      {/* Toolbar */}
      <div className="flex items-center justify-between">
        <p className="text-xs text-slate-400">
          {lastRefreshed
            ? `Refreshed ${lastRefreshed}`
            : isLoading
              ? 'Loading…'
              : 'Never refreshed'}
          {data && !isLoading && ` · ${data.count} tasks`}
        </p>
        <button
          type="button"
          onClick={onRefresh}
          disabled={isRefetching || isDegraded}
          className={cn(
            'flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white',
            'px-3 py-1.5 text-xs font-semibold text-slate-600',
            'transition-colors hover:bg-slate-50',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400',
            'disabled:cursor-not-allowed disabled:opacity-40',
          )}
        >
          <RefreshCw className={cn('h-3.5 w-3.5', isRefetching && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {/* Loading state */}
      {isLoading && (
        <div className="flex items-center justify-center gap-3 py-16 text-slate-400">
          <Loader2 className="h-5 w-5 animate-spin" />
          <span className="text-sm">Loading task states…</span>
        </div>
      )}

      {/* Non-degraded error state */}
      {isError && !isDegraded && !isLoading && (
        <div className="flex flex-col items-center gap-3 py-16 text-center">
          <AlertCircle className="h-8 w-8 text-red-400" />
          <p className="text-sm font-medium text-slate-700">Failed to load task states</p>
          <button
            type="button"
            onClick={onRefresh}
            className="text-sm text-blue-600 underline hover:no-underline"
          >
            Try again
          </button>
        </div>
      )}

      {/* Task groups — shown when we have data (possibly stale) or are degraded */}
      {!isLoading && (data || isDegraded) &&
        TASK_GROUPS.map((group) => {
          const groupTasks = group.names
            .map((name) => tasks[name])
            .filter((t): t is TaskDetail => t !== undefined)

          return (
            <section key={group.label} aria-label={group.label}>
              <div className="mb-3">
                <h2 className="text-sm font-semibold text-slate-700">{group.label}</h2>
                <p className="mt-0.5 text-[11px] text-slate-400">{group.description}</p>
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {groupTasks.length > 0
                  ? groupTasks.map((task) => (
                      <TaskCard key={task.name} task={task} isDegraded={isDegraded} />
                    ))
                  : group.names.map((name) => (
                      <div
                        key={name}
                        className="flex h-[116px] items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50"
                      >
                        <span className="text-xs text-slate-400">
                          {isDegraded ? 'Unavailable' : 'No data'}
                        </span>
                      </div>
                    ))}
              </div>
            </section>
          )
        })}
    </div>
  )
}
