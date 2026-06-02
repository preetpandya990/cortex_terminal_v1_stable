/**
 * Prediction Synthesis
 * ====================
 * Pure functions that combine the ML ensemble prediction, candlestick pattern
 * signal, and news sentiment into a single trading recommendation.
 *
 * All exports are free of React / DOM dependencies so they can be unit-tested
 * in isolation without rendering a component.
 *
 * Used by: PredictionSummaryCard
 */

import type {
  MLEnsemblePrediction,
  MLEnsemblePredictionData,
  PatternAnalysisCard,
  PredictionSynthesis,
  SentimentAnalysisCard,
} from '@/types/analysis';

// ── Domain types ───────────────────────────────────────────────────────────────

export type Direction = 'BUY' | 'SELL' | 'HOLD';

/**
 * Per-source signal alignment with respect to the overall recommendation.
 *
 *   confirms  — source direction matches the final recommendation
 *   conflicts — source direction is opposite
 *   neutral   — source present but below its actionable threshold
 *   absent    — source data is unavailable
 */
export type Alignment = 'confirms' | 'conflicts' | 'neutral' | 'absent';

/** Text content for a Signal Breakdown panel row. */
export interface PanelContent {
  label:    string;
  headline: string;
  detail:   string;
}

/** synthesize() return — PredictionSynthesis extended with per-source alignments */
export interface SynthesisResult extends PredictionSynthesis {
  ensembleAlignment:  Alignment;
  patternAlignment:   Alignment;
  sentimentAlignment: Alignment;
}

// ── Alignment helpers ──────────────────────────────────────────────────────────

/**
 * Determine how the ML ensemble's output aligns with the overall verdict.
 * Returns 'neutral' when conviction_scale is 0 (model is at its gate threshold).
 */
export function computeEnsembleAlignment(
  pred:    MLEnsemblePredictionData | null,
  verdict: Direction,
): Alignment {
  if (!pred) return 'absent';
  if (pred.direction === 'HOLD' || pred.conviction_scale === 0) return 'neutral';
  return pred.direction === verdict ? 'confirms' : 'conflicts';
}

/**
 * Determine how the best detected candlestick pattern aligns with the verdict.
 * Only checks direction — pattern confidence is handled upstream by the scanner.
 */
export function computePatternAlignment(
  patternData: PatternAnalysisCard | null,
  verdict:     Direction,
): Alignment {
  const p = patternData?.best_pattern;
  if (!p) return 'absent';
  return (p.direction === 'bullish' ? 'BUY' : 'SELL') === verdict ? 'confirms' : 'conflicts';
}

/**
 * Determine how the news sentiment impact score aligns with the verdict.
 * impact_score < ±15 is considered sub-threshold (neutral), not a signal.
 */
export function computeSentimentAlignment(
  sentimentData: SentimentAnalysisCard | null,
  verdict:       Direction,
): Alignment {
  if (!sentimentData || sentimentData.breakdown.total === 0) return 'absent';
  const score = sentimentData.impact_score;
  if (Math.abs(score) < 15) return 'neutral';
  return (score > 0 ? 'BUY' : 'SELL') === verdict ? 'confirms' : 'conflicts';
}

// ── Synthesis text ─────────────────────────────────────────────────────────────

/** @internal */
export function firstOfAlignment(
  alignments: Alignment[],
  targets:    Alignment[],
  names:      string[],
): string {
  const idx = alignments.findIndex(a => targets.includes(a));
  return idx >= 0 ? names[idx] : names[0];
}

/**
 * Generate a plain-English reasoning paragraph for the prediction card.
 * Handles all nine meaningful combinations of confirmed / conflicted / neutral / absent
 * sources so that every verdict state shows a non-generic description.
 */
