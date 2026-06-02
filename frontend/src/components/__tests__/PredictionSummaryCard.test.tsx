/**
 * PredictionSummaryCard Component Tests
 * ======================================
 * Integration tests for the PredictionSummaryCard component.
 * These tests verify correct rendering of the three distinct states
 * (loading, empty, and synthesised) and that key display values —
 * direction badge, confidence percentage, signal strength label,
 * and synthesis reasoning — are derived correctly from props.
 *
 * Note: the underlying synthesis logic is unit-tested exhaustively in
 * prediction-synthesis.test.ts.  These tests focus on the React layer:
 * does the component correctly translate synthesis output into visible UI?
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PredictionSummaryCard } from '@/components/PredictionSummaryCard';
import type {
  MLEnsemblePrediction,
  MLEnsemblePredictionData,
  PatternAnalysisCard,
  SentimentAnalysisCard,
} from '@/types/analysis';

// ── Fixtures ───────────────────────────────────────────────────────────────────

function makePrediction(
  direction: 'BUY' | 'SELL' | 'HOLD' = 'BUY',
  confidence = 0.68,
  convictionScale = 0.60,
): MLEnsemblePredictionData {
  return {
    available:          true,
    unavailable_reason: null,
    direction,
    confidence,
    conviction_scale:   convictionScale,
    threshold:          0.55,
    probabilities: {
      buy:  direction === 'BUY'  ? confidence : 0.15,
      sell: direction === 'SELL' ? confidence : 0.15,
      hold: direction === 'HOLD' ? confidence : 0.10,
    },
    entry_price: 2500,
    stop_loss:   2450,
    tp1: 2560, tp2: 2620, tp3: 2700,
    volatility: 0.18,
    models: {
      xgboost: { direction, confidence: 0.65, conviction_scale: 0.55, threshold: 0.55, probabilities: { buy: 0.65, sell: 0.20, hold: 0.15 }, weight: 0.51, version: '1.1.1' },
      gru:     { direction, confidence: 0.72, conviction_scale: 0.65, threshold: 0.55, probabilities: { buy: 0.72, sell: 0.15, hold: 0.13 }, weight: 0.49, version: '1.1.1' },
    },
    xgboost_version: '1.1.1',
    gru_version:     '1.1.1',
    xgboost_weight:  0.51,
    gru_weight:      0.49,
    timeframe:    '1D',
    predicted_at: '2026-06-01T06:00:00Z',
  };
}

function makePattern(direction: 'bullish' | 'bearish' = 'bullish'): PatternAnalysisCard {
  return {
    patterns: [{ name: 'HAMMER', timestamp: '2026-06-01T06:00:00Z', confidence: 100, direction }],
    total_detected: 1,
    timeframe: '1D',
    analyzed_candles: 60,
    best_pattern: { name: 'HAMMER', timestamp: '2026-06-01T06:00:00Z', confidence: 100, direction },
    cache_tier: 'redis',
    error: null,
    error_message: null,
    historical_accuracy: 0.72,
    historical_stats: { sample_size: 50, accuracy: 0.72, avg_move_pct: 1.8, tp1_hit_rate: 0.65, tp2_hit_rate: 0.45, tp3_hit_rate: 0.30, sl_hit_rate: 0.28 },
  };
}

function makeSentiment(impactScore: number): SentimentAnalysisCard {
  return {
    instrument_key: 'NSE_EQ|INE002A01018',
    symbol: 'RELIANCE',
    breakdown: { positive: 3, negative: 1, neutral: 1, total: 5, positive_pct: 60, negative_pct: 20, neutral_pct: 20 },
    impact_score: impactScore,
    sentiment_label: impactScore > 0 ? 'bullish' : 'bearish',
    top_event: null,
    lookback_hours: 24,
    cache_tier: 'redis',
    error: null,
    error_message: null,
  };
}

const NULL_PROPS = {
  patternData:   null as PatternAnalysisCard   | null,
  sentimentData: null as SentimentAnalysisCard | null,
  prediction:    null as MLEnsemblePrediction  | null,
};

// ── Loading state ──────────────────────────────────────────────────────────────

describe('PredictionSummaryCard — loading state', () => {
  it('renders a skeleton when isLoading is true', () => {
    const { container } = render(
      <PredictionSummaryCard {...NULL_PROPS} isLoading />,
    );
    // Skeleton uses animate-pulse divs instead of text content.
    const pulsingElements = container.querySelectorAll('.animate-pulse');
    expect(pulsingElements.length).toBeGreaterThan(0);
  });

  it('does not render the card title when loading', () => {
    render(<PredictionSummaryCard {...NULL_PROPS} isLoading />);
    expect(screen.queryByText('Prediction Summary')).not.toBeInTheDocument();
  });
});

// ── Empty state ────────────────────────────────────────────────────────────────

describe('PredictionSummaryCard — empty state (all inputs null)', () => {
  it('renders the card title', () => {
    render(<PredictionSummaryCard {...NULL_PROPS} isLoading={false} />);
    expect(screen.getByText('Prediction Summary')).toBeInTheDocument();
  });

  it('shows the "awaiting analysis" message', () => {
    render(<PredictionSummaryCard {...NULL_PROPS} isLoading={false} />);
    expect(screen.getByText(/Awaiting analysis data/i)).toBeInTheDocument();
  });

  it('does not render any BUY / SELL / HOLD badge', () => {
    render(<PredictionSummaryCard {...NULL_PROPS} isLoading={false} />);
    expect(screen.queryByText('BUY')).not.toBeInTheDocument();
    expect(screen.queryByText('SELL')).not.toBeInTheDocument();
    expect(screen.queryByText('HOLD')).not.toBeInTheDocument();
  });
});

// ── BUY recommendation ─────────────────────────────────────────────────────────

describe('PredictionSummaryCard — BUY recommendation', () => {
  const props = {
    prediction:    makePrediction('BUY', 0.68, 0.60) as MLEnsemblePrediction,
    patternData:   makePattern('bullish'),
    sentimentData: makeSentiment(30),
    isLoading:     false,
  };

  it('displays the BUY badge', () => {
    render(<PredictionSummaryCard {...props} />);
    expect(screen.getByText('BUY')).toBeInTheDocument();
  });

  it('shows a non-zero confidence percentage', () => {
    render(<PredictionSummaryCard {...props} />);
    // Confidence percentage is shown as "N%" where N is 1–100
    const confEl = screen.getByText(/^\d{1,3}%$/);
    const pct    = parseInt(confEl.textContent!);
    expect(pct).toBeGreaterThan(0);
    expect(pct).toBeLessThanOrEqual(100);
  });

  it('shows the signal strength label', () => {
    render(<PredictionSummaryCard {...props} />);
    // All three sources confirm → signal_strength = 'strong' → 'High Conviction'
    // The label appears in multiple places (card description + verdict body),
    // so we assert that at least one element contains it.
    const labels = screen.getAllByText(/conviction/i);
    expect(labels.length).toBeGreaterThan(0);
  });

  it('shows "3 of 3 sources confirm"', () => {
    render(<PredictionSummaryCard {...props} />);
    expect(screen.getByText(/3 of 3 sources confirm/i)).toBeInTheDocument();
  });

  it('renders the synthesis reasoning paragraph', () => {
    render(<PredictionSummaryCard {...props} />);
    // The synthesis text contains directional language for a triple-confirm BUY
    const text = screen.getByText(/All three sources/i);
    expect(text).toBeInTheDocument();
  });

  it('renders the Signal Breakdown section heading', () => {
    render(<PredictionSummaryCard {...props} />);
    expect(screen.getByText(/Signal Breakdown/i)).toBeInTheDocument();
  });

  it('renders panel labels for all three sources', () => {
    render(<PredictionSummaryCard {...props} />);
    expect(screen.getByText('ML Ensemble')).toBeInTheDocument();
    expect(screen.getByText('Pattern Signal')).toBeInTheDocument();
    expect(screen.getByText('AI Sentiment')).toBeInTheDocument();
  });

  it('includes the disclaimer text', () => {
    render(<PredictionSummaryCard {...props} />);
    expect(screen.getByText(/Not financial advice/i)).toBeInTheDocument();
  });
});

// ── SELL recommendation ────────────────────────────────────────────────────────

describe('PredictionSummaryCard — SELL recommendation', () => {
  it('displays the SELL badge', () => {
    render(
      <PredictionSummaryCard
        prediction={makePrediction('SELL', 0.72, 0.65) as MLEnsemblePrediction}
        patternData={makePattern('bearish')}
        sentimentData={makeSentiment(-30)}
        isLoading={false}
      />,
    );
    expect(screen.getByText('SELL')).toBeInTheDocument();
  });
});

// ── HOLD recommendation ────────────────────────────────────────────────────────

describe('PredictionSummaryCard — HOLD recommendation', () => {
  it('displays the HOLD badge when conviction_scale is 0', () => {
    render(
      <PredictionSummaryCard
        prediction={makePrediction('BUY', 0.58, 0) as MLEnsemblePrediction}
        patternData={null}
        sentimentData={null}
        isLoading={false}
      />,
    );
    expect(screen.getByText('HOLD')).toBeInTheDocument();
  });
});

// ── No model deployed ──────────────────────────────────────────────────────────

describe('PredictionSummaryCard — model unavailable', () => {
  it('shows "Model not deployed" in the ensemble panel', () => {
    render(
      <PredictionSummaryCard
        prediction={{ available: false, unavailable_reason: 'no_model' }}
        patternData={makePattern('bullish')}
        sentimentData={makeSentiment(10)}
        isLoading={false}
      />,
    );
    // Pattern data drives the recommendation; ensemble panel shows unavailable state.
    expect(screen.getByText(/Model not deployed/i)).toBeInTheDocument();
  });
});
