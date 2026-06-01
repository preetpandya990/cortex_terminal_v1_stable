'use client';

/**
 * Analysis Cards Section
 * ======================
 * Three-card financial intelligence panel:
 *  1. ML Ensemble Analysis — XGBoost + GRU prediction with per-model breakdown
 *  2. AI Sentiment         — FinBERT ONNX news sentiment (RSS + NSE/BSE feeds)
 *  3. Prediction Summary   — synthesis of ensemble + sentiment → BUY/SELL/HOLD
 *
 * Data flow: SSE stream is the primary source (real-time, 60s–5min refresh per
 * component).  React Query polling is the always-active fallback — it activates
 * immediately on mount and keeps data fresh when SSE is down or retrying.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { useAuth } from '@/contexts/AuthContext';
import { MLPatternCard } from './MLPatternCard';
import { AISentimentCard } from './AISentimentCard';
import { PredictionSummaryCard } from './PredictionSummaryCard';
import type {
  AnalysisStreamEvent,
  MLEnsemblePrediction,
  PatternAnalysisCard,
  SentimentAnalysisCard,
} from '@/types/analysis';

interface AnalysisCardsSectionProps {
  instrumentKey: string | null;
  symbol?: string | null;
  className?: string;
}

// ── SSE connection hook ────────────────────────────────────────────────────────

const SSE_MAX_RETRIES   = 3;
const SSE_RETRY_DELAY_MS = 5_000;

function useAnalysisStream(
  instrumentKey: string | null,
  symbol: string | null | undefined,
  accessToken: string | null,
  enabled: boolean,
) {
  const [predictionData, setPredictionData] = useState<MLEnsemblePrediction | null>(null);
  const [patternData,    setPatternData]    = useState<PatternAnalysisCard   | null>(null);
  const [sentimentData,  setSentimentData]  = useState<SentimentAnalysisCard | null>(null);
  const [isConnected,    setIsConnected]    = useState(false);
  const [isInitialLoad,  setIsInitialLoad]  = useState(true);

  const esRef           = useRef<EventSource | null>(null);
  const retryCountRef   = useRef(0);
  const retryTimerRef   = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (!enabled || !instrumentKey || !accessToken) return;

    esRef.current?.close();

    const url = new URL('/api/v1/ai/stream', window.location.origin);
    url.searchParams.set('instrument_key', instrumentKey);
    url.searchParams.set('token', accessToken);
    if (symbol) url.searchParams.set('symbol', symbol);

    const es = new EventSource(url.toString());
    esRef.current = es;

    es.addEventListener('analysis_update', (e: MessageEvent) => {
      try {
        const payload: AnalysisStreamEvent = JSON.parse(e.data);
        if (payload.prediction) setPredictionData(payload.prediction);
        if (payload.pattern)    setPatternData(payload.pattern);
        if (payload.sentiment)  setSentimentData(payload.sentiment);
        setIsInitialLoad(false);
        setIsConnected(true);
        retryCountRef.current = 0;
      } catch {
        // malformed payload — ignore silently
      }
    });

    es.addEventListener('error', (_e: MessageEvent) => {
      // Non-fatal server-side error — keep connection open
    });

    es.onerror = () => {
      setIsConnected(false);
      es.close();
      if (retryCountRef.current < SSE_MAX_RETRIES) {
        retryCountRef.current += 1;
        retryTimerRef.current = setTimeout(connect, SSE_RETRY_DELAY_MS);
      }
    };
  }, [enabled, instrumentKey, symbol, accessToken]);

  useEffect(() => {
    if (!enabled) return;
    setIsInitialLoad(true);
    setPredictionData(null);
    setPatternData(null);
    setSentimentData(null);
    retryCountRef.current = 0;
    connect();

    return () => {
      esRef.current?.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [connect, enabled, instrumentKey]);

  return { predictionData, patternData, sentimentData, isConnected, isInitialLoad };
}

// ── Main component ─────────────────────────────────────────────────────────────

export function AnalysisCardsSection({
  instrumentKey,
  symbol,
  className,
}: AnalysisCardsSectionProps) {
  const { isAuthenticated, isAuthReady, accessToken } = useAuth();
  const canQuery = isAuthReady && isAuthenticated && !!instrumentKey;

  // ── SSE real-time stream ───────────────────────────────────────────────────
  const {
    predictionData: ssePrediction,
    patternData:    ssePattern,
    sentimentData:  sseSentiment,
    isConnected:    sseConnected,
    isInitialLoad:  sseLoading,
  } = useAnalysisStream(instrumentKey, symbol, accessToken, canQuery);

  // ── Polling fallback (React Query) ─────────────────────────────────────────
  // All three queries run immediately on mount so the cards are never blank
  // longer than necessary.  refetchInterval is disabled when SSE is healthy.

  const predictionQuery = useQuery({
    queryKey: ['ml-ensemble-prediction', instrumentKey],
    queryFn: async () => {
      const res = await api.get('/ml/prediction-card', {
        params: { instrument_key: instrumentKey, timeframe: '1d' },
      });
      return res.data as MLEnsemblePrediction;
    },
    enabled: canQuery,
    staleTime:       60_000,
    refetchInterval: sseConnected ? false : 60_000,
  });

  const patternQuery = useQuery({
    queryKey: ['ml-pattern-strongest', instrumentKey],
    queryFn: async () => {
      const res = await api.get('/ml/pattern-analysis', {
        params: { instrument_key: instrumentKey, auto_detect: true },
      });
      return res.data as PatternAnalysisCard;
    },
    enabled: canQuery,
    staleTime:       300_000,
    refetchInterval: sseConnected ? false : 300_000,
  });

  const sentimentQuery = useQuery({
    queryKey: ['ai-sentiment', instrumentKey, symbol],
    queryFn: async () => {
      const res = await api.get('/ai/sentiment', {
        params: {
          instrument_key: instrumentKey,
          ...(symbol ? { symbol } : {}),
          lookback_hours: 24,
        },
      });
      return res.data as SentimentAnalysisCard;
    },
    enabled: canQuery,
    staleTime:       120_000,
    refetchInterval: sseConnected ? false : 120_000,
  });

  if (!instrumentKey) return null;

  // SSE data takes priority; React Query fills in until SSE fires
  const predictionData = ssePrediction ?? predictionQuery.data ?? null;
  const patternData    = ssePattern    ?? patternQuery.data    ?? null;
  const sentimentData  = sseSentiment  ?? sentimentQuery.data  ?? null;

  const isPredictionLoading = sseLoading && !predictionData && predictionQuery.isLoading;
  const isPatternLoading    = sseLoading && !patternData    && patternQuery.isLoading;
  const isSentimentLoading  = sseLoading && !sentimentData  && sentimentQuery.isLoading;
  const isSummaryLoading    = isPredictionLoading && isPatternLoading && isSentimentLoading;

  return (
    <div className={className}>
      <div className="grid gap-4 md:grid-cols-3">
        <MLPatternCard
          data={patternData}
          prediction={predictionData}
          isLoading={isPredictionLoading && isPatternLoading}
          error={
            !isPredictionLoading &&
            !isPatternLoading &&
            !predictionData &&
            !patternData &&
            !!(predictionQuery.error && patternQuery.error)
          }
        />

        <AISentimentCard
          data={sentimentData}
          isLoading={isSentimentLoading}
          error={!isSentimentLoading && !sentimentData && !!sentimentQuery.error}
        />

        <PredictionSummaryCard
          patternData={patternData}
          sentimentData={sentimentData}
          prediction={predictionData}
          isLoading={isSummaryLoading}
        />
      </div>
    </div>
  );
}
