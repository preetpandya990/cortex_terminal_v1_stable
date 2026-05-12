'use client';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Newspaper, AlertCircle, ExternalLink } from 'lucide-react';
import type { SentimentAnalysisCard } from '@/types/analysis';

interface AISentimentCardProps {
  data: SentimentAnalysisCard | null;
  isLoading: boolean;
  error?: boolean;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function impactLabel(score: number): { label: string; color: string; bg: string } {
  if (score >= 40)  return { label: 'Very Bullish', color: 'text-emerald-700', bg: 'bg-emerald-50' };
  if (score >= 15)  return { label: 'Bullish',      color: 'text-emerald-600', bg: 'bg-emerald-50' };
  if (score <= -40) return { label: 'Very Bearish', color: 'text-red-700',     bg: 'bg-red-50' };
  if (score <= -15) return { label: 'Bearish',      color: 'text-red-600',     bg: 'bg-red-50' };
  return { label: 'Neutral', color: 'text-slate-600', bg: 'bg-slate-50' };
}

function sentimentColor(label: 'positive' | 'negative' | 'neutral'): string {
  if (label === 'positive') return 'text-emerald-600 bg-emerald-50';
  if (label === 'negative') return 'text-red-600 bg-red-50';
  return 'text-slate-600 bg-slate-50';
}

function relativeTime(iso: string): string {
  try {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60_000);
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  } catch {
    return '';
  }
}

// ── Skeleton ──────────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <div className="flex items-center gap-2">
          <div className="h-5 w-5 rounded bg-slate-200 animate-pulse" />
          <div className="h-5 w-40 rounded bg-slate-200 animate-pulse" />
        </div>
        <div className="h-4 w-44 rounded bg-slate-100 animate-pulse" />
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="h-10 rounded-lg bg-slate-100 animate-pulse" />
        <div className="h-3 w-full rounded bg-slate-100 animate-pulse" />
        <div className="space-y-2">
          {[1, 2].map((i) => (
            <div key={i} className="h-4 w-full rounded bg-slate-100 animate-pulse" />
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Newspaper className="h-5 w-5 text-blue-500" />
          AI Sentiment
        </CardTitle>
        <CardDescription>News & market sentiment analysis</CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex items-start gap-2 rounded-md bg-slate-50 p-3 text-sm text-slate-500">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
          <span>{message}</span>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Sentiment breakdown bar ───────────────────────────────────────────────────

function BreakdownBar({
  positive_pct,
  negative_pct,
  neutral_pct,
  total,
}: {
  positive_pct: number;
  negative_pct: number;
  neutral_pct: number;
  total: number;
}) {
  if (total === 0) {
    return (
      <div className="overflow-hidden rounded-full bg-slate-100 h-2.5 w-full" />
    );
  }

  return (
    <div className="space-y-1">
      <div className="flex overflow-hidden rounded-full h-2.5 w-full bg-slate-100">
        {positive_pct > 0 && (
          <div
            className="h-full bg-emerald-400 transition-all duration-500"
            style={{ width: `${positive_pct}%` }}
          />
        )}
        {negative_pct > 0 && (
          <div
            className="h-full bg-red-400 transition-all duration-500"
            style={{ width: `${negative_pct}%` }}
          />
        )}
        {neutral_pct > 0 && (
          <div
            className="h-full bg-slate-300 transition-all duration-500"
            style={{ width: `${neutral_pct}%` }}
          />
        )}
      </div>
      <div className="flex justify-between text-xs text-slate-500">
        <span className="text-emerald-600">{positive_pct.toFixed(0)}% positive</span>
        <span>{total} events</span>
        <span className="text-red-600">{negative_pct.toFixed(0)}% negative</span>
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export function AISentimentCard({ data, isLoading, error }: AISentimentCardProps) {
  if (isLoading) return <SkeletonCard />;
  if (error || !data) return <EmptyState message="Unable to load sentiment analysis." />;

  if (data.breakdown.total === 0) {
    return <EmptyState message={`No news events found in the last ${data.lookback_hours}h.`} />;
  }

  const impact = impactLabel(data.impact_score);
  const absScore = Math.abs(data.impact_score);

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-lg">
          <span className="flex items-center gap-2">
            <Newspaper className="h-5 w-5 text-blue-500" />
            AI Sentiment
          </span>
          <span className="text-sm font-normal text-slate-500">
            {data.lookback_hours}h window
          </span>
        </CardTitle>
        <CardDescription>FinBERT · {data.breakdown.total} news events</CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Impact score */}
        <div className={`flex items-center justify-between rounded-lg px-4 py-3 ${impact.bg}`}>
          <div>
            <p className="text-xs font-medium text-slate-500 uppercase tracking-wide">
              Impact Score
            </p>
            <p className={`text-2xl font-bold tabular-nums ${impact.color}`}>
              {data.impact_score > 0 ? '+' : ''}{data.impact_score.toFixed(0)}
            </p>
          </div>
          <div className="text-right">
            <span className={`rounded-full px-2.5 py-1 text-xs font-semibold ${impact.color} ${impact.bg} border border-current/20`}>
              {impact.label}
            </span>
            <p className="mt-1 text-xs text-slate-500">out of ±100</p>
          </div>
        </div>

        {/* Score bar (-100 to +100) */}
        <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-100">
          <div className="absolute inset-y-0 left-1/2 w-0.5 bg-slate-300" />
          <div
            className={`absolute inset-y-0 transition-all duration-500 ${
              data.impact_score >= 0 ? 'left-1/2 bg-emerald-400' : 'right-1/2 bg-red-400'
            }`}
            style={{ width: `${absScore / 2}%` }}
          />
        </div>

        {/* Breakdown bar */}
        <BreakdownBar {...data.breakdown} />

        {/* Top event */}
        {data.top_event && (
          <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
            <div className="mb-1.5 flex items-center justify-between gap-2">
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-semibold capitalize ${sentimentColor(data.top_event.sentiment)}`}
              >
                {data.top_event.sentiment}
              </span>
              <span className="shrink-0 text-xs text-slate-400">
                {data.top_event.published_at ? relativeTime(data.top_event.published_at) : ''}
              </span>
            </div>
            <p className="line-clamp-2 text-xs leading-relaxed text-slate-700">
              {data.top_event.title}
            </p>
            <div className="mt-1.5 flex items-center gap-1 text-xs text-slate-400">
              <ExternalLink className="h-3 w-3" />
              <span className="truncate">{data.top_event.source}</span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
