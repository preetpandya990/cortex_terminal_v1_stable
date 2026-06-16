"use client";

import { useState, useEffect, useCallback, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams, useRouter } from "next/navigation";
import { Radar, AlertCircle, Star } from "lucide-react";
import { tradeSuggestionsAPI, isNetworkError, type CorrelationActivityItem } from "@/lib/api";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { TradeSuggestionCard } from "./components/TradeSuggestionCard";
import { WatchlistCard } from "./components/WatchlistCard";
import { DetailPane } from "./components/DetailPane";
import { SuggestionDetailModal } from "./components/SuggestionDetailModal";
import { SuggestionFilters } from "./components/SuggestionFilters";
import { MLActivityCard } from "./components/MLActivityCard";
import { KeyboardShortcutsPanel } from "@/components/KeyboardShortcutsPanel";
import { useWebSocket, type WebSocketMessage } from "@/hooks/useWebSocket";
import { useMLActivity } from "@/hooks/useMLActivity";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useDragReorder } from "@/hooks/useDragReorder";
import { useAuth } from "@/contexts/AuthContext";
import { usePositions } from "@/hooks/usePaperTrading";
import type { UpstoxInstrument } from "@/types/upstox";
import type { TradeSuggestion, SuggestionFilters as Filters } from "@/types/trade_suggestions";
import type { PositionSide } from "@/types/paper_trading";

function isExpiredSuggestion(s: TradeSuggestion): boolean {
  return new Date(s.expires_at).getTime() < Date.now();
}