export function buildSynthesisText(
  recommendation:    Direction,
  confirmedCount:    number,
  ea:                Alignment,  // ensemble
  pa:                Alignment,  // pattern
  sa:                Alignment,  // sentiment
): string {
  const dir        = recommendation === 'BUY' ? 'bullish' : recommendation === 'SELL' ? 'bearish' : 'neutral';
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
    const neutralState = [ea, pa, sa].find(a => a === 'neutral' || a === 'absent') === 'absent'
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

// ── Panel content builders ─────────────────────────────────────────────────────

/** Build the text content for the ML Ensemble panel row. */
export function buildEnsemblePanelContent(
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

  const confPct   = Math.round(pred.confidence       * 100);
  const convPct   = Math.round(pred.conviction_scale * 100);
  const threshPct = Math.round(pred.threshold        * 100);
  const isHeld    = pred.direction === 'HOLD' || pred.conviction_scale === 0;

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

/** Build the text content for the Pattern Signal panel row. */
export function buildPatternPanelContent(patternData: PatternAnalysisCard | null): PanelContent {
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

/** Build the text content for the AI Sentiment panel row. */
export function buildSentimentPanelContent(
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

  const score    = sentimentData.impact_score;
  const sign     = score > 0 ? '+' : '';
  const total    = sentimentData.breakdown.total;
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

// ── Core synthesis ─────────────────────────────────────────────────────────────

/**
 * Synthesise a final trading recommendation from the three available signals.
 *
 * Returns null when ALL inputs are null (no data to synthesise from).
 *
 * Signal weighting:
 *   - ML prediction carries 70–75 % of the effective confidence.
 *   - Sentiment carries 30 % when it agrees with ML, 0 % otherwise.
 *   - Patterns are used as primary driver only when ML is unavailable.
 *
 * Confidence formula (for BUY / SELL with ML + agreeing sentiment):
 *   confidence = effectiveConf × 0.70 + sentConf × 0.30
 *
 * Without agreeing sentiment:
 *   confidence = effectiveConf × 0.75
 */
export function synthesize(
  prediction:    MLEnsemblePrediction  | null,
  patternData:   PatternAnalysisCard   | null,
  sentimentData: SentimentAnalysisCard | null,
): SynthesisResult | null {
  const hasPred = prediction?.available === true;
  const pred    = hasPred ? (prediction as MLEnsemblePredictionData) : null;

  if (!hasPred && !patternData && !sentimentData) return null;

  // ── Primary ML signal ──────────────────────────────────────────────────────
  const mlBullish = pred?.direction === 'BUY'  && (pred?.conviction_scale ?? 0) > 0;
  const mlBearish = pred?.direction === 'SELL' && (pred?.conviction_scale ?? 0) > 0;
  const mlConf    = pred?.confidence ?? 0;

  // ── Pattern fallback (only when ML is unavailable) ─────────────────────────
  const patternBullish = !hasPred && patternData?.best_pattern?.direction === 'bullish';
  const patternBearish = !hasPred && patternData?.best_pattern?.direction === 'bearish';
  const patternConf    = patternData?.best_pattern
    ? Math.min(patternData.best_pattern.confidence, 100) / 100
    : 0;

  const effectiveBullish = mlBullish || patternBullish;
  const effectiveBearish = mlBearish || patternBearish;
  const effectiveConf    = hasPred ? mlConf : patternConf * 0.65;

  // ── Sentiment signal ───────────────────────────────────────────────────────
  const sentScore   = sentimentData?.impact_score ?? 0;
  const sentBullish = sentScore >=  15;
  const sentBearish = sentScore <= -15;
  const sentConf    = Math.min(Math.abs(sentScore) / 100, 0.60);

  // ── Final recommendation and confidence ───────────────────────────────────
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
    const holdProb  = pred?.probabilities?.hold ?? 0;
    confidence      = holdProb > 0 ? holdProb * 0.75 : 0.25;
    signal_strength = confidence >= 0.55 ? 'moderate' : 'weak';
  }

  // ── Per-source alignments ──────────────────────────────────────────────────
  const ensembleAlignment  = computeEnsembleAlignment(pred, recommendation);
  const patternAlignment   = computePatternAlignment(patternData, recommendation);
  const sentimentAlignment = computeSentimentAlignment(sentimentData, recommendation);

  const confirmed_count = [ensembleAlignment, patternAlignment, sentimentAlignment]
    .filter(a => a === 'confirms').length;

  // Upgrade signal strength when source unanimity is clear
  if (confirmed_count === 3)                                     signal_strength = 'strong';
  else if (confirmed_count === 2 && signal_strength === 'weak')  signal_strength = 'moderate';

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
