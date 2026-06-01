'use client';

import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  AlertCircle,
  BarChart3,
  Brain,
  Clock,
  Cpu,
  Minus,
  TrendingDown,
  TrendingUp,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type {
  MLEnsemblePredictionData,
  MLEnsemblePrediction,
  ModelPrediction,
  PatternAnalysisCard,
} from '@/types/analysis';

interface MLPatternCardProps {
  data:       PatternAnalysisCard    | null;  // TA-Lib pattern signal
  prediction: MLEnsemblePrediction   | null;  // XGBoost + GRU ensemble
  isLoading:  boolean;
  error?:     boolean;
}

// ── Direction config ───────────────────────────────────────────────────────────

const DIR = {
  BUY: {
    badgeBg:    'bg-emerald-600',
    badgeText:  'text-white',
    wrapBg:     'bg-emerald-50',
    wrapBorder: 'border-emerald-200',
    labelText:  'text-emerald-700',
    bar:        'bg-emerald-500',
    icon:       TrendingUp,
  },
  SELL: {
    badgeBg:    'bg-rose-600',
    badgeText:  'text-white',
    wrapBg:     'bg-rose-50',
    wrapBorder: 'border-rose-200',
    labelText:  'text-rose-700',
    bar:        'bg-rose-500',
    icon:       TrendingDown,
  },
  HOLD: {
    badgeBg:    'bg-amber-500',
    badgeText:  'text-white',
    wrapBg:     'bg-amber-50',
    wrapBorder: 'border-amber-200',
    labelText:  'text-amber-700',
    bar:        'bg-amber-400',
    icon:       Minus,
  },
} as const;

type Direction = keyof typeof DIR;

// ── Pure helpers ───────────────────────────────────────────────────────────────

function toDir(raw: string): Direction {
  if (raw === 'BUY' || raw === 'SELL' || raw === 'HOLD') return raw;
  return 'HOLD';
}

function patternDisplayName(raw: string): string {
  return raw.replace(/_/g, ' ').toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
}

function regimeLabel(threshold: number): string {
  if (threshold >= 0.68) return 'High Volatility';
  if (threshold <= 0.57) return 'Low Volatility';
  return 'Normal';
}

function regimeTextColor(threshold: number): string {
  if (threshold >= 0.68) return 'text-orange-600';
  if (threshold <= 0.57) return 'text-blue-600';
  return 'text-slate-500';
}

function priceFmt(n: number): string {
  return n.toLocaleString('en-IN', { maximumFractionDigits: 0 });
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <div className="h-5 w-5 rounded bg-slate-200 animate-pulse" />
            <div className="h-5 w-44 rounded bg-slate-200 animate-pulse" />
          </div>
          <div className="h-5 w-12 rounded-full bg-slate-100 animate-pulse" />
        </div>
        <div className="h-4 w-52 rounded bg-slate-100 animate-pulse" />
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="h-16 w-full rounded-lg bg-slate-100 animate-pulse" />
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="flex items-center gap-2">
              <div className="h-3 w-10 rounded bg-slate-100 animate-pulse" />
              <div className="h-2 flex-1 rounded-full bg-slate-100 animate-pulse" />
              <div className="h-3 w-10 rounded bg-slate-100 animate-pulse" />
            </div>
          ))}
        </div>
        <div className="h-14 w-full rounded-lg bg-slate-50 animate-pulse" />
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
          ML Ensemble Analysis
        </CardTitle>
        <CardDescription>XGBoost + GRU ensemble prediction</CardDescription>
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

// Horizontal probability bar (BUY / SELL / HOLD)
function ProbRow({
  label,
  value,
  barClass,
}: {
  label: string;
  value: number;
  barClass: string;
}) {
  const pct = Math.round(value * 100);
  return (
    <div className="flex items-center gap-2.5">
      <span className="w-10 text-right text-[11px] font-semibold text-slate-600 shrink-0 tabular-nums">
        {label}
      </span>
      <div className="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">
        <div
          className={cn('h-full rounded-full transition-all duration-700', barClass)}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="w-10 text-[11px] font-semibold tabular-nums text-slate-700 shrink-0">
        {pct}%
      </span>
    </div>
  );
}