export default function HawkEyeRadarClient() {
  const searchParams = useSearchParams();
  const router = useRouter();
  const { isAuthenticated, isAuthReady, accessToken, isLoading: authLoading } = useAuth();
  const [selectedInstrument, setSelectedInstrument] = useState<UpstoxInstrument | null>(null);
  const [detailSuggestion, setDetailSuggestion] = useState<TradeSuggestion | null>(null);
  // Suggestion shown in the focused SuggestionDetailModal (opened from a card).
  // Distinct from detailSuggestion, which drives the full-view DetailPane.
  const [modalSuggestion, setModalSuggestion] = useState<TradeSuggestion | null>(null);
  const [filters, setFilters] = useState<Filters>({ status: "active", page: 1, page_size: 50 });
  const queryClient = useQueryClient();

  // Keyboard navigation state
  const [focusedCardIndex, setFocusedCardIndex] = useState(0);
  const [showShortcutsPanel, setShowShortcutsPanel] = useState(false);

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
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    retry: (failureCount, error: any) => {
      if (error?.response?.status === 401) return false;
      return failureCount < 3;
    },
    retryDelay: (attemptIndex) => Math.min(1000 * 2 ** attemptIndex, 30000),
    staleTime: 30000,
  });

  // Stable flat list of suggestions for keyboard navigation.
  const suggestions = suggestionsData?.suggestions ?? [];

  // Index of the currently open modal suggestion within the suggestions list.
  const currentModalIndex = useMemo(
    () =>
      modalSuggestion
        ? suggestions.findIndex((s) => s.suggestion_id === modalSuggestion.suggestion_id)
        : -1,
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // suggestions identity changes on every refetch; compare by suggestion_id instead.
    [modalSuggestion?.suggestion_id, suggestions],
  );

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

  // Read suggestion_id from URL on mount → open the focused modal (deep linking).
  // Intentionally excludes modalSuggestion from deps: including it causes the modal
  // to reopen immediately after close because router.push is async and the URL still
  // carries suggestion_id during the render cycle where modalSuggestion becomes null.
  useEffect(() => {
    const suggestionId = searchParams.get('suggestion_id');
    if (suggestionId && !modalSuggestion && suggestionsData) {
      const suggestion = suggestionsData.suggestions.find(
        s => s.suggestion_id === suggestionId
      );
      if (suggestion) {
        setModalSuggestion(suggestion);
      }
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams, suggestionsData]);

  // Global keyboard shortcuts: ? = shortcuts panel, / = focus filters.
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent): void {
      // Don't fire when typing in inputs.
      const tag = (document.activeElement as HTMLElement)?.tagName ?? "";
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
      if ((document.activeElement as HTMLElement)?.isContentEditable) return;
      if (e.ctrlKey || e.altKey || e.metaKey) return;

      if (e.key === "?") {
        e.preventDefault();
        setShowShortcutsPanel((v) => !v);
      }

      if (e.key === "/" && !modalSuggestion) {
        e.preventDefault();
        document
          .getElementById("hawk-eye-filter-bar")
          ?.querySelector<HTMLElement>('[role="combobox"], button')
          ?.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [modalSuggestion]);

  // Suggestion card → open the focused modal (not the full DetailPane).
  // Keyed in the URL by suggestion_id so the view is shareable/deep-linkable.
  const handleViewDetails = useCallback((suggestionId: string) => {
    const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
    if (suggestion) {
      setModalSuggestion(suggestion);
      const params = new URLSearchParams(searchParams.toString());
      params.set('suggestion_id', suggestion.suggestion_id);
      params.delete('instrument_key');
      router.push(`?${params.toString()}`, { scroll: false });
    }
  }, [suggestionsData?.suggestions, searchParams, router]);

  const handleCloseModal = useCallback(() => {
    setModalSuggestion(null);
    const params = new URLSearchParams(searchParams.toString());
    params.delete('suggestion_id');
    const newUrl = params.toString() ? `?${params.toString()}` : '/hawk-eye-radar';
    router.push(newUrl, { scroll: false });

    // Restore focus to the originating grid card.
    requestAnimationFrame(() => {
      const s = suggestions[focusedCardIndex];
      if (s) document.getElementById(`card-${s.suggestion_id}`)?.focus();
    });
  }, [searchParams, router, suggestions, focusedCardIndex]);

  // Navigate to the previous suggestion while the modal is open.
  const handleModalPrevious = useCallback(() => {
    if (currentModalIndex <= 0) return;
    const prev = suggestions[currentModalIndex - 1];
    setModalSuggestion(prev);
    setFocusedCardIndex(currentModalIndex - 1);
    const params = new URLSearchParams(searchParams.toString());
    params.set('suggestion_id', prev.suggestion_id);
    router.push(`?${params.toString()}`, { scroll: false });
  }, [currentModalIndex, suggestions, searchParams, router]);

  // Navigate to the next suggestion while the modal is open.
  const handleModalNext = useCallback(() => {
    if (currentModalIndex >= suggestions.length - 1) return;
    const next = suggestions[currentModalIndex + 1];
    setModalSuggestion(next);
    setFocusedCardIndex(currentModalIndex + 1);
    const params = new URLSearchParams(searchParams.toString());
    params.set('suggestion_id', next.suggestion_id);
    router.push(`?${params.toString()}`, { scroll: false });
  }, [currentModalIndex, suggestions, searchParams, router]);

  // Modal "Open full view" → promote to the DetailPane (chart, live price, trade).
  const handleOpenFullView = useCallback(() => {
    if (!modalSuggestion) return;
    setDetailSuggestion(modalSuggestion);
    setModalSuggestion(null);
    const params = new URLSearchParams(searchParams.toString());
    params.delete('suggestion_id');
    params.set('instrument_key', modalSuggestion.instrument_key);
    router.push(`?${params.toString()}`, { scroll: false });
  }, [modalSuggestion, searchParams, router]);

  const handleCloseDetail = useCallback(() => {
    setDetailSuggestion(null);
    setSelectedInstrument(null);
    const params = new URLSearchParams(searchParams.toString());
    params.delete('instrument_key');
    const newUrl = params.toString() ? `?${params.toString()}` : '/hawk-eye-radar';
    router.push(newUrl, { scroll: false });
  }, [searchParams, router]);

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

  // Arrow-key navigation across the suggestions grid.
  const handleGridKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "Enter", " "].includes(e.key)) return;

    const total = suggestions.length;
    if (total === 0) return;

    // Column count from CSS breakpoints (matches md:grid-cols-2 xl:grid-cols-3).
    const cols =
      window.innerWidth >= 1280 ? 3 : window.innerWidth >= 768 ? 2 : 1;
    const col = focusedCardIndex % cols;

    let next = focusedCardIndex;

    switch (e.key) {
      case "ArrowRight":
        e.preventDefault();
        if (col < cols - 1 && focusedCardIndex + 1 < total) next = focusedCardIndex + 1;
        break;
      case "ArrowLeft":
        e.preventDefault();
        if (col > 0) next = focusedCardIndex - 1;
        break;
      case "ArrowDown":
        e.preventDefault();
        next = Math.min(focusedCardIndex + cols, total - 1);
        break;
      case "ArrowUp":
        e.preventDefault();
        next = Math.max(focusedCardIndex - cols, 0);
        break;
      case "Home":
        e.preventDefault();
        next = 0;
        break;
      case "End":
        e.preventDefault();
        next = total - 1;
        break;
      case "Enter":
      case " ": {
        e.preventDefault();
        const s = suggestions[focusedCardIndex];
        if (s && !isExpiredSuggestion(s)) handleViewDetails(s.suggestion_id);
        return;
      }
    }

    if (next !== focusedCardIndex) {
      setFocusedCardIndex(next);
      requestAnimationFrame(() => {
        document.getElementById(`card-${suggestions[next].suggestion_id}`)?.focus();
      });
    }
  }, [focusedCardIndex, suggestions, handleViewDetails]);

  // Always use the freshest version of detailSuggestion from the live query result.
  const currentDetailSuggestion = useMemo(() => {
    if (!detailSuggestion) return null;
    return (
      suggestionsData?.suggestions.find(
        (s) => s.suggestion_id === detailSuggestion.suggestion_id,
      ) ?? detailSuggestion
    );
  }, [detailSuggestion?.suggestion_id, suggestionsData?.suggestions]);

  // Same freshness guarantee for the modal.
  const currentModalSuggestion = useMemo(() => {
    if (!modalSuggestion) return null;
    return (
      suggestionsData?.suggestions.find(
        (s) => s.suggestion_id === modalSuggestion.suggestion_id,
      ) ?? modalSuggestion
    );
  }, [modalSuggestion?.suggestion_id, suggestionsData?.suggestions]);

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

        <div className="flex flex-col lg:flex-row lg:items-start gap-6">

          {/* ── ML Activity sidebar ── */}
          <div className="order-first lg:order-last lg:w-72 lg:shrink-0 lg:sticky lg:top-6 lg:self-start">
            <MLActivityCard items={activityItems} isConnected={isConnected} />
          </div>

          {/* ── Suggestions main column ── */}
          <div className="flex-1 min-w-0 order-last lg:order-first">
            <div className="mb-6" id="hawk-eye-filter-bar">
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
            {!isLoading && !isError && suggestions.length === 0 && (
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
            {!isLoading && !isError && suggestions.length > 0 && (
              <div
                className="grid gap-4 md:grid-cols-2 xl:grid-cols-3"
                role="grid"
                aria-label="Trade suggestions"
                onKeyDown={handleGridKeyDown}
              >
                {suggestions.map((suggestion, index) => (
                  <TradeSuggestionCard
                    key={suggestion.suggestion_id}
                    cardId={`card-${suggestion.suggestion_id}`}
                    suggestion={suggestion}
                    onViewDetails={handleViewDetails}
                    tabIndex={focusedCardIndex === index ? 0 : -1}
                    onFocusCapture={() => setFocusedCardIndex(index)}
                  />
                ))}
              </div>
            )}

            {/* Pagination Info */}
            {suggestionsData && suggestionsData.total > 0 && (
              <div className="mt-6 text-center text-sm text-slate-500">
                Showing {suggestions.length} of {suggestionsData.total} suggestions
              </div>
            )}
          </div>

        </div>
      </div>

      {/* Focused suggestion modal — opens from a TradeSuggestionCard */}
      <SuggestionDetailModal
        suggestion={currentModalSuggestion}
        open={currentModalSuggestion !== null}
        onClose={handleCloseModal}
        onOpenFullView={handleOpenFullView}
        onPrevious={handleModalPrevious}
        onNext={handleModalNext}
        hasPrevious={currentModalIndex > 0}
        hasNext={currentModalIndex < suggestions.length - 1}
        positionLabel={currentModalIndex >= 0 ? `${currentModalIndex + 1} of ${suggestions.length}` : undefined}
      />

      {/* Keyboard shortcuts cheatsheet */}
      <KeyboardShortcutsPanel
        open={showShortcutsPanel}
        onClose={() => setShowShortcutsPanel(false)}
      />

      {/* Detail Pane Overlay — full view (chart, live price, trade) */}
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
