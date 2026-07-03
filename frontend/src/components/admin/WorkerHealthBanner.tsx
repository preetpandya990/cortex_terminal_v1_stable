'use client'

import { AlertCircle, CheckCircle2, Loader2 } from 'lucide-react'
import type { WorkerHealthResponse } from '@/types/worker_control'

// ── Helpers ────────────────────────────────────────────────────────────────────

function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86_400)
  const h = Math.floor((seconds % 86_400) / 3_600)
  const m = Math.floor((seconds % 3_600) / 60)
  if (d > 0) return `${d}d ${h}h ${m}m`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

// ── Component ──────────────────────────────────────────────────────────────────

interface Props {
  health:     WorkerHealthResponse | undefined
  isLoading:  boolean
  isDegraded: boolean
}

export function WorkerHealthBanner({ health, isLoading, isDegraded }: Props) {
  if (isLoading) {
    return (
      <div className="flex items-center gap-2.5 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <Loader2 className="h-4 w-4 animate-spin text-slate-400" />
        <span className="text-sm text-slate-500">Checking worker status…</span>
      </div>
    )
  }

  if (isDegraded || !health) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 shadow-sm">
        <div className="flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-rose-100">
          <AlertCircle className="h-4 w-4 text-rose-600" />
        </div>
        <div>
          <p className="text-sm font-semibold text-rose-800">Worker sidecar unreachable</p>
          <p className="text-xs text-rose-600 mt-0.5">
            Control operations are unavailable. All task actions have been disabled until the
            sidecar comes back online.
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 shadow-sm">
      <div className="relative flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full bg-emerald-100">
        <CheckCircle2 className="h-4 w-4 text-emerald-600" />
        <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-emerald-50 bg-emerald-400" />
      </div>
      <div className="flex items-baseline gap-2">
        <p className="text-sm font-semibold text-emerald-800">Worker online</p>
        <span className="text-xs text-emerald-600">
          uptime {formatUptime(health.uptime_seconds)}
          {' · '}
          {health.task_count} task{health.task_count !== 1 ? 's' : ''} registered
        </span>
      </div>
    </div>
  )
}
