'use client';

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Brain, TrendingUp, TrendingDown, Clock, AlertCircle } from 'lucide-react';
import type { PatternAnalysisCard } from '@/types/analysis';

interface MLPatternCardProps {
  data: PatternAnalysisCard | null;
  isLoading: boolean;
  error?: boolean;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function patternDisplayName(raw: string): string {
  return raw
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function confidencePct(raw: number): number {
  // TA-Lib returns 100 or 200; normalise to 0-100 %
  return Math.min(100, raw);
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <div className="flex items-center gap-2">
          <div className="h-5 w-5 rounded bg-slate-200 animate-pulse" />
          <div className="h-5 w-36 rounded bg-slate-200 animate-pulse" />
        </div>
        <div className="h-4 w-48 rounded bg-slate-100 animate-pulse" />
      </CardHeader>
      <CardContent className="space-y-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="flex justify-between">
            <div className="h-4 w-24 rounded bg-slate-100 animate-pulse" />
            <div className="h-4 w-16 rounded bg-slate-100 animate-pulse" />
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function EmptyState({ message }: { message: string }) {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-lg">
          <Brain className="h-5 w-5 text-violet-500" />
          ML Pattern Analysis
        </CardTitle>
        <CardDescription>Auto-detected candlestick patterns</CardDescription>
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

// ── Confidence arc (SVG) ───────────────────────────────────────────────────────

function ConfidenceArc({ pct, direction }: { pct: number; direction: 'bullish' | 'bearish' }) {
  const radius = 28;
  const circumference = Math.PI * radius; // half-circle arc
  const offset = circumference - (pct / 100) * circumference;
  const color = direction === 'bullish' ? '#10b981' : '#ef4444';

  return (
    <div className="relative flex flex-col items-center">
      <svg width="72" height="42" viewBox="0 0 72 42">
        {/* Track */}
        <path
          d="M 8 36 A 28 28 0 0 1 64 36"
          fill="none"
          stroke="#e2e8f0"
          strokeWidth="6"
          strokeLinecap="round"
        />
        {/* Progress */}
        <path
          d="M 8 36 A 28 28 0 0 1 64 36"
          fill="none"
          stroke={color}
          strokeWidth="6"
          strokeLinecap="round"
          strokeDasharray={`${circumference}`}
          strokeDashoffset={`${offset}`}
          style={{ transition: 'stroke-dashoffset 0.6s ease' }}
        />
      </svg>
      <span
        className="absolute bottom-0 text-lg font-bold tabular-nums"
        style={{ color }}
      >
        {pct}%
      </span>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export function MLPatternCard({ data, isLoading, error }: MLPatternCardProps) {
  if (isLoading) return <SkeletonCard />;
  if (error || !data) return <EmptyState message="Unable to load pattern analysis." />;

  const best = data.best_pattern;

  if (!best) {
    return (
      <EmptyState message="No patterns detected on any timeframe. Market may be consolidating." />
    );
  }

  const confPct = confidencePct(best.confidence);
  const isBullish = best.direction === 'bullish';
  const DirectionIcon = isBullish ? TrendingUp : TrendingDown;
  const directionColor = isBullish ? 'text-emerald-600' : 'text-red-600';
  const directionBg = isBullish ? 'bg-emerald-50 text-emerald-700' : 'bg-red-50 text-red-700';
  const stats = data.historical_stats;

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-lg">
          <span className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-violet-500" />
            ML Pattern Analysis
          </span>
          {/* Timeframe badge */}
          <span className="flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
            <Clock className="h-3 w-3" />
            {data.timeframe}
          </span>
        </CardTitle>
        <CardDescription>Auto-detected across 1H · 4H · 1D · 1W</CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Pattern name + confidence arc */}
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            {/* Direction badge */}
            <span
              className={`mb-1 inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ${directionBg}`}
            >
              <DirectionIcon className="h-3 w-3" />
              {isBullish ? 'Bullish' : 'Bearish'}
            </span>
            <p className="truncate text-sm font-semibold text-slate-800">
              {patternDisplayName(best.name)}
            </p>
            <p className="text-xs text-slate-400">
              {new Date(best.timestamp).toLocaleDateString('en-IN', {
                day: 'numeric',
                month: 'short',
              })}
            </p>
          </div>
          <ConfidenceArc pct={confPct} direction={best.direction} />
        </div>

        {/* Historical accuracy */}
        {stats && stats.sample_size > 0 ? (
          <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
            <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
              Historical Accuracy · {stats.sample_size} samples
            </p>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs">
              <div className="flex justify-between">
                <span className="text-slate-500">Direction</span>
                <span className={`font-semibold ${(stats.accuracy * 100) >= 60 ? 'text-emerald-600' : 'text-amber-600'}`}>
                  {(stats.accuracy * 100).toFixed(0)}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">Avg move</span>
                <span className="font-semibold text-slate-700">
                  {stats.avg_move_pct >= 0 ? '+' : ''}{stats.avg_move_pct.toFixed(1)}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">TP1 hit</span>
                <span className="font-semibold text-slate-700">
                  {(stats.tp1_hit_rate * 100).toFixed(0)}%
                </span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-500">SL hit</span>
                <span className="font-semibold text-slate-700">
                  {(stats.sl_hit_rate * 100).toFixed(0)}%
                </span>
              </div>
            </div>
          </div>
        ) : (
          <p className="text-xs text-slate-400">
            No historical accuracy data yet — predictions accumulating.
          </p>
        )}

        {/* Total detected footnote */}
        {data.total_detected > 1 && (
          <p className="text-xs text-slate-400">
            +{data.total_detected - 1} more pattern{data.total_detected - 1 > 1 ? 's' : ''} detected on this timeframe
          </p>
        )}
      </CardContent>
    </Card>
  );
}
