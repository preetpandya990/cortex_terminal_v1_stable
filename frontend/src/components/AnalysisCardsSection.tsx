'use client';

/**
 * Analysis Cards Section
 * ======================
 * Four-card financial intelligence panel:
 *  1. ML Ensemble Analysis — XGBoost + GRU prediction with per-model breakdown
 *  2. AI Sentiment         — LLM news sentiment (RSS + NSE/BSE feeds)
 *  3. Prediction Summary   — synthesis of ensemble + sentiment → BUY/SELL/HOLD
 *  4. AI Explanation       — LLM plain-English narrative grounded in recent news
 *
 * Data flow: SSE stream is the primary source (real-time, 60s–5min refresh per
 * component; explanation pushed immediately on generation via Redis pub/sub).
 * React Query polling is the always-active fallback — activates immediately on
 * mount and keeps data fresh when SSE is down or retrying.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api';
import { cn } from '@/lib/utils';
import { useAuth } from '@/contexts/AuthContext';
import { MLPatternCard } from './MLPatternCard';
import { AISentimentCard } from './AISentimentCard';
import { PredictionSummaryCard } from './PredictionSummaryCard';
import { AIExplanationPanel } from './AIExplanationPanel';
import type {
  AnalysisStreamEvent,
  ExplanationData,
  MLEnsemblePrediction,
  PatternAnalysisCard,
  SentimentAnalysisCard,
} from '@/types/analysis';
import type { TradeSuggestion } from '@/types/trade_suggestions';

interface AnalysisCardsSectionProps {
  instrumentKey: string | null;
  symbol?: string | null;
  className?: string;
  /**
   * The trade suggestion currently displayed in the parent pane.
   * When provided, its explanation fields (from the REST response) are used
   * to pre-populate the panel immediately — before the SSE stream's first
   * poll cycle completes.  SSE still overrides once it delivers fresher data
   * (including structured source citations from the push path).
   */
  suggestion?: TradeSuggestion;
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
  const [predictionData,  setPredictionData]  = useState<MLEnsemblePrediction | null>(null);
  const [patternData,     setPatternData]     = useState<PatternAnalysisCard   | null>(null);
  const [sentimentData,   setSentimentData]   = useState<SentimentAnalysisCard | null>(null);
  const [explanationData, setExplanationData] = useState<ExplanationData       | null>(null);
  const [isConnected,     setIsConnected]     = useState(false);
  const [isInitialLoad,   setIsInitialLoad]   = useState(true);

  const esRef         = useRef<EventSource | null>(null);
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
        if (payload.prediction) setPredictionData(payload.prediction);
        if (payload.pattern)    setPatternData(payload.pattern);
        if (payload.sentiment)  setSentimentData(payload.sentiment);

        // ``explanation`` is explicitly nullable — null means no active suggestion,
        // which is distinct from "not yet received" (undefined).  Update whenever
        // the field is present in the payload, including on null.
        if ('explanation' in payload) setExplanationData(payload.explanation);

        setIsInitialLoad(false);
        setIsConnected(true);
        retryCountRef.current = 0;
      } catch {
        // Malformed payload — ignore silently; SSE will continue
      }
    });

    es.addEventListener('error', (_e: MessageEvent) => {
      // Non-fatal server-side error event — keep connection open
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
    setExplanationData(null);
    retryCountRef.current = 0;
    connect();

    return () => {
      esRef.current?.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [connect, enabled, instrumentKey]);

  return { predictionData, patternData, sentimentData, explanationData, isConnected, isInitialLoad };
}

// ── Main component ─────────────────────────────────────────────────────────────

export function AnalysisCardsSection({
  instrumentKey,
  symbol,
  className,
  suggestion,
}: AnalysisCardsSectionProps) {
  const { isAuthenticated, isAuthReady, accessToken } = useAuth();
  const canQuery = isAuthReady && isAuthenticated && !!instrumentKey;

  // ── SSE real-time stream ───────────────────────────────────────────────────
  const {
    predictionData:  ssePrediction,
    patternData:     ssePattern,
    sentimentData:   sseSentiment,
    explanationData: sseExplanation,
    isConnected:     sseConnected,
    isInitialLoad:   sseLoading,
  } = useAnalysisStream(instrumentKey, symbol, accessToken, canQuery);

  // ── Invalidate suggestions when explanation becomes ready ─────────────────
  // When the SSE push path delivers a completed explanation, the suggestion list
  // in HawkEyeRadarClient still holds a stale snapshot (llm_explanation = null).
  // Invalidating here causes React Query to refetch, which updates
  // currentDetailSuggestion → the next suggestionExplanation memo cycle returns
  // available: true → the panel transitions from skeleton to content.
  const queryClient = useQueryClient();
  useEffect(() => {
    if (sseExplanation?.available) {
      queryClient.invalidateQueries({ queryKey: ['trade-suggestions'] });
    }
  }, [sseExplanation?.available, queryClient]);

  // ── Explanation seed from REST response ────────────────────────────────────
  // The parent DetailPane already has the suggestion object from the REST list
  // call (which includes llm_summary / llm_explanation).  Use it to populate
  // the panel immediately — before the SSE stream's first poll cycle arrives.
  //
  // SSE takes priority once it delivers data: it carries structured source
  // citations (push path) and always reflects the latest DB state (poll path).
  //
  // Keyed on suggestion_id so the memo only recomputes when the suggestion
  // actually changes, not on every parent re-render.
  const suggestionExplanation = useMemo((): ExplanationData | null => {
    if (!suggestion) return null;

    // Explanation is fully ready — seed the panel with the REST data immediately.
    if (suggestion.llm_explanation) {
      return {
        available:           true,
        summary:             suggestion.llm_summary          ?? null,
        full_explanation:    suggestion.llm_explanation,
        model:               suggestion.explanation_model    ?? null,
        generated_at:        suggestion.explanation_generated_at ?? null,
        sources:             [],   // sources arrive via SSE push path, not REST
        context_type:        'suggestion_explanation',
        signal_direction:    (suggestion.signal_direction as 'BUY' | 'SELL') ?? null,
        signal_generated_at: suggestion.created_at ?? null,
      };
    }

    // Active suggestion exists but explanation not yet generated — show skeleton
    // so the panel is visible and waiting rather than hidden.
    if (suggestion.status === 'active') {
      return {
        available:           false,
        summary:             null,
        full_explanation:    null,
        model:               null,
        generated_at:        null,
        sources:             [],
        context_type:        'suggestion_explanation',
        signal_direction:    (suggestion.signal_direction as 'BUY' | 'SELL') ?? null,
        signal_generated_at: suggestion.created_at ?? null,
      };
    }

    return null;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [suggestion?.suggestion_id, suggestion?.llm_explanation]);

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

  // Explanation REST fallback — the fourth query.
  //
  // Fires immediately on mount (enabled: canQuery, not gated on !sseConnected) so
  // the React Query cache is populated before SSE might fail — the cached result
  // is then instantly available on modal reopen without a network request.
  //
  // staleTime matches the backend AIInstrumentContext TTL (2 h): explanation text
  // is stable within that window, so background refetches on window focus or
  // remount are suppressed while data is fresh.
  //
  // refetchInterval at 5 min (SSE-down only): a REST poll at 30 s would be
  // 120 requests/hour per tab per instrument. 5 min is aggressive enough to catch
  // a completed explanation (workers finish in 15–30 s) without excess load.
  const explanationQuery = useQuery({
    queryKey: ['ai-explanation', instrumentKey, symbol],
    queryFn: async () => {
      const res = await api.get('/ai/explanation', {
        params: {
          instrument_key: instrumentKey,
          ...(symbol ? { symbol } : {}),
        },
      });
      return res.data as ExplanationData;
    },
    enabled: canQuery,
    staleTime:                  2 * 60 * 60 * 1000,   // 2 hours — matches backend cache TTL
    refetchInterval:            sseConnected ? false : 5 * 60 * 1000,
    refetchIntervalInBackground: false,
  });

  // ── On-demand explanation bypass ───────────────────────────────────────────
  // A ref captures the latest explanationData so the callback is stable across
  // renders (deps=[accessToken] only), which keeps AIExplanationPanel's memo
  // effective when prediction/sentiment SSE ticks arrive but explanation hasn't changed.
  const explanationDataRef = useRef<ExplanationData | null>(null);

  const handleRequestExplanation = useCallback(async () => {
    const exp = explanationDataRef.current;
    if (!exp?.suggestion_id || !accessToken) return;
    await api.post(
      `/ai/explanation/${exp.suggestion_id}/request`,
      null,
      { params: { token: accessToken } },
    );
  }, [accessToken]);

  if (!instrumentKey) return null;

  // SSE data takes priority; React Query fills in until SSE fires
  const predictionData  = ssePrediction  ?? predictionQuery.data  ?? null;
  const patternData     = ssePattern     ?? patternQuery.data     ?? null;
  const sentimentData   = sseSentiment   ?? sentimentQuery.data   ?? null;
  // Explanation merge priority — five tiers, evaluated in order:
  //
  //   1. SSE with available:true  → authoritative; carries structured RAG source
  //      citations from the push path that are not stored in DB or returned by REST.
  //
  //   2. REST suggestion seed with available:true → use immediately; this covers
  //      explanations pre-generated before the SSE watcher subscribed (push missed).
  //
  //   3. REST fallback query with available:true → activates when SSE proxy is
  //      unavailable or degraded; populated within ~200 ms of component mount.
  //
  //   4. SSE streaming partial → available:false but full_explanation has token
  //      text flowing in; beat the static skeleton so the panel renders progressively.
  //
  //   5. Final fallback → any non-null value, including REST available:false skeleton,
  //      so the panel shows "Generating…" rather than being hidden entirely.
  //      REST fallback data is included so the generating state from the first REST
  //      call is surfaced while SSE is still connecting.
  //
  // Rationale for NOT using `sseExplanation ?? suggestionExplanation`: SSE fires a
  // transient { available: false } on its first poll before the DB commit lands, or
  // when a newer explanation-less suggestion exists for the same instrument_key. That
  // truthy-but-unavailable object wins ?? and permanently blocks the REST seed.
  const explanationData: ExplanationData | null = (() => {
    if (sseExplanation?.available)        return sseExplanation;
    if (suggestionExplanation?.available) return suggestionExplanation;
    if (explanationQuery.data?.available) return explanationQuery.data;
    // SSE streaming partial — render progressively while tokens flow in.
    if (sseExplanation && !sseExplanation.available && sseExplanation.full_explanation) {
      return sseExplanation;
    }
    return sseExplanation ?? explanationQuery.data ?? suggestionExplanation ?? null;
  })();

  // Keep the ref current so handleRequestExplanation always reads the latest
  // suggestion_id without the callback needing to re-create on every render.
  explanationDataRef.current = explanationData;

  const isPredictionLoading  = sseLoading && !predictionData  && predictionQuery.isLoading;
  const isPatternLoading     = sseLoading && !patternData     && patternQuery.isLoading;
  const isSentimentLoading   = sseLoading && !sentimentData   && sentimentQuery.isLoading;
  const isSummaryLoading     = isPredictionLoading && isPatternLoading && isSentimentLoading;
  // Show skeleton only during initial SSE load when there is no seeded data at all.
  // If suggestionExplanation is set, the panel renders from it immediately (no skeleton).
  const isExplanationLoading = sseLoading && explanationData === null;

  return (
    <div className={cn('space-y-4', className)}>
      {/* ── Three-card grid: ML Pattern | AI Sentiment | Prediction Summary ── */}
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

      {/* ── Full-width AI Explanation panel ────────────────────────────────── */}
      <AIExplanationPanel
        data={explanationData}
        isLoading={isExplanationLoading}
        onRequestExplanation={
          explanationData?.weak_signal && explanationData.suggestion_id
            ? handleRequestExplanation
            : undefined
        }
      />
    </div>
  );
}
