'use client';

import { useMemo } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Sparkles, TrendingUp, TrendingDown, Minus, Brain, Newspaper, AlertTriangle } from 'lucide-react';
import type { PatternAnalysisCard, SentimentAnalysisCard, PredictionSynthesis } from '@/types/analysis';

interface PredictionSummaryCardProps {
  patternData: PatternAnalysisCard | null;
  sentimentData: SentimentAnalysisCard | null;
  isLoading: boolean;
}

// ── Synthesis logic ────────────────────────────────────────────────────────────

function synthesize(
  pattern: PatternAnalysisCard | null,
  sentiment: SentimentAnalysisCard | null,
): PredictionSynthesis | null {
  if (!pattern && !sentiment) return null;

  // Directional signals
  const mlBullish = pattern?.best_pattern?.direction === 'bullish';
  const mlBearish = pattern?.best_pattern?.direction === 'bearish';
  const mlConfidence = pattern?.best_pattern
    ? Math.min(pattern.best_pattern.confidence, 100) / 100
    : 0;

  const sentimentScore = sentiment?.impact_score ?? 0;
  const aiBullish = sentimentScore >= 15;
  const aiBearish = sentimentScore <= -15;
  const aiConfidence = Math.abs(sentimentScore) / 100;

  // Determine recommendation
  let recommendation: 'BUY' | 'SELL' | 'HOLD';
  let confidence: number;
  let signal_strength: 'strong' | 'moderate' | 'weak';

  if (mlBullish && aiBullish) {
    recommendation = 'BUY';
    confidence = (mlConfidence * 0.6 + aiConfidence * 0.4);
    signal_strength = confidence >= 0.65 ? 'strong' : 'moderate';
  } else if (mlBearish && aiBearish) {
    recommendation = 'SELL';
    confidence = (mlConfidence * 0.6 + aiConfidence * 0.4);
    signal_strength = confidence >= 0.65 ? 'strong' : 'moderate';
  } else if (mlBullish && !aiBearish) {
    recommendation = 'BUY';
    confidence = mlConfidence * 0.55;
    signal_strength = 'weak';
  } else if (mlBearish && !aiBullish) {
    recommendation = 'SELL';
    confidence = mlConfidence * 0.55;
    signal_strength = 'weak';
  } else {
    recommendation = 'HOLD';
    confidence = 0.5;
    signal_strength = 'weak';
  }

  // ML insight text
  const ml_insight = pattern?.best_pattern
    ? `${pattern.best_pattern.direction === 'bullish' ? '↑' : '↓'} ${pattern.best_pattern.name
        .replace(/_/g, ' ')
        .toLowerCase()
        .replace(/\b\w/g, (c) => c.toUpperCase())} detected on ${pattern.timeframe}`
    : 'No strong pattern detected across timeframes';

  // AI insight text
  const ai_insight = sentiment
    ? `${sentiment.breakdown.total} news events · impact ${
        sentimentScore > 0 ? '+' : ''
      }${sentimentScore.toFixed(0)} (${sentiment.sentiment_label})`
    : 'Sentiment data unavailable';

  // Action text
  const action_text =
    recommendation !== 'HOLD'
      ? `Consider ${recommendation === 'BUY' ? 'a long entry' : 'a short entry'} — ${
          signal_strength === 'strong'
            ? 'both ML pattern and news sentiment confirm direction'
            : signal_strength === 'moderate'
            ? 'signals moderately aligned'
            : 'weak confirmation — proceed with caution'
        }.`
      : 'Conflicting signals — hold and wait for clearer confluence.';

  return {
    recommendation,
    confidence: Math.min(1, Math.max(0, confidence)),
    ml_insight,
    ai_insight,
    action_text,
    signal_strength,
  };
}

// ── Helpers ───────────────────────────────────────────────────────────────────

const VERDICT_STYLES = {
  BUY: {
    bg: 'bg-emerald-50 border-emerald-200',
    badge: 'bg-emerald-500 text-white',
    text: 'text-emerald-700',
    icon: TrendingUp,
  },
  SELL: {
    bg: 'bg-red-50 border-red-200',
    badge: 'bg-red-500 text-white',
    text: 'text-red-700',
    icon: TrendingDown,
  },
  HOLD: {
    bg: 'bg-amber-50 border-amber-200',
    badge: 'bg-amber-400 text-white',
    text: 'text-amber-700',
    icon: Minus,
  },
} as const;

