/**
 * Portfolio Insight & Advise — React Query hooks (F2)
 * ====================================================
 * TanStack Query v5 hooks for the read-only advisory layer, plus a thin live
 * selector over the existing P&L WebSocket. Mirrors the staleTime / retry / key
 * conventions in usePaperTrading.ts.
 *
 *  - usePortfolioInsight   → GET /portfolio-insight/stats (react-query, polled)
 *  - usePortfolioAdvice    → POST /portfolio-insight/advice (on-demand, cached)
 *  - useLivePositionPnL    → live per-position frame (incl. hit_probability) read
 *                            from the shared usePnLWebSocket ref — NO new socket.
 *
 * Both query hooks treat a 404 as "feature disabled" (backend INSIGHT_ENABLED
 * off): permanent, so retry is suppressed and callers can hide the section.
 */

import { useEffect, useState, type MutableRefObject } from 'react';
import { useQuery, type UseQueryOptions } from '@tanstack/react-query';
import { portfolioInsightAPI } from '@/lib/api';
import type { PortfolioAdvice, PortfolioInsightStats } from '@/types/portfolio_insight';
import type { LivePositionPnL } from '@/types/paper_trading';

// ──────────────────────────────────────────────────────────────────────────────
// Query Keys
// ──────────────────────────────────────────────────────────────────────────────

export const portfolioInsightKeys = {
  all: ['portfolio-insight'] as const,
  stats: () => [...portfolioInsightKeys.all, 'stats'] as const,
  advice: () => [...portfolioInsightKeys.all, 'advice'] as const,
};

/** True when an APIError carries HTTP 404 (feature disabled) — don't retry. */
function isFeatureDisabled(error: unknown): boolean {
  return (error as { statusCode?: number } | null)?.statusCode === 404;
}

// ──────────────────────────────────────────────────────────────────────────────
// Stats — polled read
// ──────────────────────────────────────────────────────────────────────────────

// /stats is recomputed per request (no server-side cache); its aggregates drift
// slowly as prices move, so a 60 s poll keeps them fresh without hammering the
// correlation query. The real-time per-position edge comes over the WS, not here.
const STATS_STALE_MS = 30_000;
const STATS_REFETCH_MS = 60_000;

export function usePortfolioInsight(
  enabled = true,
  options?: Partial<UseQueryOptions<PortfolioInsightStats>>,
) {
  return useQuery({
    queryKey: portfolioInsightKeys.stats(),
    queryFn: () => portfolioInsightAPI.getStats(),
    staleTime: STATS_STALE_MS,
    refetchInterval: STATS_REFETCH_MS,        // paused automatically while the tab is hidden
    enabled,
    retry: (failureCount, error) => {
      if (isFeatureDisabled(error)) return false;
      return failureCount < 2;
    },
    ...options,
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Advice — on-demand, client-cached
// ──────────────────────────────────────────────────────────────────────────────

// On-demand (lazy) query: it never fetches on mount, only when the user triggers
// `fetchAdvice()`. staleTime Infinity means the result is served from the query
// cache on subsequent mounts without a re-POST (the backend also caches by
// materiality hash, so even a forced refetch is usually one Gemini call at most).
const ADVICE_GC_MS = 30 * 60_000;

export interface UsePortfolioAdviceReturn {
  advice: PortfolioAdvice | null;
  /** True when the advice was served stale from cache during a quota/rate-limit degrade */
  isStale: boolean;
  isFetching: boolean;
  isError: boolean;
  error: unknown;
  /** Generate (or refresh) the advice on demand */
  fetchAdvice: () => void;
}

export function usePortfolioAdvice(): UsePortfolioAdviceReturn {
  const query = useQuery({
    queryKey: portfolioInsightKeys.advice(),
    queryFn: () => portfolioInsightAPI.getAdvice(),
    enabled: false,          // on-demand only — user clicks "Get advice"
    staleTime: Infinity,     // don't auto-refetch; the user drives regeneration
    gcTime: ADVICE_GC_MS,
    retry: (failureCount, error) => {
      if (isFeatureDisabled(error)) return false;
      return failureCount < 1;
    },
  });

  return {
    advice: query.data ?? null,
    isStale: query.data?.stale ?? false,
    isFetching: query.isFetching,
    isError: query.isError,
    error: query.error,
    fetchAdvice: () => {
      void query.refetch();
    },
  };
}

// ──────────────────────────────────────────────────────────────────────────────
// Live per-position selector (reads the shared P&L WS ref — no new socket)
// ──────────────────────────────────────────────────────────────────────────────

// Polls the shared positionPnLMap ref at 500 ms — the exact pattern
// OpenPositionsTable uses for its rows, so only the consuming component
// re-renders on each tick and OpenPositionsTable is untouched. Returns the live
// frame (carrying hit_probability / hit_prob_stale) or null until the first tick.
const LIVE_POLL_MS = 500;

export function useLivePositionPnL(
  positionId: string,
  positionPnLMap: MutableRefObject<Map<string, LivePositionPnL>>,
): LivePositionPnL | null {
  const [live, setLive] = useState<LivePositionPnL | null>(null);

  useEffect(() => {
    const id = window.setInterval(() => {
      const tick = positionPnLMap.current.get(positionId);
      if (tick) setLive(tick);
    }, LIVE_POLL_MS);
    return () => window.clearInterval(id);
  }, [positionId, positionPnLMap]);

  return live;
}
