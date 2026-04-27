"use client";

import { useState, useEffect, useCallback } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams, useRouter } from "next/navigation";
import { Radar, Loader2, AlertCircle, Star } from "lucide-react";
import { tradeSuggestionsAPI, isNetworkError } from "@/lib/api";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InstrumentSearchCombobox } from "@/components/market/InstrumentSearchCombobox";
import { TradeSuggestionCard } from "./components/TradeSuggestionCard";
import { WatchlistCard } from "./components/WatchlistCard";
import { DetailPane } from "./components/DetailPane";
import { SuggestionFilters } from "./components/SuggestionFilters";
import { SuggestionStats } from "./components/SuggestionStats";
import { useWebSocket, type WebSocketMessage } from "@/hooks/useWebSocket";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useDragReorder } from "@/hooks/useDragReorder";
import { useAuth } from "@/contexts/AuthContext";
import type { UpstoxInstrument } from "@/types/upstox";
import type { TradeSuggestion, SuggestionFilters as Filters } from "@/types/trade_suggestions";

export default function HawkEyeRadarPage() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { isAuthenticated, isAuthReady, accessToken, isLoading: authLoading } = useAuth();
  const [selectedInstrument, setSelectedInstrument] = useState<UpstoxInstrument | null>(null);
  const [detailSuggestion, setDetailSuggestion] = useState<TradeSuggestion | null>(null);
  const [filters, setFilters] = useState<Filters>({ status: "active", page: 1, page_size: 50 });
  const queryClient = useQueryClient();

  // Watchlist hook
  const {
    items: watchlistItems,
    removeFromWatchlist,
    reorderWatchlist,
    isLoading: watchlistLoading,
  } = useWatchlist();

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
      if (data.type === 'new_suggestion') {
        console.log('[Hawk-Eye] New suggestion received:', data.suggestion_id);
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
    enabled: isAuthenticated && !authLoading, // Only fetch when authenticated
    refetchInterval: isConnected ? false : 30000, // Only poll if WebSocket disconnected
    retry: (failureCount, error: any) => {
      // Don't retry 401 errors (authentication required)
      if (error?.response?.status === 401) {
        return false;
      }
      // Retry other errors up to 3 times
      return failureCount < 3;
    },
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000), // Exponential backoff
    staleTime: 30000, // Consider data fresh for 30 seconds
  });

  // Read instrument_key from URL on mount
  useEffect(() => {
    const instrumentKey = searchParams.get('instrument_key');
    if (instrumentKey && !selectedInstrument && suggestionsData) {
      // Find suggestion with this instrument_key
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
        // Open DetailPane for deep-linked suggestions
        setDetailSuggestion(suggestion);
        // Update URL to use instrument_key (canonical parameter)
        const params = new URLSearchParams(searchParams.toString());
        params.delete('suggestion_id'); // Remove legacy parameter
        params.set('instrument_key', suggestion.instrument_key);
        router.replace(`?${params.toString()}`, { scroll: false });
      }
    }
  }, [searchParams, suggestionsData, detailSuggestion, router]);

  const handleViewDetails = (suggestionId: string) => {
    const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
    if (suggestion) {
      // Open DetailPane with chart for technical analysis
      setDetailSuggestion(suggestion);
      // Update URL with instrument_key for shareability and deep linking
      const params = new URLSearchParams(searchParams.toString());
      params.set('instrument_key', suggestion.instrument_key);
      router.push(`?${params.toString()}`, { scroll: false });
    }
  };

  const handleManualSelect = (instrument: UpstoxInstrument) => {
    setSelectedInstrument(instrument);
    // Update URL with instrument_key
    const params = new URLSearchParams(searchParams.toString());
    params.set('instrument_key', instrument.instrument_key);
    router.push(`?${params.toString()}`, { scroll: false });
  };

  const handleCloseDetail = () => {
    setDetailSuggestion(null);
    setSelectedInstrument(null);
    // Remove instrument_key from URL
    const params = new URLSearchParams(searchParams.toString());
    params.delete('instrument_key');
    const newUrl = params.toString() ? `?${params.toString()}` : '/hawk-eye-radar';
    router.push(newUrl, { scroll: false });
  };

  const handleWatchlistItemClick = useCallback((instrumentKey: string) => {
    // Suppress navigation when the click was synthesised at the end of a drag.
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

  // Determine which instrument to show in detail pane
  const detailInstrument = selectedInstrument || (detailSuggestion ? {
    instrument_key: detailSuggestion.instrument_key,
    trading_symbol: detailSuggestion.trading_symbol || detailSuggestion.symbol,
    name: detailSuggestion.symbol,
    exchange: "NSE",
  } as UpstoxInstrument : null);

  if (!isAuthReady || !isAuthenticated) return null;

  return (
    <div className="space-y-8">
      {/* Page Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900">Hawk-Eye Radar</h1>
      </div>

      {/* Stats Dashboard */}
      {/* <SuggestionStats /> */}

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
            // touch-action:none prevents browser scroll from interfering while
            // the user is pressing on a grip handle.
            <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3 mb-8 touch-none">
              {watchlistItems.map((item) => (
                <WatchlistCard
                  key={item.id}
                  item={item}
                  ltp={item.ltp}
                  prevClose={item.prevClose}
                  onRemove={handleRemoveFromWatchlist}
                  onViewDetails={handleWatchlistItemClick}
                  isDragging={draggingId === item.id}
                  isOver={overId === item.id}
                  gripHandlers={getGripHandlers(item.id)}
                />
              ))}
            </div>
          )}
        </div>
      )}

      {/* Trade Suggestions Grid */}
      <div>
        <div className="mb-4 flex items-center justify-between gap-4">
          <div>
            <h2 className="text-lg font-semibold text-slate-900">Active Trade Suggestions</h2>
            <p className="text-sm text-slate-500">
              Multi-agent validated opportunities with high consensus scores
            </p>
          </div>
        </div>

        {/* Filters */}
        <div className="mb-6">
          <SuggestionFilters filters={filters} onFiltersChange={setFilters} />
        </div>

        {/* Loading State */}
        {isLoading && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
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
                The correlation engine is analyzing market conditions. New trade suggestions will appear here when
                multi-agent consensus is reached.
              </p>
            </CardContent>
          </Card>
        )}

        {/* Suggestions Grid */}
        {!isLoading && !isError && suggestionsData && suggestionsData.suggestions.length > 0 && (
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
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

      {/* Detail Pane Overlay */}
      {detailInstrument && (
        <DetailPane
          instrument={detailInstrument}
          onClose={handleCloseDetail}
        />
      )}
    </div>
  );
}
