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
  MLEnsemblePredictionData,
  PatternAnalysisCard,
  PredictionSynthesis,
  SentimentAnalysisCard,
} from '@/types/analysis';

// ── Internal types ─────────────────────────────────────────────────────────────

type Direction = 'BUY' | 'SELL' | 'HOLD';
type Alignment = 'confirms' | 'conflicts' | 'neutral' | 'absent';

/** synthesize() return — extends the exported PredictionSynthesis with per-source alignments */
interface SynthesisResult extends PredictionSynthesis {
  ensembleAlignment:  Alignment;
  patternAlignment:   Alignment;
  sentimentAlignment: Alignment;
}

interface PanelContent {
  label:    string;
  headline: string;
  detail:   string;
}

// ── Config maps ────────────────────────────────────────────────────────────────

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

// ── Alignment helpers ──────────────────────────────────────────────────────────

function computeEnsembleAlignment(
  pred:    MLEnsemblePredictionData | null,
  verdict: Direction,
): Alignment {
  if (!pred) return 'absent';
  if (pred.direction === 'HOLD' || pred.conviction_scale === 0) return 'neutral';
  return pred.direction === verdict ? 'confirms' : 'conflicts';
}

function computePatternAlignment(
  patternData: PatternAnalysisCard | null,
  verdict:     Direction,
): Alignment {
  const p = patternData?.best_pattern;
  if (!p) return 'absent';
  return (p.direction === 'bullish' ? 'BUY' : 'SELL') === verdict ? 'confirms' : 'conflicts';
}

function computeSentimentAlignment(
  sentimentData: SentimentAnalysisCard | null,
  verdict:       Direction,
): Alignment {
  if (!sentimentData || sentimentData.breakdown.total === 0) return 'absent';
  const score = sentimentData.impact_score;
  if (Math.abs(score) < 15) return 'neutral';
  return (score > 0 ? 'BUY' : 'SELL') === verdict ? 'confirms' : 'conflicts';
}

// ── Synthesis text generator ───────────────────────────────────────────────────

function buildSynthesisText(
  recommendation:    Direction,
  confirmedCount:    number,
  ea:                Alignment,  // ensemble
  pa:                Alignment,  // pattern
  sa:                Alignment,  // sentiment
): string {
  const dir         = recommendation === 'BUY' ? 'bullish' : recommendation === 'SELL' ? 'bearish' : 'neutral';
  const conflictCnt = [ea, pa, sa].filter(a => a === 'conflicts').length;

  if (recommendation === 'HOLD' && confirmedCount === 0 && conflictCnt === 0) {
    if (ea === 'absent') {
      return 'No ensemble model is deployed. Pattern and sentiment provide insufficient signal to form a directional view. Hold and await further data.';
    }
    return 'The ensemble is at or below its confidence threshold. No source produces a high-conviction directional signal. Wait for clearer market confluence before committing a position.';
  }

  if (confirmedCount === 3) {
    return `All three sources — ensemble model, candlestick pattern, and news sentiment — align ${dir}. Signals are mutually reinforcing. High conviction.`;
  }

  if (confirmedCount === 2 && conflictCnt === 0) {
    const neutralName = firstOfAlignment([ea, pa, sa], ['neutral', 'absent'],
      ['the ensemble', 'the pattern signal', 'sentiment']);
    const neutralState = [ea, pa, sa].find(a =>
      a === 'neutral' || a === 'absent') === 'absent'
        ? 'absent — no data available to corroborate or oppose'
        : 'sub-threshold — mildly supportive but not a confirmed signal';
    return `Two of three sources confirm ${dir}. ${neutralName} is ${neutralState}. No opposing evidence — moderate conviction.`;
  }

  if (confirmedCount === 2 && conflictCnt === 1) {
    const conflictName = firstOfAlignment([ea, pa, sa], ['conflicts'],
      ['the ensemble model', 'the pattern signal', 'sentiment']);
    return `Two sources confirm ${dir}, but ${conflictName} signals the opposite direction. Conflicting evidence lowers conviction — size position conservatively.`;
  }

  if (confirmedCount === 1 && ea === 'confirms') {
    return `The ensemble predicts ${dir} but receives no confirmation from pattern or sentiment. Signal is present but unvalidated by secondary sources. Low conviction.`;
  }

  if (confirmedCount === 1) {
    return `Only one secondary source confirms ${dir} with the ensemble absent or suppressed. Insufficient confluence for a reliable directional call.`;
  }

  if (conflictCnt > 0) {
    return 'Sources are in direct disagreement. No consistent directional signal can be established. Hold and wait for signal resolution.';
  }

  return 'Signals are neutral or absent across all sources. No actionable directional view. Await stronger market data before committing a position.';
}

function firstOfAlignment(
  alignments: Alignment[],
  targets:    Alignment[],
  names:      string[],
): string {
  const idx = alignments.findIndex(a => targets.includes(a));
  return idx >= 0 ? names[idx] : names[0];
}

// ── Panel content builders ─────────────────────────────────────────────────────

