"use client";

/**
 * EventsPanel — AI-processed market news feed.
 *
 * Layout: header → collapsible filters → news-feed list → pagination.
 * Each row shows source, content preview, impact score, sentiment, and
 * affected symbols. Clicking a row opens EventDetailModal.
 */

import * as React from "react";
import {
  TrendingUp,
  TrendingDown,
  Minus,
  RefreshCw,
  Filter,
  ChevronRight,
  Newspaper,
  AlertCircle,
  X,
} from "lucide-react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { useEvents } from "@/hooks/useEvents";
import { EventDetailModal } from "@/components/ai/EventDetailModal";
import {
  EVENT_TYPE_META,
  type EventFilters,
  type EventType,
  type MarketEvent,
  type SentimentLabel,
} from "@/types/events";

// ─────────────────────────────────────────────────────────────────────────────
// Constants
// ─────────────────────────────────────────────────────────────────────────────

const EVENT_TYPE_OPTIONS: { value: EventType; label: string }[] = (
  Object.entries(EVENT_TYPE_META) as [EventType, { label: string; badgeCls: string }][]
).map(([value, { label }]) => ({ value, label }));

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1)  return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24)  return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function toLabel(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─────────────────────────────────────────────────────────────────────────────
// Micro-components
// ─────────────────────────────────────────────────────────────────────────────

function ImpactPill({ score }: { score: number }) {
  const level = score >= 70 ? "High" : score >= 40 ? "Med" : "Low";
  const cls =
    score >= 70 ? "bg-rose-50 border-rose-200 text-rose-700" :
    score >= 40 ? "bg-amber-50 border-amber-200 text-amber-700" :
                  "bg-emerald-50 border-emerald-200 text-emerald-700";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-[11px] font-semibold ${cls}`}>
      Impact
      <span className="tabular-nums font-bold">{score.toFixed(0)}</span>
      <span className="opacity-70 font-medium">· {level}</span>
    </span>
  );
}

function SentimentPill({ sentiment }: { sentiment: SentimentLabel | null }) {
  if (!sentiment) return null;
  const Icon =
    sentiment === "positive" ? TrendingUp :
    sentiment === "negative" ? TrendingDown : Minus;
  const cls =
    sentiment === "positive" ? "bg-emerald-50 border-emerald-200 text-emerald-700" :
    sentiment === "negative" ? "bg-rose-50 border-rose-200 text-rose-700" :
                               "bg-slate-100 border-slate-200 text-slate-600";
  const label =
    sentiment === "positive" ? "Positive" :
    sentiment === "negative" ? "Negative" : "Neutral";
  return (
    <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-0.5 text-[11px] font-semibold ${cls}`}>
      <Icon className="size-3 shrink-0" />
      {label}
    </span>
  );
}