const STRENGTH_LABEL = {
  strong: 'Strong Signal',
  moderate: 'Moderate Signal',
  weak: 'Weak Signal',
} as const;

// ── Skeleton ──────────────────────────────────────────────────────────────────

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
      <CardContent className="space-y-4">
        <div className="h-16 w-full rounded-xl bg-slate-100 animate-pulse" />
        <div className="h-4 w-full rounded bg-slate-100 animate-pulse" />
        <div className="space-y-2">
          <div className="h-4 w-full rounded bg-slate-100 animate-pulse" />
          <div className="h-4 w-3/4 rounded bg-slate-100 animate-pulse" />
        </div>
      </CardContent>
    </Card>
  );
}

// ── Main component ─────────────────────────────────────────────────────────────

export function PredictionSummaryCard({
  patternData,
  sentimentData,
  isLoading,
}: PredictionSummaryCardProps) {
  const synthesis = useMemo(
    () => synthesize(patternData, sentimentData),
    [patternData, sentimentData],
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
          <CardDescription>AI + ML synthesis</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-start gap-2 rounded-md bg-slate-50 p-3 text-sm text-slate-500">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-400" />
            <span>Awaiting data from pattern analysis and sentiment engine.</span>
          </div>
        </CardContent>
      </Card>
    );
  }

  const styles = VERDICT_STYLES[synthesis.recommendation];
  const VerdictIcon = styles.icon;
  const confPct = Math.round(synthesis.confidence * 100);

  return (
    <Card className="border-slate-200/80 bg-white/90">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-lg">
          <Sparkles className="h-5 w-5 text-amber-500" />
          Prediction Summary
        </CardTitle>
        <CardDescription>ML + AI synthesis · {STRENGTH_LABEL[synthesis.signal_strength]}</CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        {/* Verdict badge + confidence */}
        <div className={`flex items-center justify-between rounded-xl border px-4 py-3 ${styles.bg}`}>
          <div className="flex items-center gap-3">
            <span className={`flex h-10 w-10 items-center justify-center rounded-full text-lg font-black ${styles.badge}`}>
              {synthesis.recommendation}
            </span>
            <div>
              <p className={`text-sm font-semibold ${styles.text}`}>
                {synthesis.signal_strength === 'strong'
                  ? 'High conviction'
                  : synthesis.signal_strength === 'moderate'
                  ? 'Moderate conviction'
                  : 'Low conviction'}
              </p>
              <VerdictIcon className={`h-4 w-4 ${styles.text}`} />
            </div>
          </div>

          {/* Confidence bar */}
          <div className="text-right">
            <p className="text-xs text-slate-500 mb-1">Confidence</p>
            <div className="flex items-center gap-2">
              <div className="h-1.5 w-20 overflow-hidden rounded-full bg-slate-200">
                <div
                  className={`h-full rounded-full transition-all duration-700 ${
                    synthesis.recommendation === 'BUY'
                      ? 'bg-emerald-500'
                      : synthesis.recommendation === 'SELL'
                      ? 'bg-red-500'
                      : 'bg-amber-400'
                  }`}
                  style={{ width: `${confPct}%` }}
                />
              </div>
              <span className="text-sm font-semibold text-slate-700 tabular-nums">{confPct}%</span>
            </div>
          </div>
        </div>

        {/* Insights */}
        <div className="space-y-2">
          <div className="flex items-start gap-2 text-sm">
            <Brain className="mt-0.5 h-4 w-4 shrink-0 text-violet-500" />
            <span className="text-slate-700">{synthesis.ml_insight}</span>
          </div>
          <div className="flex items-start gap-2 text-sm">
            <Newspaper className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
            <span className="text-slate-700">{synthesis.ai_insight}</span>
          </div>
        </div>

        {/* Action text */}
        <p className="rounded-md bg-slate-50 px-3 py-2 text-xs leading-relaxed text-slate-600">
          {synthesis.action_text}
        </p>

        {/* Disclaimer */}
        <p className="text-[10px] leading-relaxed text-slate-400">
          AI-generated analysis for informational purposes only. Not financial advice.
          Past pattern performance does not guarantee future results.
        </p>
      </CardContent>
    </Card>
  );
}
