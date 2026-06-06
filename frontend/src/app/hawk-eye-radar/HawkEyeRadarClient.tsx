"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams, useRouter } from "next/navigation";
import { Radar, Loader2, AlertCircle, Star } from "lucide-react";
import { tradeSuggestionsAPI, isNetworkError, type CorrelationActivityItem } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InstrumentSearchCombobox } from "@/components/market/InstrumentSearchCombobox";
import { TradeSuggestionCard } from "./components/TradeSuggestionCard";
import { WatchlistCard } from "./components/WatchlistCard";
import { DetailPane } from "./components/DetailPane";
import { SuggestionFilters } from "./components/SuggestionFilters";
import { SuggestionStats } from "./components/SuggestionStats";
import { MLActivityCard } from "./components/MLActivityCard";
import { useWebSocket, type WebSocketMessage } from "@/hooks/useWebSocket";
import { useMLActivity } from "@/hooks/useMLActivity";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useDragReorder } from "@/hooks/useDragReorder";
import { useAuth } from "@/contexts/AuthContext";
import { usePositions } from "@/hooks/usePaperTrading";
import type { UpstoxInstrument } from "@/types/upstox";
import type { TradeSuggestion, SuggestionFilters as Filters } from "@/types/trade_suggestions";
import type { PositionSide } from "@/types/paper_trading";

