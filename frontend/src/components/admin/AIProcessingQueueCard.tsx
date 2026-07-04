'use client'

import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Info, Loader2, Send, Zap } from 'lucide-react'
import { useToast } from '@/components/ui/toast'
import {
  useAIProcessingStatus,
  useDispatchAll,
  useDispatchCategory,
} from '@/hooks/useAIProcessingQueue'
import { extractWorkerError } from '@/hooks/useWorkerControl'
import type { AIProcessingCategory, CategoryStatus } from '@/types/ai_processing'
import { cn } from '@/lib/utils'

// ── Category display metadata ──────────────────────────────────────────────────

const CATEGORY_LABELS: Record<AIProcessingCategory, string> = {
  sentiment:      'Sentiment Batching',
  forecast:       'News Forecast Batching',
  classification: 'Event Classification',
}

const CATEGORY_INFO: Record<AIProcessingCategory, string> = {
  sentiment:
    'Batches article sentiment scoring into one Gemini call per batch. Queue is in-process memory — a worker restart before dispatch silently drops pending items.',
  forecast:
    'Batches per-symbol news-forecast generation into one Gemini call per batch of symbols. Queue is a durable Kafka topic (Redpanda) — survives worker restarts.',
  classification:
    'Classifies ambiguous ("general") events one at a time via Gemini, upgrading the immediate rule-based classification already persisted for that event. Queue is a durable Kafka topic (Redpanda).',
}

interface Props {
  isDegraded: boolean
}

// ── Info portal ────────────────────────────────────────────────────────────────

function InfoPortal({ anchor }: { anchor: { top: number; left: number; width: number } }) {
  const [mounted, setMounted] = useState(false)
  useEffect(() => { setMounted(true) }, [])
  if (!mounted) return null

  const TOOLTIP_WIDTH = 340
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
      <div className="border-b border-slate-100 px-4 py-3">
        <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
          Demand-Driven Dispatch
        </p>
        <p className="mt-0.5 text-[13px] font-semibold text-slate-800 leading-snug">
          Manual + scheduled Gemini dispatch
        </p>
      </div>
      <div className="px-4 pt-3 pb-3 space-y-2">
        <p className="text-[12px] leading-relaxed text-slate-600">
          These 3 queues only fire Gemini calls when dispatched here, or by the
          daily 09:00 IST safety net — by design, to protect the daily quota
          for user-facing trade explanations and watchlist context. This is a
          no-op while auto-dispatch is still enabled for a category.
        </p>
        <p className="text-[12px] leading-relaxed text-slate-600">
          <span className="font-semibold text-amber-700">Sentiment queue is non-durable</span> —
          it lives in worker process memory. A worker restart before dispatch
          silently drops any pending sentiment items. Forecast and
          classification queues are durable Kafka topics (Redpanda) — they
          survive worker restarts.
        </p>
      </div>
    </div>,
    document.body,
  )
}

// ── Category row ───────────────────────────────────────────────────────────────

interface RowProps {
  category:   AIProcessingCategory
  status:     CategoryStatus | undefined
  isDegraded: boolean
}

function CategoryRow({ category, status, isDegraded }: RowProps) {
  const { success, error: toastError } = useToast()
  const dispatch = useDispatchCategory()

  const [dispatching, setDispatching] = useState(false)
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => () => {
    if (clearTimer.current) clearTimeout(clearTimer.current)
  }, [])

  const pending    = status?.pending ?? 0
  const autoFlush  = status?.auto_flush ?? true
  const disabled   = isDegraded || dispatch.isPending

  const badgeColor =
    pending === 0
      ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
      : pending >= 50
        ? 'bg-rose-50 text-rose-700 border-rose-200'
        : 'bg-amber-50 text-amber-700 border-amber-200'

  async function handleDispatch() {
    if (clearTimer.current) clearTimeout(clearTimer.current)
    setDispatching(true)
    try {
      const result = await dispatch.mutateAsync(category)
      success(
        'Dispatched',
        `${CATEGORY_LABELS[category]}: ${result.dispatched} drained, ${result.calls_made} Gemini call(s)`,
      )
    } catch (err) {
      toastError(`${CATEGORY_LABELS[category]} dispatch failed`, extractWorkerError(err))
    } finally {
      clearTimer.current = setTimeout(() => setDispatching(false), 800)
    }
  }

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="min-w-0" title={CATEGORY_INFO[category]}>
          <p className="truncate text-[13px] font-semibold text-slate-900">
            {CATEGORY_LABELS[category]}
          </p>
          <p className="mt-0.5 text-[11px] text-slate-400">
            {autoFlush ? 'Auto-dispatch enabled (no-op)' : 'Manual dispatch required'}
          </p>
        </div>
        <span
          className={cn(
            'inline-flex flex-shrink-0 items-center rounded-full border px-2 py-0.5 text-[11px] font-semibold',
            badgeColor,
          )}
        >
          {pending} pending
        </span>
      </div>

      <button
        type="button"
        onClick={handleDispatch}
        disabled={disabled}
        className={cn(
          'flex items-center justify-center gap-1.5 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5',
          'text-[11px] font-semibold text-slate-700 transition-colors hover:bg-slate-50',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-1',
          'disabled:cursor-not-allowed disabled:opacity-40',
        )}
      >
        {dispatch.isPending
          ? <Loader2 className="h-3 w-3 animate-spin" />
          : <Send className="h-3 w-3" />
        }
        Dispatch Now
      </button>

      {dispatching && (
        <div
          className="h-1 w-full overflow-hidden rounded-full bg-slate-100"
          role="progressbar"
          aria-label={`Dispatching ${CATEGORY_LABELS[category]}`}
        >
          <div className="h-full w-2/5 rounded-full bg-blue-400 animate-[progress-slide_1.5s_ease-in-out_infinite]" />
        </div>
      )}
    </div>
  )
}

