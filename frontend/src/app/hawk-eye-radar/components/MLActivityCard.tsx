"use client";

import { memo, useEffect, useRef, useState } from "react";
import { Brain, ChevronDown, ChevronUp, Loader2, CheckCircle2, XCircle, Newspaper, ScanLine } from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { MLActivityItem } from "@/hooks/useMLActivity";
import type { TriggerType } from "@/hooks/useWebSocket";

// ── Constants ─────────────────────────────────────────────────────────────────

const STORAGE_KEY = "hawk-eye:ml-activity:expanded";

const REJECTION_LABELS: Record<string, string> = {
  ML_NEUTRAL:           "ML Hold",
  DIRECTION_MISMATCH:   "Dir. Conflict",
  LOW_CONFIDENCE:       "Low Consensus",
  NEUTRAL_SIGNAL:       "Neutral Signal",
  DUPLICATE_SUPPRESSED: "Duplicate",
  TIMEOUT:              "Timeout",
  PROCESSING_ERROR:     "Error",
};

// ── Helpers ───────────────────────────────────────────────────────────────────

function formatRelativeTime(ts: number, now: number): string {
  const diff = Math.max(0, now - ts);
  if (diff < 8_000)   return "just now";
  if (diff < 60_000)  return `${Math.floor(diff / 1_000)}s`;
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m`;
  return `${Math.floor(diff / 3_600_000)}h`;
}

function readInitialExpanded(): boolean {
  if (typeof window === "undefined") return true;
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored === null ? true : stored === "true";
  } catch {
    return true;
  }
}

// ── Sub-components ────────────────────────────────────────────────────────────

function LiveIndicator({ isConnected }: { isConnected: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <span
        className={cn(
          "relative flex h-2 w-2",
          isConnected ? "text-emerald-500" : "text-slate-400",
        )}
      >
        {isConnected && (
          <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
        )}
        <span
          className={cn(
            "relative inline-flex h-2 w-2 rounded-full",
            isConnected ? "bg-emerald-500" : "bg-slate-400",
          )}
        />
      </span>
      <span
        className={cn(
          "text-[11px] font-semibold uppercase tracking-wide",
          isConnected ? "text-emerald-600" : "text-slate-400",
        )}
      >
        {isConnected ? "Live" : "Offline"}
      </span>
    </div>
  );
}

function TriggerBadge({ triggerType }: { triggerType: TriggerType }) {
  const isScanner = triggerType === "SCANNER_ANOMALY";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-semibold shrink-0",
        isScanner
          ? "bg-blue-50 text-blue-600"
          : "bg-violet-50 text-violet-600",
      )}
    >
      {isScanner
        ? <ScanLine className="h-2.5 w-2.5" />
        : <Newspaper className="h-2.5 w-2.5" />}
      {isScanner ? "Scanner" : "News"}
    </span>
  );
}

function DirectionChip({ direction }: { direction: "BUY" | "SELL" }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide",
        direction === "BUY"
          ? "bg-emerald-100 text-emerald-700"
          : "bg-rose-100 text-rose-700",
      )}
    >
      {direction}
    </span>
  );
}

interface ActivityRowProps {
  item: MLActivityItem;
  now: number;
}

function ActivityRow({ item, now }: ActivityRowProps) {
  // Prefer trading_symbol; if it looks like a raw instrument_key (contains "|"),
  // use whatever comes after the last pipe — that is the human-readable ticker.
  const rawTicker = item.trading_symbol || item.symbol;
  const ticker = rawTicker.includes("|")
    ? (rawTicker.split("|").pop() ?? rawTicker)
    : rawTicker;

  return (
    <div className="flex items-start gap-2.5 px-3.5 py-2.5 border-b border-slate-100 last:border-b-0 hover:bg-slate-50/60 transition-colors duration-100">
      {/* Status icon */}
      <div className="mt-0.5 shrink-0">
        {item.status === "processing" && (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-indigo-500" />
        )}
        {item.status === "completed" && (
          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
        )}
        {item.status === "rejected" && (
          <XCircle className="h-3.5 w-3.5 text-rose-400" />
        )}
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0">
        {/* Top row: ticker + trigger badge + result/time */}
        <div className="flex items-center gap-1.5 flex-wrap">
          <span className="text-xs font-bold text-slate-900 tabular-nums shrink-0">
            {ticker}
          </span>
          <TriggerBadge triggerType={item.trigger_type} />

          {item.status === "completed" && item.direction && (
            <>
              <DirectionChip direction={item.direction} />
              {item.consensus_score !== undefined && (
                <span className="text-[10px] font-semibold text-slate-600 tabular-nums">
                  {item.consensus_score.toFixed(1)}
                </span>
              )}
            </>
          )}

          {item.status === "processing" && (
            <span className="text-[10px] text-slate-400 italic">
              Analyzing…
            </span>
          )}

          {item.status === "rejected" && item.rejection_reason && (
            <span className="text-[10px] font-medium text-rose-500 truncate">
              {REJECTION_LABELS[item.rejection_reason] ?? item.rejection_reason}
            </span>
          )}

          {/* Timestamp — pushed right */}
          <span className="ml-auto text-[10px] text-slate-400 tabular-nums shrink-0">
            {formatRelativeTime(item.started_at, now)}
          </span>
        </div>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-8 px-4 text-center">
      <Brain className="h-8 w-8 text-slate-300 mb-2" />
      <p className="text-xs font-medium text-slate-500">Monitoring market activity</p>
      <p className="text-[11px] text-slate-400 mt-0.5">
        Pipeline events will appear here in real time
      </p>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface MLActivityCardProps {
  items: MLActivityItem[];
  isConnected: boolean;
  className?: string;
}

function MLActivityCardComponent({
  items,
  isConnected,
  className,
}: MLActivityCardProps) {
  const [isExpanded, setIsExpanded] = useState<boolean>(readInitialExpanded);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Live timestamp — ticks every second, isolated to this component.
  const [now, setNow] = useState<number>(Date.now);
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, []);

  // Persist collapse state.
  const handleToggle = () => {
    setIsExpanded((prev) => {
      const next = !prev;
      try {
        localStorage.setItem(STORAGE_KEY, String(next));
      } catch {
        // localStorage may be blocked in some environments — non-fatal.
      }
      return next;
    });
  };

  // Auto-scroll to top when a new item is inserted (newest always at top).
  const prevCountRef = useRef(items.length);
  useEffect(() => {
    if (items.length > prevCountRef.current && scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
    prevCountRef.current = items.length;
  }, [items.length]);

  const processingCount = items.filter((i) => i.status === "processing").length;

  return (
    <Card
      className={cn(
        "border-slate-200 bg-white overflow-hidden",
        className,
      )}
    >
      {/* Header */}
      <CardHeader className="px-3.5 py-3 border-b border-slate-100">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <Brain className="h-4 w-4 text-indigo-500 shrink-0" />
            <span className="text-sm font-semibold text-slate-800 leading-none">
              ML Activity
            </span>
            {processingCount > 0 && (
              <span className="inline-flex items-center justify-center h-4 min-w-4 rounded-full bg-indigo-100 text-indigo-700 text-[10px] font-bold px-1">
                {processingCount}
              </span>
            )}
          </div>

          <div className="flex items-center gap-2 shrink-0">
            <LiveIndicator isConnected={isConnected} />
            <button
              onClick={handleToggle}
              className="p-0.5 rounded text-slate-400 hover:text-slate-600 hover:bg-slate-100 transition-colors"
              aria-label={isExpanded ? "Collapse ML Activity" : "Expand ML Activity"}
            >
              {isExpanded
                ? <ChevronUp className="h-3.5 w-3.5" />
                : <ChevronDown className="h-3.5 w-3.5" />}
            </button>
          </div>
        </div>
      </CardHeader>

      {/* Body — collapsible */}
      {isExpanded && (
        <CardContent className="p-0">
          <div
            ref={scrollRef}
            className="overflow-y-auto max-h-[min(420px,calc(100vh-18rem))] overscroll-contain"
          >
            {items.length === 0 ? (
              <EmptyState />
            ) : (
              items.map((item) => (
                <ActivityRow key={item.correlation_id} item={item} now={now} />
              ))
            )}
          </div>
        </CardContent>
      )}
    </Card>
  );
}

export const MLActivityCard = memo(MLActivityCardComponent);
