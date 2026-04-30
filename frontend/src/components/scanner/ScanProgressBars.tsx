// PATH: frontend/src/components/scanner/ScanProgressBars.tsx
// ────────────────────────────────────────────────────────────
/**
 * Two progress indicators for the Scanner page:
 *
 *   ScanCountdownBar  — compact strip showing time until the next automatic
 *                       refetch.  Visible only when idle.
 *
 *   ScanProgressBar   — full card driven by real SSE events during a manual
 *                       scan, or a shimmer bar during an automatic background
 *                       refetch.  Fades out 2.5 s after scan completion.
 */
'use client';

import { useEffect, useRef, useState } from 'react';
import { RefreshCw, CheckCircle2, AlertCircle, Loader2 } from 'lucide-react';

// ── Constants ──────────────────────────────────────────────────────────────────

const COMPLETION_HOLD_MS = 2_500;
const FADE_DURATION_MS    = 500;

// ── Helpers ────────────────────────────────────────────────────────────────────

function formatCountdown(remainingMs: number): string {
  const totalSecs = Math.ceil(remainingMs / 1_000);
  if (totalSecs < 60) return `${totalSecs}s`;
  const mins = Math.floor(totalSecs / 60);
  const secs = totalSecs % 60;
  return `${mins}m ${secs.toString().padStart(2, '0')}s`;
}

// ── ScanCountdownBar ───────────────────────────────────────────────────────────

export interface ScanCountdownBarProps {
  /** React Query dataUpdatedAt (ms epoch). 0 means no data has been fetched yet. */
  dataUpdatedAt: number;
  /** True while the React Query background refetch is in flight. */
  isFetching: boolean;
  /** True while the SSE-driven manual scan is in progress. */
  isStreaming: boolean;
  /** The actual polling interval in ms — matches the refetchInterval on the query. */
  refetchMs: number;
}

export function ScanCountdownBar({ dataUpdatedAt, isFetching, isStreaming, refetchMs }: ScanCountdownBarProps) {
  const [remainingMs, setRemainingMs] = useState(refetchMs);

  useEffect(() => {
    if (!dataUpdatedAt || isFetching || isStreaming) return;

    const tick = () =>
      setRemainingMs(Math.max(0, refetchMs - (Date.now() - dataUpdatedAt)));

    tick();
    const id = setInterval(tick, 250);
    return () => clearInterval(id);
  }, [dataUpdatedAt, isFetching, isStreaming, refetchMs]);

  if (!dataUpdatedAt || isFetching || isStreaming) return null;

  // elapsedPct fills left→right as time passes; acts as a "charge depleting" indicator.
  const elapsedPct = Math.min(100, ((refetchMs - remainingMs) / refetchMs) * 100);
  const timeLabel  = formatCountdown(remainingMs);

  return (
    <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-4 py-2.5">
      <RefreshCw className="h-3.5 w-3.5 shrink-0 text-slate-400" />
      <span className="shrink-0 text-xs font-medium text-slate-500">Next refresh</span>
      <div className="flex-1 overflow-hidden rounded-full bg-slate-200 h-1.5">
        <div
          className="h-full rounded-full bg-slate-400"
          style={{ width: `${elapsedPct}%`, transition: 'none' }}
        />
      </div>
      <span className="shrink-0 min-w-[4ch] text-right tabular-nums text-xs font-semibold text-slate-500">
        {timeLabel}
      </span>
    </div>
  );
}

// ── ScanProgressBar ────────────────────────────────────────────────────────────

export interface ScanProgressBarProps {
  /** True while the SSE scan stream is open. */
  isStreaming: boolean;
  /** True while React Query is doing an automatic background refetch. */
  isFetching: boolean;
  /** Current progress percentage (0–100), driven by SSE progress events. */
  pct: number;
  /** Human-readable stage description from the backend. */
  message: string;
  /** Pipeline stage identifier — 'complete' triggers the fade-out sequence. */
  stage: string;
  /** Non-null when the scan or stream failed. */
  error: string | null;
}