// ── AIProcessingQueueCard ──────────────────────────────────────────────────────

export function AIProcessingQueueCard({ isDegraded }: Props) {
  const { success, error: toastError } = useToast()
  const { data, isLoading } = useAIProcessingStatus()
  const { dispatchAll, isPending: dispatchAllPending } = useDispatchAll()

  const infoRef = useRef<HTMLButtonElement>(null)
  const [infoHovered, setInfoHovered] = useState(false)
  const [infoAnchor,  setInfoAnchor]  = useState({ top: 0, left: 0, width: 0 })

  const handleInfoEnter = () => {
    if (infoRef.current) {
      const r = infoRef.current.getBoundingClientRect()
      setInfoAnchor({ top: r.bottom, left: r.left, width: r.width })
    }
    setInfoHovered(true)
  }

  async function handleDispatchAll() {
    const results = await dispatchAll()
    const failed = results.filter((r) => !r.success)
    if (failed.length === 0) {
      const totalDispatched = results.reduce((sum, r) => sum + (r.result?.dispatched ?? 0), 0)
      success('Dispatch All complete', `${totalDispatched} items drained across 3 categories`)
    } else {
      toastError(
        'Dispatch All: some categories failed',
        failed.map((f) => `${f.category}: ${f.error}`).join('; '),
      )
    }
  }

  const disabledAll = isDegraded || dispatchAllPending

  return (
    <section aria-label="AI Processing Queue" className="space-y-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-1.5">
          <div>
            <h2 className="text-sm font-semibold text-slate-700">Demand-Driven AI Processing</h2>
            <p className="mt-0.5 text-[11px] text-slate-400">
              Manual dispatch for sentiment, forecast, and event-classification Gemini queues
            </p>
          </div>
          <button
            ref={infoRef}
            type="button"
            aria-label="About demand-driven AI processing"
            onMouseEnter={handleInfoEnter}
            onMouseLeave={() => setInfoHovered(false)}
            onFocus={handleInfoEnter}
            onBlur={() => setInfoHovered(false)}
            className="mt-0.5 flex-shrink-0 rounded p-0.5 text-slate-300 transition-colors hover:text-slate-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
          >
            <Info className="h-3 w-3" />
          </button>
        </div>

        <button
          type="button"
          onClick={handleDispatchAll}
          disabled={disabledAll}
          className={cn(
            'flex flex-shrink-0 items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold',
            'transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1',
            'disabled:cursor-not-allowed disabled:opacity-40',
            'border-blue-200 bg-blue-50 text-blue-700 hover:bg-blue-100 focus-visible:ring-blue-300',
          )}
        >
          {dispatchAllPending
            ? <Loader2 className="h-3 w-3 animate-spin" />
            : <Zap className="h-3 w-3" />
          }
          Dispatch All
        </button>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {isLoading && !data
          ? (['sentiment', 'forecast', 'classification'] as const).map((category) => (
              <div
                key={category}
                className="flex h-[104px] items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50"
              >
                <span className="text-xs text-slate-400">Loading…</span>
              </div>
            ))
          : (['sentiment', 'forecast', 'classification'] as const).map((category) => (
              <CategoryRow
                key={category}
                category={category}
                status={data?.[category]}
                isDegraded={isDegraded}
              />
            ))}
      </div>

      {infoHovered && <InfoPortal anchor={infoAnchor} />}
    </section>
  )
}
