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

// Exponential reconnect backoff: 5s → 10s → 20s → 40s → 60s cap, forever.
// The old design gave up permanently after 3 attempts (~15s) — one flaky
// minute killed real-time delivery for the rest of the session (WS8 fix).
const SSE_RETRY_BASE_MS = 5_000;
const SSE_RETRY_MAX_MS  = 60_000;

// A healthy stream emits an analysis_update at least every 60s (the
// prediction refresher's cadence, and every emit carries all four slices).
// No event for 3× that window ⇒ the stream is silently stalled and the
// per-component polling fallback must resume.
const SSE_EVENT_SILENCE_HORIZON_MS = 180_000;

// Re-evaluate liveness on a timer: when the stream goes quiet there are no
// events to trigger a render, so staleness would otherwise go unnoticed.
const SSE_FRESHNESS_TICK_MS = 30_000;

type StreamComponent = 'prediction' | 'pattern' | 'sentiment' | 'explanation';
const STREAM_COMPONENTS: readonly StreamComponent[] = [
  'prediction', 'pattern', 'sentiment', 'explanation',
];

// Exported for direct hook-level testing (connection lifecycle, backoff and
// per-component liveness are regression-prone — see the WS8 test suite).
export function useAnalysisStream(
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
  // Per-component degradation (WS8): the backend emits analysis_error when a
  // component's refresher fails while re-broadcasting its last good data —
  // the shared boolean used to hide exactly this. Value = when it degraded,
  // so a genuinely fresher slice (updated_at after that moment) clears it.
  const [degradedAt, setDegradedAt] = useState<Partial<Record<StreamComponent, number>>>({});
  const [lastEventAt, setLastEventAt] = useState(0);
  // Timer-driven re-render so liveness decays even when the stream is quiet.
  const [, setFreshnessTick] = useState(0);

  const esRef         = useRef<EventSource | null>(null);
  const retryCountRef = useRef(0);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Token ref (WS8, mirrors useWebSocket): kept current on every render so a
  // reconnect always presents a FRESH token, while the connection lifecycle
  // never depends on the token value — the silent ~29-min refresh used to
  // recreate `connect`, wiping all four cards and tearing down the stream.
  const tokenRef = useRef(accessToken);
  tokenRef.current = accessToken;

  const connect = useCallback(() => {
    if (!enabled || !instrumentKey || !tokenRef.current) return;

    esRef.current?.close();

    const url = new URL('/api/v1/ai/stream', window.location.origin);
    url.searchParams.set('instrument_key', instrumentKey);
    url.searchParams.set('token', tokenRef.current);
    if (symbol) url.searchParams.set('symbol', symbol);

    const es = new EventSource(url.toString());
    esRef.current = es;

    es.onopen = () => {
      // Connected is flipped on OPEN, not first data (WS8): a connected-but-
      // quiet stream previously left polling running in parallel (double-fetch).
      setIsConnected(true);
      setLastEventAt(Date.now());
      retryCountRef.current = 0;
    };

    es.addEventListener('analysis_update', (e: MessageEvent) => {
      try {
        const payload: AnalysisStreamEvent = JSON.parse(e.data);
        const receivedAt = Date.now();
        if (payload.prediction) setPredictionData(payload.prediction);
        if (payload.pattern)    setPatternData(payload.pattern);
        if (payload.sentiment)  setSentimentData(payload.sentiment);

        // ``explanation`` is explicitly nullable — null means no active suggestion,
        // which is distinct from "not yet received" (undefined).  Update whenever
        // the field is present in the payload, including on null.
        if ('explanation' in payload) setExplanationData(payload.explanation);

        // Clear degradation for components whose slice genuinely refreshed
        // (updated_at newer than the degradation moment). Slices without an
        // updated_at (pre-WS8 backend) clear on receipt.
        setDegradedAt((prev) => {
          const entries = Object.entries(prev) as [StreamComponent, number][];
          if (entries.length === 0) return prev;
          const next = { ...prev };
          let changed = false;
          for (const [component, markedAt] of entries) {
            const slice = payload[component] as { updated_at?: string } | null;
            if (!slice) continue;
            const sliceAt = slice.updated_at ? Date.parse(slice.updated_at) : receivedAt;
            if (sliceAt >= markedAt) {
              delete next[component];
              changed = true;
            }
          }
          return changed ? next : prev;
        });

        setLastEventAt(receivedAt);
        setIsInitialLoad(false);
        setIsConnected(true);
        retryCountRef.current = 0;
      } catch {
        // Malformed payload — ignore silently; SSE will continue
      }
    });

    es.addEventListener('analysis_error', (e: MessageEvent) => {
      // Per-component degradation (WS8 — the old generic listener no-op'd):
      // the backend keeps re-broadcasting the last good data; marking the
      // component degraded re-arms ITS polling fallback and shows the
      // "live data delayed" strip, without touching the healthy cards.
      try {
        const { component } = JSON.parse(e.data) as { component?: string };
        if (component && (STREAM_COMPONENTS as readonly string[]).includes(component)) {
          setDegradedAt((prev) => ({ ...prev, [component]: Date.now() }));
        }
      } catch {
        // Malformed error event — ignore
      }
    });

    es.onerror = () => {
      setIsConnected(false);
      es.close();
      // Never give up: exponential backoff, reset on the next successful open.
      const delay = Math.min(
        SSE_RETRY_BASE_MS * 2 ** retryCountRef.current,
        SSE_RETRY_MAX_MS,
      );
      retryCountRef.current += 1;
      retryTimerRef.current = setTimeout(connect, delay);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps -- accessToken is
  // deliberately read via tokenRef so token rotation never recreates connect.
  }, [enabled, instrumentKey, symbol]);

  useEffect(() => {
    if (!enabled) return;
    const id = setInterval(() => setFreshnessTick((t) => t + 1), SSE_FRESHNESS_TICK_MS);
    return () => clearInterval(id);
  }, [enabled]);

  // Connection lifecycle — keyed on the INSTRUMENT, never the token (WS8):
  // this reset-and-reconnect must run only when the user views something new.
  useEffect(() => {
    if (!enabled) return;
    setIsInitialLoad(true);
    setPredictionData(null);
    setPatternData(null);
    setSentimentData(null);
    setExplanationData(null);
    setDegradedAt({});
    setLastEventAt(0);
    retryCountRef.current = 0;
    connect();

    return () => {
      esRef.current?.close();
      if (retryTimerRef.current) clearTimeout(retryTimerRef.current);
    };
  }, [connect, enabled, instrumentKey]);

  // Per-component liveness: the stream is delivering for THIS component.
  // False → its React Query refetchInterval re-arms (see query definitions).
  const isComponentLive = useCallback(
    (component: StreamComponent): boolean =>
      isConnected &&
      degradedAt[component] === undefined &&
      Date.now() - lastEventAt < SSE_EVENT_SILENCE_HORIZON_MS,
    [isConnected, degradedAt, lastEventAt],
  );

  const degradedComponents = useMemo(
    () => STREAM_COMPONENTS.filter((c) => degradedAt[c] !== undefined),
    [degradedAt],
  );

  return {
    predictionData, patternData, sentimentData, explanationData,
    isConnected, isInitialLoad, isComponentLive, degradedComponents,
  };
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
    isInitialLoad:   sseLoading,
    isComponentLive,
    degradedComponents,
  } = useAnalysisStream(instrumentKey, symbol, accessToken, canQuery);

  // ── Invalidate suggestions when explanation becomes ready ─────────────────
  // When the SSE push path delivers a completed explanation, the suggestion list
  // in HawkEyeRadarClient still holds a stale snapshot (llm_explanation = null).
  // Invalidating here causes React Query to refetch, which updates
  // currentDetailSuggestion → the next suggestionExplanation memo cycle returns
  // available: true → the panel transitions from skeleton to content.
  //
  // Keyed on the explanation's identity (generated_at) rather than the
  // `available` boolean so it fires ONCE per newly delivered explanation —
  // not on every reconnect that re-delivers the same one (WS8 churn fix).
  const queryClient = useQueryClient();
  const readyExplanationKey = sseExplanation?.available
    ? sseExplanation.generated_at ?? 'ready'
    : null;
  useEffect(() => {
    if (readyExplanationKey !== null) {
      queryClient.invalidateQueries({ queryKey: ['trade-suggestions'] });
    }
  }, [readyExplanationKey, queryClient]);

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
    refetchInterval: isComponentLive('prediction') ? false : 60_000,
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
    refetchInterval: isComponentLive('pattern') ? false : 300_000,
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
    refetchInterval: isComponentLive('sentiment') ? false : 120_000,
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
    refetchInterval:            isComponentLive('explanation') ? false : 5 * 60 * 1000,
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
      {/* ── Live-data degradation strip (WS8) — inline, no toasts. Shown when
          the backend reported a component's refresher failing; its last good
          data stays on screen and polling has re-armed for it. ── */}
      {degradedComponents.length > 0 && (
        <div
          className="flex items-center gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-1.5 text-xs text-amber-700"
          role="status"
          aria-live="polite"
        >
          <span
            className="h-1.5 w-1.5 shrink-0 animate-pulse rounded-full bg-amber-500"
            aria-hidden
          />
          Live data delayed for {degradedComponents.join(', ')} — showing last
          received data.
        </div>
      )}

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
          (explanationData?.weak_signal || explanationData?.failed) &&
          explanationData.suggestion_id
            ? handleRequestExplanation
            : undefined
        }
      />
    </div>
  );
}