export default function HawkEyeRadarClient() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { isAuthenticated, isAuthReady, accessToken, isLoading: authLoading } = useAuth();
  const [selectedInstrument, setSelectedInstrument] = useState<UpstoxInstrument | null>(null);
  const [detailSuggestion, setDetailSuggestion] = useState<TradeSuggestion | null>(null);
  const [filters, setFilters] = useState<Filters>({ status: "active", page: 1, page_size: 50 });
  const queryClient = useQueryClient();

  // Seed ML Activity feed with recent history from the DB on first load.
  const { data: activitySeed } = useQuery<CorrelationActivityItem[]>({
    queryKey: ["ml-activity-seed"],
    queryFn: async () => {
      const res = await tradeSuggestionsAPI.getRecentActivity(50);
      return res.items;
    },
    enabled: isAuthenticated && !authLoading,
    staleTime: Infinity, // Seed data is one-shot — WS handles subsequent updates.
    gcTime: Infinity,
  });

  // ML Activity feed — seeded from DB history, then driven by WebSocket messages.
  const { items: activityItems, handleMessage: handleActivityMessage } = useMLActivity({
    seedItems: activitySeed,
  });

  // Watchlist hook
  const {
    items: watchlistItems,
    removeFromWatchlist,
    reorderWatchlist,
    isLoading: watchlistLoading,
  } = useWatchlist();

  // Open positions — fetched once here and passed per-card for O(1) lookup.
  // React Query deduplicates with DetailPane's identical call, so no extra requests.
  const { data: openPositionsData } = usePositions({ status: "OPEN" }, isAuthenticated);
  const openPositionsBySide = useMemo(() => {
    const map = new Map<string, PositionSide>();
    openPositionsData?.positions.forEach((p) => map.set(p.instrument_key, p.side));
    return map;
  }, [openPositionsData?.positions]);

  const { draggingId, overId, clickPreventedRef, getGripHandlers } = useDragReorder(
    useCallback(
      async (draggedId: number, targetId: number) => {
        const targetItem = watchlistItems.find((i) => i.id === targetId);
        if (!targetItem) return;
        try {
          await reorderWatchlist({ itemId: draggedId, newPosition: targetItem.position });
        } catch (error) {
          if (!isNetworkError(error)) console.error('[Hawk-Eye] Failed to reorder watchlist:', error);
        }
      },
      [watchlistItems, reorderWatchlist],
    ),
  );

  // WebSocket connection for real-time updates.
  // Only opens once auth is confirmed; token is sent in-band on connect.
  const { isConnected } = useWebSocket({
    url: process.env.NEXT_PUBLIC_WS_URL || 'ws://localhost:8000/api/v1/trade-suggestions/ws',
    token: accessToken ?? undefined,
    enabled: isAuthReady && isAuthenticated,
    onMessage: (data: WebSocketMessage) => {
      // Route every message through the ML Activity state machine first so the
      // live feed updates before the suggestions grid re-fetches.
      handleActivityMessage(data);

      if (data.type === 'new_suggestion') {
        queryClient.invalidateQueries({ queryKey: ["trade-suggestions"] });
      }
    },
    onConnect: () => {
      console.log('[Hawk-Eye] WebSocket connected');
    },
    onDisconnect: () => {
      console.log('[Hawk-Eye] WebSocket disconnected');
    },
    reconnect: true,
    reconnectAttempts: 10,
  });

  // Fetch active trade suggestions (only when authenticated)
  const {
    data: suggestionsData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["trade-suggestions", filters],
    queryFn: () => tradeSuggestionsAPI.getSuggestions(filters),
    enabled: isAuthenticated && !authLoading,
    refetchInterval: isConnected ? false : 30000,
    retry: (failureCount, error: any) => {
      if (error?.response?.status === 401) return false;
      return failureCount < 3;
    },
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
    staleTime: 30000,
  });

  // Read instrument_key from URL on mount
  useEffect(() => {
    const instrumentKey = searchParams.get('instrument_key');
    if (instrumentKey && !selectedInstrument && suggestionsData) {
      const suggestion = suggestionsData.suggestions.find(
        s => s.instrument_key === instrumentKey
      );
      if (suggestion) {
        setDetailSuggestion(suggestion);
      }
    }
  }, [searchParams, suggestionsData, selectedInstrument]);

  // Read suggestion_id from URL on mount (deep linking support)
  useEffect(() => {
    const suggestionId = searchParams.get('suggestion_id');
    if (suggestionId && !detailSuggestion && suggestionsData) {
      const suggestion = suggestionsData.suggestions.find(
        s => s.suggestion_id === suggestionId
      );
      if (suggestion) {
        setDetailSuggestion(suggestion);
        const params = new URLSearchParams(searchParams.toString());
        params.delete('suggestion_id');
        params.set('instrument_key', suggestion.instrument_key);
        router.replace(`?${params.toString()}`, { scroll: false });
      }
    }
  }, [searchParams, suggestionsData, detailSuggestion, router]);

  const handleViewDetails = (suggestionId: string) => {
    const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
    if (suggestion) {
      setDetailSuggestion(suggestion);
      const params = new URLSearchParams(searchParams.toString());
      params.set('instrument_key', suggestion.instrument_key);
      router.push(`?${params.toString()}`, { scroll: false });
    }
  };

  const handleManualSelect = (instrument: UpstoxInstrument) => {
    setSelectedInstrument(instrument);
    const params = new URLSearchParams(searchParams.toString());
    params.set('instrument_key', instrument.instrument_key);
    router.push(`?${params.toString()}`, { scroll: false });
  };

  const handleCloseDetail = () => {
    setDetailSuggestion(null);
    setSelectedInstrument(null);
    const params = new URLSearchParams(searchParams.toString());
    params.delete('instrument_key');
    const newUrl = params.toString() ? `?${params.toString()}` : '/hawk-eye-radar';
    router.push(newUrl, { scroll: false });
  };

  const handleWatchlistItemClick = useCallback((instrumentKey: string) => {
    if (clickPreventedRef.current) return;

    const item = watchlistItems.find(w => w.instrument_key === instrumentKey);
    if (item) {
      const instrument: UpstoxInstrument = {
        instrument_key: item.instrument_key,
        trading_symbol: item.trading_symbol,
        name: item.name || "",
        exchange: item.exchange || "NSE",
      };
      setSelectedInstrument(instrument);
      const params = new URLSearchParams(searchParams.toString());
      params.set('instrument_key', instrument.instrument_key);
      router.push(`?${params.toString()}`, { scroll: false });
    }
  }, [clickPreventedRef, watchlistItems, searchParams, router]);

  const handleRemoveFromWatchlist = async (itemId: number) => {
    try {
      await removeFromWatchlist(itemId);
    } catch (error) {
      if (!isNetworkError(error)) console.error('[Hawk-Eye] Failed to remove from watchlist:', error);
    }
  };

  // Always use the freshest version of detailSuggestion from the live query result.
  // detailSuggestion is set at click time and never mutated; when React Query
  // refetches (e.g. after llm_explanation is generated), the updated fields
  // (llm_summary, llm_explanation) are in suggestionsData but NOT in the stored
  // state.  This lookup ensures the DetailPane always receives the latest snapshot.
  const currentDetailSuggestion = useMemo(() => {
    if (!detailSuggestion) return null;
    return (
      suggestionsData?.suggestions.find(
        (s) => s.suggestion_id === detailSuggestion.suggestion_id,
      ) ?? detailSuggestion  // fall back to stored object if not in current list
    );
  }, [detailSuggestion?.suggestion_id, suggestionsData?.suggestions]);

  const detailInstrument = selectedInstrument ?? (currentDetailSuggestion
    ? ({
        instrument_key: currentDetailSuggestion.instrument_key,
        trading_symbol: currentDetailSuggestion.trading_symbol ?? currentDetailSuggestion.symbol,
        name: currentDetailSuggestion.company_name
          ? currentDetailSuggestion.company_name
              .toLowerCase()
              .split(" ")
              .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
              .join(" ")
          : (currentDetailSuggestion.trading_symbol ?? currentDetailSuggestion.symbol),
        exchange: currentDetailSuggestion.instrument_key.split("_")[0] ?? "NSE",
      } as UpstoxInstrument)
    : null);

  if (!isAuthReady || !isAuthenticated) return null;

  return (
    <div className="space-y-8">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900">Hawk-Eye Radar</h1>
      </div>

      {/* Watchlist Section */}
      {isAuthenticated && (
        <div>
          <div className="mb-4 flex items-center gap-3">
            <Star className="h-5 w-5 text-yellow-500 fill-yellow-500" />
            <div>
              <h2 className="text-lg font-semibold text-slate-900">My Watchlist</h2>
              <p className="text-sm text-slate-500">
                Track your favorite stocks with live prices
              </p>
            </div>
          </div>

          {watchlistLoading && (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
              {[...Array(3)].map((_, i) => (
                <Card key={i} className="border-slate-200 bg-white animate-pulse">
                  <CardHeader className="pb-3">
                    <div className="h-6 w-32 bg-slate-200 rounded" />
                    <div className="h-4 w-48 bg-slate-100 rounded mt-2" />
                  </CardHeader>
                  <CardContent className="space-y-3">
                    <div className="h-8 w-24 bg-slate-200 rounded" />
                    <div className="h-4 w-full bg-slate-100 rounded" />
                  </CardContent>
                </Card>
              ))}
            </div>
          )}

          {!watchlistLoading && watchlistItems.length === 0 && (
            <Card className="border-slate-200 bg-slate-50">
              <CardContent className="flex flex-col items-center justify-center py-12 text-center">
                <Star className="h-12 w-12 text-slate-400 mb-4" />
                <h3 className="text-lg font-semibold text-slate-900 mb-2">No Stocks in Watchlist</h3>
                <p className="text-sm text-slate-500 max-w-md">
                  Search for stocks above and add them to your watchlist to track live prices and performance.
                </p>
              </CardContent>
            </Card>
          )}

          {!watchlistLoading && watchlistItems.length > 0 && (
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 mb-8 touch-none">
              {watchlistItems.map((item) => (
                <WatchlistCard
                  key={item.id}
                  item={item}
                  onRemove={handleRemoveFromWatchlist}
                  onViewDetails={handleWatchlistItemClick}
                  isDragging={draggingId === item.id}
                  isOver={overId === item.id}
                  gripHandlers={getGripHandlers(item.id)}
                  openSide={openPositionsBySide.get(item.instrument_key) ?? null}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Active Trade Suggestions + ML Activity sidebar */}
      <div>
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">Active Trade Suggestions</h2>
            <p className="text-sm text-slate-500">
              Multi-agent validated opportunities with high consensus scores
            </p>
          </div>
        </div>

        {/*
          Two-column layout on lg+: suggestions grid (flex-1) + ML Activity sidebar (w-72).
          On smaller screens the columns stack — ML Activity sits above the grid.
          CSS `order` controls stacking order without rendering the card twice.
        */}
        <div className="flex flex-col lg:flex-row lg:items-start gap-6">

          {/* ── ML Activity sidebar ── */}
          <div className="order-first lg:order-last lg:w-72 lg:shrink-0 lg:sticky lg:top-6 lg:self-start">
            <MLActivityCard items={activityItems} isConnected={isConnected} />
          </div>

          {/* ── Suggestions main column ── */}
          <div className="flex-1 min-w-0 order-last lg:order-first">
            <div className="mb-6">
              <SuggestionFilters filters={filters} onFiltersChange={setFilters} />
            </div>

            {/* Loading State */}
            {isLoading && (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {[...Array(6)].map((_, i) => (
                  <Card key={i} className="border-slate-200 bg-white animate-pulse">
                    <CardHeader className="pb-3">
                      <div className="h-6 w-32 bg-slate-200 rounded" />
                      <div className="h-4 w-48 bg-slate-100 rounded mt-2" />
                    </CardHeader>
                    <CardContent className="space-y-4">
                      <div className="h-2 w-full bg-slate-100 rounded" />
                      <div className="flex gap-2">
                        <div className="h-6 w-20 bg-slate-100 rounded-full" />
                        <div className="h-6 w-20 bg-slate-100 rounded-full" />
                        <div className="h-6 w-20 bg-slate-100 rounded-full" />
                      </div>
                      <div className="grid grid-cols-2 gap-3">
                        <div className="h-12 bg-slate-100 rounded" />
                        <div className="h-12 bg-slate-100 rounded" />
                      </div>
                    </CardContent>
                  </Card>
                ))}
              </div>
            )}

            {/* Error State */}
            {isError && (
              <Card className="border-red-200 bg-red-50">
                <CardContent className="flex items-center gap-3 py-6">
                  <AlertCircle className="h-5 w-5 text-red-600" />
                  <div>
                    <p className="font-medium text-red-900">Failed to load trade suggestions</p>
                    <p className="text-sm text-red-700">
                      {error instanceof Error ? error.message : "Please try again later"}
                    </p>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Empty State */}
            {!isLoading && !isError && suggestionsData?.suggestions.length === 0 && (
              <Card className="border-slate-200 bg-slate-50">
                <CardContent className="flex flex-col items-center justify-center py-12 text-center">
                  <Radar className="h-12 w-12 text-slate-400 mb-4" />
                  <h3 className="text-lg font-semibold text-slate-900 mb-2">No Active Suggestions</h3>
                  <p className="text-sm text-slate-500 max-w-md">
                    The correlation engine is analyzing market conditions. New trade suggestions
                    will appear here when multi-agent consensus is reached.
                  </p>
                </CardContent>
              </Card>
            )}

            {/* Suggestions Grid */}
            {!isLoading && !isError && suggestionsData && suggestionsData.suggestions.length > 0 && (
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {suggestionsData.suggestions.map((suggestion) => (
                  <TradeSuggestionCard
                    key={suggestion.suggestion_id}
                    suggestion={suggestion}
                    onViewDetails={handleViewDetails}
                  />
                ))}
              </div>
            )}

            {/* Pagination Info */}
            {suggestionsData && suggestionsData.total > 0 && (
              <div className="mt-6 text-center text-sm text-slate-500">
                Showing {suggestionsData.suggestions.length} of {suggestionsData.total} suggestions
              </div>
            )}
          </div>

        </div>
      </div>

      {/* Detail Pane Overlay */}
      {detailInstrument && (
        <DetailPane
          instrument={detailInstrument}
          onClose={handleCloseDetail}
          suggestion={currentDetailSuggestion ?? undefined}
        />
      )}
    </div>
  );
}