function buildEnsemblePanelContent(
  prediction: MLEnsemblePrediction | null,
): PanelContent {
  const pred = prediction?.available ? (prediction as MLEnsemblePredictionData) : null;

  if (!pred) {
    const reason = (prediction as { available: false; unavailable_reason?: string } | null)
      ?.unavailable_reason;
    return {
      label:    'ML Ensemble',
      headline: reason === 'insufficient_data'
        ? 'Insufficient price history for predictions'
        : 'Model not deployed',
      detail: reason === 'insufficient_data'
        ? 'More historical candle data is needed to run ensemble inference.'
        : 'Promote a trained model to enable XGBoost + GRU predictions.',
    };
  }

  const confPct  = Math.round(pred.confidence       * 100);
  const convPct  = Math.round(pred.conviction_scale * 100);
  const threshPct = Math.round(pred.threshold        * 100);
  const isHeld   = pred.direction === 'HOLD' || pred.conviction_scale === 0;

  const headline = isHeld
    ? `HOLD (gate suppressed) · ${confPct}% confidence at ${threshPct}% threshold`
    : `${pred.direction} · ${confPct}% confidence · ${convPct}% conviction above ${threshPct}%`;

  const xgb = pred.models.xgboost;
  const gru = pred.models.gru;
  let detail: string;

  if (xgb && gru) {
    const agree = xgb.direction === gru.direction;
    detail = `XGBoost ${Math.round(xgb.confidence * 100)}% · GRU ${Math.round(gru.confidence * 100)}%`
      + `  —  ${agree ? 'both models confirm direction' : 'models diverge on direction'}`;
  } else if (xgb) {
    detail = `XGBoost ${Math.round(xgb.confidence * 100)}% (GRU not loaded)`
      + `  ·  Buy ${Math.round(pred.probabilities.buy * 100)}% / `
      + `Sell ${Math.round(pred.probabilities.sell * 100)}% / `
      + `Hold ${Math.round(pred.probabilities.hold * 100)}%`;
  } else {
    detail = `Buy ${Math.round(pred.probabilities.buy * 100)}% · `
      + `Sell ${Math.round(pred.probabilities.sell * 100)}% · `
      + `Hold ${Math.round(pred.probabilities.hold * 100)}%`;
  }

  return { label: 'ML Ensemble', headline, detail };
}

function buildPatternPanelContent(patternData: PatternAnalysisCard | null): PanelContent {
  const best  = patternData?.best_pattern;
  const stats = patternData?.historical_stats;

  if (!best) {
    return {
      label:    'Pattern Signal',
      headline: 'No pattern detected',
      detail:   'No candlestick pattern found across any ingested timeframe.',
    };
  }

  const dirLabel = best.direction === 'bullish' ? 'Bullish' : 'Bearish';
  const name     = best.name
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());

  const headline = `${dirLabel} ${name} · ${patternData?.timeframe ?? ''}`;

  let detail: string;
  if (stats && stats.sample_size > 0) {
    const acc  = Math.round(stats.accuracy * 100);
    const sign = stats.avg_move_pct >= 0 ? '+' : '';
    detail = `${acc}% historical accuracy · avg move ${sign}${stats.avg_move_pct.toFixed(1)}%`;
    if (stats.tp1_hit_rate > 0) {
      detail += ` · TP1 hit ${Math.round(stats.tp1_hit_rate * 100)}%`;
    }
  } else {
    detail = 'Accumulating historical performance data for this pattern.';
  }

  return { label: 'Pattern Signal', headline, detail };
}

function buildSentimentPanelContent(
  sentimentData: SentimentAnalysisCard | null,
): PanelContent {
  if (!sentimentData || sentimentData.breakdown.total === 0) {
    return {
      label:    'AI Sentiment',
      headline: 'No sentiment data',
      detail:   sentimentData
        ? `No news events found in the last ${sentimentData.lookback_hours}h.`
        : 'Sentiment service unavailable.',
    };
  }

  const score = sentimentData.impact_score;
  const sign  = score > 0 ? '+' : '';
  const total = sentimentData.breakdown.total;
  const absScore = Math.abs(score);

  const impactLabel =
    absScore >= 40 ? (score > 0 ? 'Very Bullish' : 'Very Bearish') :
    absScore >= 15 ? (score > 0 ? 'Bullish'      : 'Bearish')      :
    'Neutral';

  const headline = `Impact ${sign}${score.toFixed(0)} · ${total} event${total !== 1 ? 's' : ''} · ${impactLabel}`;

  const pos = Math.round(sentimentData.breakdown.positive_pct);
  const neg = Math.round(sentimentData.breakdown.negative_pct);
  const neu = Math.round(sentimentData.breakdown.neutral_pct);
  let detail = `${pos}% positive · ${neu}% neutral · ${neg}% negative`;

  if (absScore < 15) {
    detail += ' · Below ±15 threshold — not a confirmed directional signal';
  } else if (sentimentData.top_event) {
    const t   = sentimentData.top_event;
    const txt = t.title.length > 55 ? t.title.slice(0, 55) + '…' : t.title;
    detail += ` · "${txt}"`;
  }

  return { label: 'AI Sentiment', headline, detail };
}