function SymbolPills({ symbols }: { symbols: string[] }) {
  const shown = symbols.slice(0, 3);
  const rest  = symbols.length - shown.length;
  return (
    <div className="flex flex-wrap items-center gap-1">
      {shown.map((s) => (
        <span
          key={s}
          className="rounded-md border border-blue-200 bg-blue-50 px-2 py-0.5 text-[11px] font-semibold text-blue-700"
        >
          {s}
        </span>
      ))}
      {rest > 0 && (
        <span className="rounded-md border border-slate-200 bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500">
          +{rest} more
        </span>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// News feed row
// ─────────────────────────────────────────────────────────────────────────────

function EventRow({
  event,
  onClick,
}: {
  event: MarketEvent;
  onClick: () => void;
}) {
  const typeMeta = EVENT_TYPE_META[event.event_type] ?? {
    label: toLabel(event.event_type),
    badgeCls: "bg-slate-100 text-slate-600 border-slate-200",
  };

  return (
    <button
      onClick={onClick}
      className="group w-full border-b border-slate-100 px-5 py-3.5 text-left transition-colors last:border-0 hover:bg-slate-50/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-blue-500"
    >
      {/* Top row: type badge + source + time */}
      <div className="flex items-center gap-2 flex-wrap mb-1.5">
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-bold ${typeMeta.badgeCls}`}>
          {typeMeta.label}
        </span>
        <div className="flex min-w-0 items-center gap-1 text-slate-500">
          <Newspaper className="size-3 shrink-0" />
          <span className="truncate text-xs font-medium">{event.source_name}</span>
        </div>
        <span className="ml-auto shrink-0 text-[10px] tabular-nums text-slate-400">
          {relativeTime(event.generated_at)}
        </span>
      </div>

      {/* Content snippet — 2 lines */}
      <p className="mb-2.5 line-clamp-2 text-sm leading-relaxed text-slate-700">
        {event.content}
      </p>

      {/* Bottom row: symbols · impact · sentiment · chevron */}
      <div className="flex flex-wrap items-center gap-2">
        {event.affected_symbols.length > 0 && (
          <>
            <SymbolPills symbols={event.affected_symbols} />
            <span className="text-slate-200 select-none">·</span>
          </>
        )}
        <ImpactPill score={event.impact_score} />
        <SentimentPill sentiment={event.sentiment} />
        <ChevronRight className="ml-auto size-4 text-slate-300 transition-transform group-hover:translate-x-0.5 group-hover:text-slate-400" />
      </div>
    </button>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Skeleton rows (loading state)
// ─────────────────────────────────────────────────────────────────────────────

function SkeletonRow() {
  return (
    <div className="border-b border-slate-100 px-5 py-3.5 last:border-0">
      <div className="mb-1.5 flex items-center gap-2">
        <div className="h-4 w-16 animate-pulse rounded-full bg-slate-100" />
        <div className="h-3.5 w-28 animate-pulse rounded bg-slate-100" />
        <div className="ml-auto h-3 w-10 animate-pulse rounded bg-slate-100" />
      </div>
      <div className="mb-2.5 space-y-1.5">
        <div className="h-3.5 w-full animate-pulse rounded bg-slate-100" />
        <div className="h-3.5 w-3/4 animate-pulse rounded bg-slate-100" />
      </div>
      <div className="flex items-center gap-2">
        <div className="h-4 w-14 animate-pulse rounded bg-slate-100" />
        <div className="h-4 w-14 animate-pulse rounded bg-slate-100" />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Filter panel
// ─────────────────────────────────────────────────────────────────────────────

interface FilterPanelProps {
  filters: EventFilters;
  onChange: (key: keyof EventFilters, value: unknown) => void;
  onClear: () => void;
  onClose: () => void;
}

function FilterPanel({ filters, onChange, onClear, onClose }: FilterPanelProps) {
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-4">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-sm font-semibold text-slate-700">Filters</span>
        <div className="flex items-center gap-2">
          <button
            onClick={onClear}
            className="text-xs font-medium text-slate-400 hover:text-slate-600 transition-colors"
          >
            Clear all
          </button>
          <button
            onClick={onClose}
            className="rounded-md p-1 text-slate-400 hover:bg-slate-200 hover:text-slate-600 transition-colors"
          >
            <X className="size-3.5" />
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Symbol
          </label>
          <input
            type="text"
            placeholder="e.g. RELIANCE"
            value={filters.symbol ?? ""}
            onChange={(e) => onChange("symbol", e.target.value || undefined)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-800 placeholder-slate-400 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 transition-all"
          />
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Event Type
          </label>
          <select
            value={filters.event_type ?? ""}
            onChange={(e) => onChange("event_type", e.target.value || undefined)}
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-800 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 transition-all"
          >
            <option value="">All Types</option>
            {EVENT_TYPE_OPTIONS.map(({ value, label }) => (
              <option key={value} value={value}>{label}</option>
            ))}
          </select>
        </div>

        <div className="flex flex-col gap-1">
          <label className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Min Impact Score
          </label>
          <input
            type="number"
            min={0}
            max={100}
            step={5}
            placeholder="0 – 100"
            value={filters.min_impact ?? ""}
            onChange={(e) =>
              onChange("min_impact", e.target.value ? Number(e.target.value) : undefined)
            }
            className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-800 placeholder-slate-400 outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 transition-all"
          />
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────────────
// Main panel
// ─────────────────────────────────────────────────────────────────────────────

interface EventsPanelProps {
  className?: string;
}

export function EventsPanel({ className }: EventsPanelProps) {
  const [filters, setFilters]       = React.useState<EventFilters>({ page: 1, limit: 15 });
  const [showFilters, setShowFilters] = React.useState(false);
  const [selectedEvent, setSelectedEvent] = React.useState<MarketEvent | null>(null);

  const { data, isLoading, isFetching, refetch } = useEvents(filters);

  const handleFilterChange = React.useCallback(
    (key: keyof EventFilters, value: unknown) => {
      setFilters((prev) => ({
        ...prev,
        [key]: value,
        page: key === "page" ? (value as number) : 1,
      }));
    },
    []
  );

  const clearFilters = React.useCallback(() => {
    setFilters({ page: 1, limit: 15 });
  }, []);

  const currentPage = filters.page ?? 1;
  const pageSize    = filters.limit ?? 15;
  const total       = data?.total ?? 0;
  const totalPages  = Math.max(1, Math.ceil(total / pageSize));
  const hasFilter   = !!(filters.symbol || filters.event_type || filters.min_impact != null);

  return (
    <>
      <Card className={className}>
        {/* ── Header ──────────────────────────────────────────────────── */}
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <span className="text-base font-semibold text-slate-900">Market Events</span>
              {data && (
                <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] font-semibold text-slate-500">
                  {total.toLocaleString()}
                </span>
              )}
            </div>

            <div className="flex items-center gap-2">
              <button
                onClick={() => setShowFilters((v) => !v)}
                className={`flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-all ${
                  showFilters || hasFilter
                    ? "border-blue-300 bg-blue-50 text-blue-700"
                    : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                }`}
              >
                <Filter className="size-3.5" />
                Filters
                {hasFilter && (
                  <span className="size-1.5 rounded-full bg-blue-500" />
                )}
              </button>

              <button
                onClick={() => refetch()}
                disabled={isFetching}
                className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-50"
              >
                <RefreshCw className={`size-3.5 ${isFetching ? "animate-spin" : ""}`} />
                Refresh
              </button>
            </div>
          </div>

          {showFilters && (
            <div className="mt-3">
              <FilterPanel
                filters={filters}
                onChange={handleFilterChange}
                onClear={clearFilters}
                onClose={() => setShowFilters(false)}
              />
            </div>
          )}
        </CardHeader>

        {/* ── Feed list ────────────────────────────────────────────────── */}
        <CardContent className="p-0">
          {isLoading ? (
            <div className="divide-y divide-slate-100">
              {Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)}
            </div>
          ) : !data || data.events.length === 0 ? (
            <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
              <AlertCircle className="size-8 text-slate-300" />
              <p className="text-sm font-medium text-slate-400">No events found</p>
              {hasFilter && (
                <button
                  onClick={clearFilters}
                  className="text-xs font-medium text-blue-500 hover:text-blue-700 transition-colors"
                >
                  Clear filters
                </button>
              )}
            </div>
          ) : (
            <div>
              {data.events.map((event) => (
                <EventRow
                  key={event.id}
                  event={event}
                  onClick={() => setSelectedEvent(event)}
                />
              ))}
            </div>
          )}

          {/* ── Pagination ──────────────────────────────────────────── */}
          {!isLoading && totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-slate-100 px-5 py-3">
              <span className="text-xs text-slate-400">
                {((currentPage - 1) * pageSize) + 1}–
                {Math.min(currentPage * pageSize, total)} of {total.toLocaleString()}
              </span>

              <div className="flex items-center gap-2">
                <button
                  onClick={() => handleFilterChange("page", currentPage - 1)}
                  disabled={currentPage <= 1}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Previous
                </button>
                <span className="min-w-[80px] text-center text-xs text-slate-500">
                  {currentPage} / {totalPages}
                </span>
                <button
                  onClick={() => handleFilterChange("page", currentPage + 1)}
                  disabled={currentPage >= totalPages}
                  className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Detail modal ─────────────────────────────────────────────── */}
      {selectedEvent && (
        <EventDetailModal
          event={selectedEvent}
          onClose={() => setSelectedEvent(null)}
        />
      )}
    </>
  );
}