// Individual model row (XGBoost / GRU)
function ModelRow({
  name,
  model,
}: {
  name: string;
  model: ModelPrediction;
}) {
  const dir = toDir(model.direction);
  const cfg = DIR[dir];
  const DirIcon = cfg.icon;
  const confPct = Math.round(model.confidence * 100);

  return (
    <div className="flex items-center gap-2">
      <div className="flex items-center gap-1.5 w-[76px] shrink-0">
        <span className="text-[11px] font-semibold text-slate-700">{name}</span>
        <span className="rounded bg-slate-100 px-1 py-0.5 text-[10px] font-medium text-slate-500">
          {Math.round(model.weight * 100)}%
        </span>
      </div>
      <span
        className={cn(
          'inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-bold shrink-0',
          cfg.badgeBg,
          cfg.badgeText,
        )}
      >
        <DirIcon className="h-2.5 w-2.5" />
        {dir}
      </span>
      <div className="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
        <div
          className={cn('h-full rounded-full transition-all duration-700', cfg.bar)}
          style={{ width: `${confPct}%` }}
        />
      </div>
      <span className="w-9 text-right text-[11px] font-semibold tabular-nums text-slate-600 shrink-0">
        {confPct}%
      </span>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export function MLPatternCard({ data, prediction, isLoading, error }: MLPatternCardProps) {
  if (isLoading) return <SkeletonCard />;
  if (error || (!prediction && !data)) return <EmptyState message="Unable to load ML analysis." />;

  const hasPrediction = prediction?.available === true;
  const pred          = hasPrediction ? (prediction as MLEnsemblePredictionData) : null;
  const hasPattern    = !!data?.best_pattern;

  // Handle various unavailable states
  if (!hasPrediction && !hasPattern) {
    if (!prediction) return <EmptyState message="No ML analysis available for this instrument." />;
    const reason = (prediction as { available: false; unavailable_reason: string }).unavailable_reason;
    if (reason === 'no_model') {
      return (
        <EmptyState message="No production model deployed. Promote a trained model to enable ensemble predictions." />
      );
    }
    if (reason === 'insufficient_data') {
      return (
        <EmptyState message="Insufficient price history to run predictions. More candle data is needed." />
      );
    }
    return <EmptyState message="ML predictions are temporarily unavailable." />;
  }

  const dir          = pred ? toDir(pred.direction) : null;
  const cfg          = dir ? DIR[dir] : null;
  const DirIcon      = cfg?.icon ?? Brain;
  const confPct      = pred ? Math.round(pred.confidence       * 100) : 0;
  const convictionPct = pred ? Math.round(pred.conviction_scale * 100) : 0;
  const thresholdPct = pred ? Math.round(pred.threshold         * 100) : 60;

  const displayTF    = pred?.timeframe?.toUpperCase() ?? data?.timeframe ?? '1D';
  const versionBadge = pred?.xgboost_version ? `v${pred.xgboost_version}` : null;

  const best  = data?.best_pattern;
  const stats = data?.historical_stats;

  const xgb = pred?.models?.xgboost ?? null;
  const gru = pred?.models?.gru     ?? null;

  // Only show price levels when entry price is meaningful
  const showPriceLevels = pred !== null && pred.entry_price > 0;

  return (
    <Card className="border-slate-200/80 bg-white/90">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center justify-between text-lg">
          <span className="flex items-center gap-2">
            <Brain className="h-5 w-5 text-violet-500" />
            ML Ensemble Analysis
          </span>
          <span className="flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
            <Clock className="h-3 w-3" />
            {displayTF}
          </span>
        </CardTitle>
        <CardDescription>
          XGBoost + GRU ensemble
          {versionBadge ? ` · ${versionBadge}` : ''}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">

        {/* ── Section 1: Ensemble Verdict ────────────────────────────────── */}
        {pred && dir && cfg && (
          <div
            className={cn(
              'rounded-lg border p-3 space-y-2.5',
              cfg.wrapBg,
              cfg.wrapBorder,
            )}
          >
            <div className="flex items-center gap-3">
              <span
                className={cn(
                  'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-black tracking-wide uppercase shrink-0',
                  cfg.badgeBg,
                  cfg.badgeText,
                )}
              >
                <DirIcon className="h-4 w-4" />
                {dir}
              </span>
              <div className="min-w-0">
                <p className={cn('text-base font-bold leading-tight', cfg.labelText)}>
                  {confPct}% confidence
                </p>
                <p className={cn('text-[11px] leading-tight', regimeTextColor(pred.threshold))}>
                  {regimeLabel(pred.threshold)} regime · threshold {thresholdPct}%
                </p>
              </div>
            </div>

            {/* Conviction bar */}
            <div className="space-y-1">
              <div className="flex justify-between text-[10px]">
                <span className="text-slate-500">Conviction above threshold</span>
                <span className={cn('font-semibold tabular-nums', cfg.labelText)}>
                  {convictionPct}%
                </span>
              </div>
              <div className="h-1.5 bg-slate-200/80 rounded-full overflow-hidden">
                <div
                  className={cn('h-full rounded-full transition-all duration-700', cfg.bar)}
                  style={{ width: `${convictionPct}%` }}
                />
              </div>
            </div>
          </div>
        )}

        {/* ── Section 2: Class Probabilities ─────────────────────────────── */}
        {pred?.probabilities && (
          <div className="space-y-1.5">
            <p className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <BarChart3 className="h-3 w-3" />
              Class Probabilities
            </p>
            <div className="space-y-1.5">
              <ProbRow label="BUY"  value={pred.probabilities.buy}  barClass="bg-emerald-500" />
              <ProbRow label="SELL" value={pred.probabilities.sell} barClass="bg-rose-500" />
              <ProbRow label="HOLD" value={pred.probabilities.hold} barClass="bg-amber-400" />
            </div>
          </div>
        )}

        {/* ── Section 3: Model Breakdown ─────────────────────────────────── */}
        {pred && (xgb || gru) && (
          <div className="space-y-1.5">
            <p className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              <Cpu className="h-3 w-3" />
              Model Breakdown
            </p>
            <div className="space-y-1.5">
              {xgb && <ModelRow name="XGBoost" model={xgb} />}
              {gru && <ModelRow name="GRU"     model={gru} />}
            </div>
          </div>
        )}

        {/* ── Section 4: Pattern Signal (supporting) ─────────────────────── */}
        {hasPattern && best && (
          <>
            {hasPrediction && (
              <div className="border-t border-slate-100" />
            )}
            <div
              className={cn(
                'rounded-lg border p-2.5',
                best.direction === 'bullish'
                  ? 'bg-emerald-50 border-emerald-200'
                  : 'bg-rose-50 border-rose-200',
              )}
            >
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2 min-w-0">
                  <span
                    className={cn(
                      'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold shrink-0',
                      best.direction === 'bullish'
                        ? 'bg-emerald-100 text-emerald-700'
                        : 'bg-rose-100 text-rose-700',
                    )}
                  >
                    {best.direction === 'bullish'
                      ? <TrendingUp className="h-3 w-3" />
                      : <TrendingDown className="h-3 w-3" />}
                    {best.direction === 'bullish' ? 'Bullish' : 'Bearish'}
                  </span>
                  <span className="text-xs font-semibold text-slate-800 truncate">
                    {patternDisplayName(best.name)}
                  </span>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] text-slate-500">{data?.timeframe}</span>
                  {stats && stats.sample_size > 0 && (
                    <span
                      className={cn(
                        'text-[11px] font-semibold tabular-nums',
                        stats.accuracy * 100 >= 60 ? 'text-emerald-600' : 'text-amber-600',
                      )}
                    >
                      {Math.round(stats.accuracy * 100)}% hist. acc
                    </span>
                  )}
                </div>
              </div>
            </div>
          </>
        )}

        {/* ── Section 5: Price Levels ─────────────────────────────────────── */}
        {showPriceLevels && pred && (
          <>
            <div className="border-t border-slate-100" />
            <div className="grid grid-cols-5 gap-1 text-center">
              {(
                [
                  { label: 'Entry', value: pred.entry_price, color: 'text-slate-700' },
                  { label: 'SL',    value: pred.stop_loss,   color: 'text-rose-600'  },
                  { label: 'TP1',   value: pred.tp1,         color: 'text-emerald-600' },
                  { label: 'TP2',   value: pred.tp2,         color: 'text-emerald-500' },
                  { label: 'TP3',   value: pred.tp3,         color: 'text-emerald-400' },
                ] as const
              ).map(({ label, value, color }) => (
                <div key={label} className="rounded bg-slate-50 px-1 py-1.5">
                  <p className="text-[9px] uppercase tracking-wide text-slate-400 mb-0.5">
                    {label}
                  </p>
                  <p className={cn('text-[11px] font-semibold tabular-nums truncate', color)}>
                    ₹{priceFmt(value)}
                  </p>
                </div>
              ))}
            </div>
          </>
        )}

      </CardContent>
    </Card>
  );
}