// ── Core synthesis function ────────────────────────────────────────────────────

function synthesize(
  prediction:    MLEnsemblePrediction  | null,
  patternData:   PatternAnalysisCard   | null,
  sentimentData: SentimentAnalysisCard | null,
): SynthesisResult | null {
  const hasPred = prediction?.available === true;
  const pred    = hasPred ? (prediction as MLEnsemblePredictionData) : null;

  if (!hasPred && !patternData && !sentimentData) return null;

  // ── Primary ML signal ──
  // Use pred.confidence directly — conviction_scale only gates whether the model
  // is above the regime threshold (used in mlBullish/mlBearish) and for the
  // signal_strength label. Multiplying confidence by conviction_scale causes
  // near-threshold models (conviction_scale ≈ 0) to produce near-zero confidence
  // even when the raw class probability is high (e.g. 68% BUY → 3% effective).
  const mlBullish = pred?.direction === 'BUY'  && (pred?.conviction_scale ?? 0) > 0;
  const mlBearish = pred?.direction === 'SELL' && (pred?.conviction_scale ?? 0) > 0;
  const mlConf    = pred?.confidence ?? 0;

  // ── Pattern fallback (only when ensemble unavailable) ──
  const patternBullish = !hasPred && patternData?.best_pattern?.direction === 'bullish';
  const patternBearish = !hasPred && patternData?.best_pattern?.direction === 'bearish';
  const patternConf    = patternData?.best_pattern
    ? Math.min(patternData.best_pattern.confidence, 100) / 100
    : 0;

  const effectiveBullish = mlBullish || patternBullish;
  const effectiveBearish = mlBearish || patternBearish;
  const effectiveConf    = hasPred ? mlConf : patternConf * 0.65;

  // ── Sentiment signal ──
  const sentScore   = sentimentData?.impact_score ?? 0;
  const sentBullish = sentScore >=  15;
  const sentBearish = sentScore <= -15;
  // Normalise to 0–1 but cap at 0.6 — raw impact_score skews negative in practice
  // (negative news dominates financial feeds), which would inflate SELL confidence
  // relative to BUY if left uncapped.
  const sentConf    = Math.min(Math.abs(sentScore) / 100, 0.60);

  // ── Recommendation ──
  // Formula is symmetric for BUY and SELL: ML carries 70% weight, sentiment 30%
  // when both agree.  When sentiment is neutral/absent, ML carries 75% with no
  // additional penalty — the previous 0.55 haircut was the main cause of BUY
  // confidence being capped at ~10% on near-threshold models.
  let recommendation:  Direction;
  let confidence:      number;
  let signal_strength: PredictionSynthesis['signal_strength'];

  if (effectiveBullish && sentBullish) {
    recommendation  = 'BUY';
    confidence      = effectiveConf * 0.70 + sentConf * 0.30;
    signal_strength = confidence >= 0.65 ? 'strong' : 'moderate';
  } else if (effectiveBearish && sentBearish) {
    recommendation  = 'SELL';
    confidence      = effectiveConf * 0.70 + sentConf * 0.30;
    signal_strength = confidence >= 0.65 ? 'strong' : 'moderate';
  } else if (effectiveBullish && !sentBearish) {
    recommendation  = 'BUY';
    confidence      = effectiveConf * 0.75;
    signal_strength = confidence >= 0.50 ? 'moderate' : 'weak';
  } else if (effectiveBearish && !sentBullish) {
    recommendation  = 'SELL';
    confidence      = effectiveConf * 0.75;
    signal_strength = confidence >= 0.50 ? 'moderate' : 'weak';
  } else {
    recommendation  = 'HOLD';
    // Use the ensemble's actual HOLD class probability when available.
    // Falls back to a conservative 0.25 when no model is loaded — the old
    // hardcoded 0.4 was arbitrary and detached from the model's real certainty.
    const holdProb  = pred?.probabilities?.hold ?? 0;
    confidence      = holdProb > 0 ? holdProb * 0.75 : 0.25;
    signal_strength = confidence >= 0.55 ? 'moderate' : 'weak';
  }

  // ── Per-source alignments ──
  const ensembleAlignment  = computeEnsembleAlignment(pred, recommendation);
  const patternAlignment   = computePatternAlignment(patternData, recommendation);
  const sentimentAlignment = computeSentimentAlignment(sentimentData, recommendation);

  const confirmed_count = [ensembleAlignment, patternAlignment, sentimentAlignment]
    .filter(a => a === 'confirms').length;

  // Upgrade signal strength when source count is unambiguous
  if (confirmed_count === 3)                                          signal_strength = 'strong';
  else if (confirmed_count === 2 && signal_strength === 'weak')      signal_strength = 'moderate';

  const synthesis_text = buildSynthesisText(
    recommendation,
    confirmed_count,
    ensembleAlignment,
    patternAlignment,
    sentimentAlignment,
  );

  return {
    recommendation,
    confidence:      Math.min(1, Math.max(0, confidence)),
    signal_strength,
    confirmed_count,
    synthesis_text,
    ensembleAlignment,
    patternAlignment,
    sentimentAlignment,
  };
}

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
