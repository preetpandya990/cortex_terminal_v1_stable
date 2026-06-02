'use client';

import { useMemo } from 'react';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  AlertTriangle,
  ArrowRight,
  Check,
  Minus,
  Sparkles,
  TrendingDown,
  TrendingUp,
  X,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import type {
  MLEnsemblePrediction,
  PatternAnalysisCard,
  PredictionSynthesis,
  SentimentAnalysisCard,
} from '@/types/analysis';
import {
  synthesize,
  buildEnsemblePanelContent,
  buildPatternPanelContent,
  buildSentimentPanelContent,
} from '@/lib/prediction-synthesis';
import type { Alignment, Direction, PanelContent, SynthesisResult } from '@/lib/prediction-synthesis';

// ── Rendering config maps ──────────────────────────────────────────────────────

const DIR_CONFIG = {
  BUY: {
    badgeBg:    'bg-emerald-600',
    badgeText:  'text-white',
    wrapBg:     'bg-emerald-50',
    wrapBorder: 'border-emerald-200',
    labelText:  'text-emerald-700',
    accentBar:  'bg-emerald-500',
    icon:       TrendingUp,
  },
  SELL: {
    badgeBg:    'bg-rose-600',
    badgeText:  'text-white',
    wrapBg:     'bg-rose-50',
    wrapBorder: 'border-rose-200',
    labelText:  'text-rose-700',
    accentBar:  'bg-rose-500',
    icon:       TrendingDown,
  },
  HOLD: {
    badgeBg:    'bg-amber-500',
    badgeText:  'text-white',
    wrapBg:     'bg-amber-50',
    wrapBorder: 'border-amber-200',
    labelText:  'text-amber-700',
    accentBar:  'bg-amber-400',
    icon:       Minus,
  },
} as const;

const ALIGNMENT_CONFIG = {
  confirms: {
    badgeClass:  'bg-emerald-100 text-emerald-700',
    borderClass: 'border-l-emerald-400',
    bgClass:     'bg-emerald-50/50',
    dotClass:    'bg-emerald-500',
    label:       'Confirms',
    icon:        Check,
  },
  conflicts: {
    badgeClass:  'bg-rose-100 text-rose-700',
    borderClass: 'border-l-rose-400',
    bgClass:     'bg-rose-50/50',
    dotClass:    'bg-rose-500',
    label:       'Conflicts',
    icon:        X,
  },
  neutral: {
    badgeClass:  'bg-amber-100 text-amber-700',
    borderClass: 'border-l-amber-300',
    bgClass:     'bg-amber-50/30',
    dotClass:    'bg-amber-400',
    label:       'Neutral',
    icon:        ArrowRight,
  },
  absent: {
    badgeClass:  'bg-slate-100 text-slate-500',
    borderClass: 'border-l-slate-200',
    bgClass:     'bg-slate-50/50',
    dotClass:    'bg-slate-300',
    label:       'Absent',
    icon:        Minus,
  },
} as const;

const STRENGTH_LABEL: Record<PredictionSynthesis['signal_strength'], string> = {
  strong:   'High Conviction',
  moderate: 'Moderate Conviction',
  weak:     'Low Conviction',
};

// ── Sub-components ─────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader>
        <div className="flex items-center gap-2">
          <div className="h-5 w-5 rounded bg-slate-200 animate-pulse" />
          <div className="h-5 w-44 rounded bg-slate-200 animate-pulse" />
        </div>
        <div className="h-4 w-40 rounded bg-slate-100 animate-pulse" />
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="h-16 w-full rounded-lg bg-slate-100 animate-pulse" />
        <div className="space-y-2">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-12 w-full rounded-lg bg-slate-50 animate-pulse" />
          ))}
        </div>
        <div className="h-14 w-full rounded-lg bg-slate-50 animate-pulse" />
      </CardContent>
    </Card>
  );
}

