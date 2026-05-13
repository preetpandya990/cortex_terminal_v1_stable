"use client";

/**
 * EventDetailModal — Full market-event detail view.
 *
 * Matches the SignalDetailModal overlay pattern (custom fixed overlay,
 * no shadcn Dialog). Sections: header with prominent source link,
 * full content, AI impact analysis, AI reasoning, affected symbols.
 */

import { ExternalLink, X, TrendingUp, TrendingDown, Minus, Newspaper } from "lucide-react";
import {
  EVENT_TYPE_META,
  SENTIMENT_META,
  type MarketEvent,
  type SentimentLabel,
} from "@/types/events";

// ── Formatters ─────────────────────────────────────────────────────────────────

function fmtIST(iso: string): string {
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(new Date(iso));
}

function toLabel(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Impact bar ─────────────────────────────────────────────────────────────────

function ImpactBar({ score }: { score: number }) {
  const pct    = Math.min(100, Math.max(0, score));
  const barCls = score >= 70 ? "bg-rose-500" : score >= 40 ? "bg-amber-500" : "bg-emerald-500";
  const textCls = score >= 70 ? "text-rose-600" : score >= 40 ? "text-amber-600" : "text-emerald-600";
  const label   = score >= 70 ? "High" : score >= 40 ? "Medium" : "Low";

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          Market Impact
        </span>
        <div className="flex items-center gap-1.5">
          <span className={`rounded-md border px-2 py-0.5 text-[11px] font-bold ${
            score >= 70 ? "bg-rose-50 border-rose-200 text-rose-700" :
            score >= 40 ? "bg-amber-50 border-amber-200 text-amber-700" :
                          "bg-emerald-50 border-emerald-200 text-emerald-700"
          }`}>
            {label}
          </span>
          <span className={`text-base font-bold tabular-nums ${textCls}`}>
            {score.toFixed(0)}
          </span>
        </div>
      </div>
      <div className="h-2.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-all ${barCls}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Confidence bar ─────────────────────────────────────────────────────────────

function ConfidenceBar({ value }: { value: number }) {
  const pct     = Math.min(100, Math.max(0, value * 100));
  const barCls  = value >= 0.8 ? "bg-emerald-500" : value >= 0.6 ? "bg-amber-500" : "bg-rose-500";
  const textCls = value >= 0.8 ? "text-emerald-700" : value >= 0.6 ? "text-amber-700" : "text-rose-700";

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          AI Classification Confidence
        </span>
        <span className={`text-sm font-bold tabular-nums ${textCls}`}>
          {pct.toFixed(0)}%
        </span>
      </div>
      <div className="h-2.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-all ${barCls}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Sentiment row ──────────────────────────────────────────────────────────────

function SentimentRow({ sentiment, score }: { sentiment: SentimentLabel | null; score: number | null }) {
  if (!sentiment) return null;
  const meta = SENTIMENT_META[sentiment];

  const Icon =
    sentiment === "positive" ? TrendingUp :
    sentiment === "negative" ? TrendingDown : Minus;

  const accentCls =
    sentiment === "positive" ? "border-l-emerald-400" :
    sentiment === "negative" ? "border-l-rose-400" :
                               "border-l-slate-300";

  return (
    <div className={`flex items-center justify-between rounded-xl border border-l-[3px] border-slate-100 bg-slate-50/60 px-4 py-3 ${accentCls}`}>
      <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">Sentiment</span>
      <div className={`flex items-center gap-2 ${meta.cls}`}>
        <span className={`size-2 rounded-full ${meta.dotCls}`} />
        <Icon className="size-4" />
        <span className="text-sm font-semibold">{meta.label}</span>
        {score != null && (
          <span className="rounded bg-white/80 px-1.5 py-0.5 text-[11px] font-semibold tabular-nums text-slate-500 ring-1 ring-slate-200">
            {score > 0 ? "+" : ""}{score.toFixed(2)}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Section header ─────────────────────────────────────────────────────────────

function SectionHeader({ label }: { label: string }) {
  return (
    <div className="mb-2.5 flex items-center gap-2">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        {label}
      </span>
      <div className="flex-1 border-t border-slate-100" />
    </div>
  );
}

// ── Public component ───────────────────────────────────────────────────────────

export interface EventDetailModalProps {
  event: MarketEvent;
  onClose: () => void;
}

export function EventDetailModal({ event, onClose }: EventDetailModalProps) {
  const typeMeta = EVENT_TYPE_META[event.event_type] ?? {
    label: toLabel(event.event_type),
    badgeCls: "bg-slate-100 text-slate-600 border-slate-200",
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm p-0 sm:p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full sm:max-w-2xl max-h-[92dvh] flex flex-col rounded-t-2xl sm:rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden">

        {/* ── Header ──────────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-3 border-b border-slate-100 bg-white px-5 py-4 shrink-0">
          <div className="flex min-w-0 flex-col gap-1.5">
            {/* Type badge + source */}
            <div className="flex flex-wrap items-center gap-2">
              <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${typeMeta.badgeCls}`}>
                {typeMeta.label}
              </span>
              <div className="flex items-center gap-1.5 min-w-0">
                <Newspaper className="size-3.5 shrink-0 text-slate-400" />
                <span className="text-sm font-semibold text-slate-800 truncate">
                  {event.source_name}
                </span>
              </div>
            </div>
            {/* Timestamp */}
            <p className="text-[11px] text-slate-400">{fmtIST(event.generated_at)} IST</p>
          </div>

          <div className="flex shrink-0 items-center gap-2">
            {/* Prominent source link button */}
            {event.source_url && (
              <a
                href={event.source_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-blue-700 active:bg-blue-800"
              >
                <ExternalLink className="size-3.5" />
                Read Article
              </a>
            )}

            {/* Close */}
            <button
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
              aria-label="Close"
            >
              <X className="size-4" />
            </button>
          </div>
        </div>

        {/* ── Scrollable body ─────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">

          {/* Full content */}
          <section>
            <SectionHeader label="Article Content" />
            <p className="text-sm leading-relaxed text-slate-700">{event.content}</p>
          </section>

          {/* AI Impact Analysis */}
          <section className="space-y-3">
            <SectionHeader label="AI Impact Analysis" />
            <ImpactBar score={event.impact_score} />
            {event.confidence != null && <ConfidenceBar value={event.confidence} />}
            <SentimentRow sentiment={event.sentiment} score={event.sentiment_score} />
          </section>

          {/* AI Reasoning */}
          {event.reasoning && (
            <section>
              <SectionHeader label="AI Reasoning" />
              <div className="rounded-xl border border-violet-200 bg-violet-50 px-4 py-3.5">
                <p className="text-sm italic leading-relaxed text-violet-800">
                  {event.reasoning}
                </p>
              </div>
            </section>
          )}

          {/* Affected Symbols */}
          {event.affected_symbols.length > 0 && (
            <section>
              <SectionHeader label={`Affected Symbols (${event.affected_symbols.length})`} />
              <div className="flex flex-wrap gap-1.5">
                {event.affected_symbols.map((s) => (
                  <span
                    key={s}
                    className="rounded-lg border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs font-semibold text-slate-700"
                  >
                    {s}
                  </span>
                ))}
              </div>
            </section>
          )}

          {/* Metadata */}
          <div className="rounded-xl border border-slate-100 bg-slate-50/60 px-4 py-3">
            <div className="grid grid-cols-2 gap-2 text-xs">
              <div>
                <p className="text-[10px] uppercase tracking-wide text-slate-400">Published (IST)</p>
                <p className="mt-0.5 font-medium text-slate-700">{fmtIST(event.generated_at)}</p>
              </div>
              <div>
                <p className="text-[10px] uppercase tracking-wide text-slate-400">Event Type</p>
                <p className="mt-0.5 font-medium text-slate-700">{typeMeta.label}</p>
              </div>
            </div>
          </div>

        </div>
      </div>
    </div>
  );
}
