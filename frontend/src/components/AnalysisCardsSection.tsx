'use client';

/**
 * Analysis Cards Section
 * ======================
 * World-class financial intelligence dashboard displaying three cards:
 *  1. ML Pattern Analysis  — auto-detected candlestick pattern (TA-Lib, 61 patterns)
 *  2. AI Sentiment         — FinBERT ONNX news sentiment from RSS + NSE/BSE feeds
 *  3. Prediction Summary   — client-side synthesis → BUY / SELL / HOLD
 *
 * Real-time updates via Server-Sent Events (SSE).
 * Falls back to parallel React Query polling if SSE is unavailable.
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
  PatternAnalysisCard,
  SentimentAnalysisCard,
} from '@/types/analysis';

interface AnalysisCardsSectionProps {
  instrumentKey: string | null;
  symbol?: string | null;  // Optional NSE ticker for news filtering (e.g. "RELIANCE")
  className?: string;
}

// ── SSE connection hook ────────────────────────────────────────────────────────

const SSE_MAX_RETRIES = 3;
const SSE_RETRY_DELAY_MS = 5_000;

function useAnalysisStream(
  instrumentKey: string | null,
  symbol: string | null | undefined,
  accessToken: string | null,
  enabled: boolean,
) {
  const [patternData, setPatternData] = useState<PatternAnalysisCard | null>(null);
  const [sentimentData, setSentimentData] = useState<SentimentAnalysisCard | null>(null);
  const [isConnected, setIsConnected] = useState(false);
  const [isInitialLoad, setIsInitialLoad] = useState(true);

  const esRef = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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
        if (payload.pattern)   setPatternData(payload.pattern);
        if (payload.sentiment) setSentimentData(payload.sentiment);
        setIsInitialLoad(false);
        setIsConnected(true);
        retryCountRef.current = 0;
      } catch {
        // malformed payload — ignore
      }
    });

    es.addEventListener('error', (e: MessageEvent) => {
      // Non-fatal error from server — keep connection open
      // (distinct from es.onerror which fires on connection loss)
    });

    es.onerror = () => {
      setIsConnected(false);
      es.close();

      if (retryCountRef.current < SSE_MAX_RETRIES) {
        retryCountRef.current += 1;
        retryTimerRef.current = setTimeout(connect, SSE_RETRY_DELAY_MS);
      }
      // After max retries, fall back to React Query polling (queries are always enabled)
    };
  }, [enabled, instrumentKey, symbol, accessToken]);

  // Connect / reconnect when key deps change
  useEffect(() => {
    if (!enabled) return;
    setIsInitialLoad(true);
    setPatternData(null);
    setSentimentData(null);
    retryCountRef.current = 0;
    connect();

    return () => {
      esRef.current?.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [connect, enabled, instrumentKey]);

  return { patternData, sentimentData, isConnected, isInitialLoad };
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
    patternData: ssePattern,
    sentimentData: sseSentiment,
    isConnected: sseConnected,
    isInitialLoad: sseLoading,
  } = useAnalysisStream(instrumentKey, symbol, accessToken, canQuery);

  // ── Polling fallback (React Query) ─────────────────────────────────────────
  // Always active — provides data when SSE hasn't fired yet or after max retries.
  const patternQuery = useQuery({
    queryKey: ['ml-pattern-strongest', instrumentKey],
    queryFn: async () => {
      const res = await api.get('/ml/pattern-analysis', {
        params: { instrument_key: instrumentKey, auto_detect: true },
      });
      return res.data as PatternAnalysisCard;
    },
    enabled: canQuery,
    staleTime: 300_000,   // 5 minutes
    refetchInterval: sseConnected ? false : 300_000,  // poll only when SSE is down
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
    staleTime: 120_000,   // 2 minutes
    refetchInterval: sseConnected ? false : 120_000,
  });

  if (!instrumentKey) return null;

  // SSE data takes priority; fall back to React Query data
  const patternData = ssePattern ?? patternQuery.data ?? null;
  const sentimentData = sseSentiment ?? sentimentQuery.data ?? null;

  const isPatternLoading = sseLoading && !patternData && patternQuery.isLoading;
  const isSentimentLoading = sseLoading && !sentimentData && sentimentQuery.isLoading;
  const isSummaryLoading = isPatternLoading && isSentimentLoading;

  return (
    <div className={className}>
      <div className="grid gap-4 md:grid-cols-3">
        <MLPatternCard
          data={patternData}
          isLoading={isPatternLoading}
          error={!isPatternLoading && !patternData && !!patternQuery.error}
        />

        <AISentimentCard
          data={sentimentData}
          isLoading={isSentimentLoading}
          error={!isSentimentLoading && !sentimentData && !!sentimentQuery.error}
        />

        <PredictionSummaryCard
          patternData={patternData}
          sentimentData={sentimentData}
          isLoading={isSummaryLoading}
        />
      </div>
    </div>
  );
}