export function ScanProgressBar({
  isStreaming,
  isFetching,
  pct,
  message,
  stage,
  error,
}: ScanProgressBarProps) {
  const [mounted, setMounted]   = useState(false);
  const [opacity, setOpacity]   = useState(1);
  const fadeTimer   = useRef<ReturnType<typeof setTimeout> | null>(null);
  const unmountTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const hasError       = !!error || stage === 'error';
  const isActive       = isStreaming || (isFetching && !isStreaming);
  const isIndeterminate = isFetching && !isStreaming;
  const isComplete     = !isActive && !hasError && stage === 'complete' && pct === 100;

  useEffect(() => {
    if (fadeTimer.current)   clearTimeout(fadeTimer.current);
    if (unmountTimer.current) clearTimeout(unmountTimer.current);

    if (isActive || hasError) {
      setMounted(true);
      setOpacity(1);
    } else if (isComplete) {
      setMounted(true);
      setOpacity(1);
      fadeTimer.current   = setTimeout(() => setOpacity(0), COMPLETION_HOLD_MS);
      unmountTimer.current = setTimeout(
        () => setMounted(false),
        COMPLETION_HOLD_MS + FADE_DURATION_MS,
      );
    }

    return () => {
      if (fadeTimer.current)   clearTimeout(fadeTimer.current);
      if (unmountTimer.current) clearTimeout(unmountTimer.current);
    };
  }, [isActive, isComplete, hasError]);

  if (!mounted) return null;

  // ── Theme tokens per state ─────────────────────────────────────────────────

  const theme = hasError
    ? {
        container: 'border-red-200 bg-red-50/70',
        track:     'bg-red-100',
        fill:      'bg-gradient-to-r from-red-600 to-red-400',
        dot:       null,
        icon:      <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />,
        msgCls:    'text-red-800',
        pctPill:   'bg-red-100 text-red-700',
      }
    : isComplete
    ? {
        container: 'border-emerald-200 bg-emerald-50/70',
        track:     'bg-emerald-100',
        fill:      'bg-gradient-to-r from-emerald-600 to-emerald-400',
        dot:       null,
        icon:      <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />,
        msgCls:    'text-emerald-800',
        pctPill:   'bg-emerald-100 text-emerald-700',
      }
    : isIndeterminate
    ? {
        container: 'border-slate-200 bg-slate-50/80',
        track:     'bg-slate-100',
        fill:      null, // shimmer rendered separately
        dot:       null,
        icon:      <Loader2 className="h-4 w-4 shrink-0 animate-spin text-slate-400" />,
        msgCls:    'text-slate-600',
        pctPill:   null,
      }
    : {
        container: 'border-blue-200 bg-blue-50/70',
        track:     'bg-blue-100',
        fill:      'bg-gradient-to-r from-blue-600 to-blue-400',
        dot:       'bg-blue-500',
        icon:      null,
        msgCls:    'text-blue-800',
        pctPill:   'bg-blue-100 text-blue-700',
      };

  const displayMessage = hasError
    ? (error ?? 'Scan failed')
    : isIndeterminate
      ? 'Refreshing data…'
      : (message || 'Starting scan…');

  return (
    <div
      className={`rounded-xl border px-4 py-3.5 ${theme.container}`}
      style={{ opacity, transition: `opacity ${FADE_DURATION_MS}ms ease-out` }}
    >
      {/* ── Header row ────────────────────────────────────────────────── */}
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          {/* State icon / pulsing dot */}
          {theme.icon ?? (
            theme.dot ? (
              <span className="relative flex h-2.5 w-2.5 shrink-0">
                <span className={`absolute inline-flex h-full w-full animate-ping rounded-full ${theme.dot} opacity-60`} />
                <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${theme.dot}`} />
              </span>
            ) : null
          )}
          <span className={`truncate text-sm font-medium ${theme.msgCls}`}>
            {displayMessage}
          </span>
        </div>

        {/* Percentage pill (hidden for indeterminate) */}
        {theme.pctPill && (
          <span className={`shrink-0 rounded-md px-2 py-0.5 tabular-nums text-xs font-bold ${theme.pctPill}`}>
            {pct}%
          </span>
        )}
      </div>

      {/* ── Progress track ────────────────────────────────────────────── */}
      <div className={`relative h-2.5 w-full overflow-hidden rounded-full ${theme.track}`}>
        {isIndeterminate ? (
          /* Shimmer sweep — reuses the @keyframes shimmer defined in globals.css */
          <>
            <div className="absolute inset-0 rounded-full bg-slate-300" />
            <div
              className="absolute inset-0 -translate-x-full rounded-full bg-gradient-to-r from-transparent via-slate-100/80 to-transparent"
              style={{ animation: 'shimmer 1.8s infinite ease-in-out' }}
            />
          </>
        ) : (
          <div
            className={`h-full rounded-full ${theme.fill}`}
            style={{
              width: `${pct}%`,
              transition: 'width 500ms ease-out',
            }}
          />
        )}
      </div>
    </div>
  );
}