function SignalPanel({
  content,
  alignment,
}: {
  content:   PanelContent;
  alignment: Alignment;
}) {
  const cfg       = ALIGNMENT_CONFIG[alignment];
  const AlignIcon = cfg.icon;

  return (
    <div
      className={cn(
        'rounded-r-lg border-l-2 py-2.5 pl-3 pr-2.5',
        cfg.borderClass,
        cfg.bgClass,
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1">
            {content.label}
          </p>
          <p className="text-xs font-semibold text-slate-800 leading-snug">
            {content.headline}
          </p>
          <p className="mt-0.5 text-[11px] leading-snug text-slate-500">
            {content.detail}
          </p>
        </div>
        <span
          className={cn(
            'mt-0.5 inline-flex shrink-0 items-center gap-0.5 rounded-full px-2 py-0.5 text-[10px] font-bold',
            cfg.badgeClass,
          )}
        >
          <AlignIcon className="h-2.5 w-2.5" />
          {cfg.label}
        </span>
      </div>
    </div>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

interface PredictionSummaryCardProps {
  patternData:   PatternAnalysisCard   | null;
  sentimentData: SentimentAnalysisCard | null;
  prediction:    MLEnsemblePrediction  | null;
  isLoading:     boolean;
}

export function PredictionSummaryCard({
  patternData,
  sentimentData,
  prediction,
  isLoading,
}: PredictionSummaryCardProps) {
  const synthesis = useMemo(
    () => synthesize(prediction, patternData, sentimentData),
    [prediction, patternData, sentimentData],
  );

  if (isLoading) return <SkeletonCard />;

  if (!synthesis) {
    return (
      <Card className="border-slate-200/80 bg-white/90">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-lg">
            <Sparkles className="h-5 w-5 text-amber-500" />
            Prediction Summary
          </CardTitle>
          <CardDescription>Ensemble + pattern + sentiment synthesis</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-start gap-2 rounded-md bg-slate-50 p-3 text-sm text-slate-500">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            <span>Awaiting analysis data from all three sources.</span>
          </div>
        </CardContent>
      </Card>
    );
  }

  const dir     = synthesis.recommendation;
  const cfg     = DIR_CONFIG[dir];
  const DirIcon = cfg.icon;
  const confPct = Math.round(synthesis.confidence * 100);

  const ensembleContent  = buildEnsemblePanelContent(prediction);
  const patternContent   = buildPatternPanelContent(patternData);
  const sentimentContent = buildSentimentPanelContent(sentimentData);

  return (
    <Card className="border-slate-200/80 bg-white/90">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-lg">
          <Sparkles className="h-5 w-5 text-amber-500" />
          Prediction Summary
        </CardTitle>
        <CardDescription>
          Ensemble + pattern + sentiment · {STRENGTH_LABEL[synthesis.signal_strength]}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-3">

        {/* ── Final Verdict block ─────────────────────────────────────────── */}
        <div
          className={cn(
            'rounded-lg border p-3',
            cfg.wrapBg,
            cfg.wrapBorder,
          )}
        >
          <div className="flex items-center gap-3">
            {/* Direction badge */}
            <span
              className={cn(
                'inline-flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-black uppercase tracking-wide',
                cfg.badgeBg,
                cfg.badgeText,
              )}
            >
              <DirIcon className="h-4 w-4" />
              {dir}
            </span>

            {/* Conviction + source dots */}
            <div className="flex-1 min-w-0">
              <p className={cn('text-sm font-bold leading-tight', cfg.labelText)}>
                {STRENGTH_LABEL[synthesis.signal_strength]}
              </p>
              <div className="mt-0.5 flex items-center gap-1.5">
                {(
                  [
                    synthesis.ensembleAlignment,
                    synthesis.patternAlignment,
                    synthesis.sentimentAlignment,
                  ] as Alignment[]
                ).map((al, i) => (
                  <div
                    key={i}
                    className={cn('h-2 w-2 shrink-0 rounded-full', ALIGNMENT_CONFIG[al].dotClass)}
                  />
                ))}
                <span className="text-[11px] text-slate-500">
                  {synthesis.confirmed_count} of 3 sources confirm
                </span>
              </div>
            </div>

            {/* Confidence */}
            <div className="shrink-0 text-right">
              <p className="mb-1 text-[10px] text-slate-500">Confidence</p>
              <div className="flex items-center gap-1.5">
                <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
                  <div
                    className={cn(
                      'h-full rounded-full transition-all duration-700',
                      cfg.accentBar,
                    )}
                    style={{ width: `${confPct}%` }}
                  />
                </div>
                <span
                  className={cn(
                    'text-sm font-bold tabular-nums',
                    cfg.labelText,
                  )}
                >
                  {confPct}%
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* ── Signal Breakdown ────────────────────────────────────────────── */}
        <div className="space-y-1.5">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
            Signal Breakdown
          </p>
          <SignalPanel
            content={ensembleContent}
            alignment={synthesis.ensembleAlignment}
          />
          <SignalPanel
            content={patternContent}
            alignment={synthesis.patternAlignment}
          />
          <SignalPanel
            content={sentimentContent}
            alignment={synthesis.sentimentAlignment}
          />
        </div>

        {/* ── Synthesis reasoning ─────────────────────────────────────────── */}
        <div className="rounded-md border border-slate-100 bg-slate-50 px-3 py-2.5">
          <p className="text-xs leading-relaxed text-slate-600">
            {synthesis.synthesis_text}
          </p>
        </div>

        {/* ── Disclaimer ──────────────────────────────────────────────────── */}
        <p className="text-[10px] leading-relaxed text-slate-400">
          AI-generated analysis for informational purposes only. Not financial advice.
          Past model performance does not guarantee future results.
        </p>

      </CardContent>
    </Card>
  );
}
