"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { watchlistAPI, upstoxAPI, isNetworkError, type WatchlistItem, type WatchlistItemCreate } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

const LTP_POLL_INTERVAL_MS = 3000; // Poll every 3 seconds
const LTP_CACHE_TTL_MS = 5000; // Cache for 5 seconds

interface WatchlistItemWithPrice extends WatchlistItem {
  ltp: number | null;
  prevClose: number | null;
  loading: boolean;
}

interface LTPCache {
  ltp: number | null;
  prevClose: number | null;
  fetchedAt: number;
}

export function useWatchlist() {
  const { isAuthenticated } = useAuth();
  const queryClient = useQueryClient();
  const [itemsWithPrices, setItemsWithPrices] = useState<WatchlistItemWithPrice[]>([]);
  const ltpCacheRef = useRef<Map<string, LTPCache>>(new Map());
  const isMountedRef = useRef(true);

  // Fetch watchlist items
  const {
    data: watchlistItems = [],
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ["watchlist"],
    queryFn: watchlistAPI.getWatchlist,
    enabled: isAuthenticated,
    staleTime: 30000,
    refetchOnWindowFocus: true,
  });

  // Add to watchlist mutation
  const addMutation = useMutation({
    mutationFn: (item: WatchlistItemCreate) => watchlistAPI.addToWatchlist(item),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
  });

  // Remove from watchlist mutation
  const removeMutation = useMutation({
    mutationFn: (itemId: number) => watchlistAPI.removeFromWatchlist(itemId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
  });

  // Reorder watchlist mutation with optimistic update + rollback on failure.
  const reorderMutation = useMutation({
    mutationFn: ({ itemId, newPosition }: { itemId: number; newPosition: number }) =>
      watchlistAPI.reorderWatchlist({ item_id: itemId, new_position: newPosition }),

    onMutate: async ({ itemId, newPosition }) => {
      // Cancel any in-flight refetch so it doesn't overwrite our optimistic state.
      await queryClient.cancelQueries({ queryKey: ["watchlist"] });

      const previousItems = queryClient.getQueryData<WatchlistItem[]>(["watchlist"]);

      if (previousItems) {
        // Work on a position-sorted copy so index math is predictable.
        const sorted = [...previousItems].sort((a, b) => a.position - b.position);
        const sourceIdx = sorted.findIndex((i) => i.id === itemId);
        const targetIdx = sorted.findIndex((i) => i.position === newPosition);

        if (sourceIdx !== -1 && targetIdx !== -1 && sourceIdx !== targetIdx) {
          const [moved] = sorted.splice(sourceIdx, 1);
          sorted.splice(targetIdx, 0, moved);
          // Re-assign contiguous positions so the list stays consistent.
          const reordered = sorted.map((item, idx) => ({ ...item, position: idx + 1 }));
          queryClient.setQueryData<WatchlistItem[]>(["watchlist"], reordered);
        }
      }

      // Return snapshot for rollback in onError.
      return { previousItems };
    },

    onError: (_err, _vars, context) => {
      // Roll back to the pre-mutation state if the API call fails.
      if (context?.previousItems) {
        queryClient.setQueryData<WatchlistItem[]>(["watchlist"], context.previousItems);
      }
    },

    onSettled: () => {
      // Always re-sync with server truth after the mutation completes.
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
  });

  // Check if item is in watchlist
  const checkInWatchlist = useCallback(
    async (instrumentKey: string) => {
      if (!isAuthenticated) return { in_watchlist: false, item_id: null };
      return watchlistAPI.checkInWatchlist(instrumentKey);
    },
    [isAuthenticated]
  );

  // Fetch LTP for a single instrument
  const fetchLTP = useCallback(async (instrumentKey: string): Promise<LTPCache> => {
    const now = Date.now();
    const cached = ltpCacheRef.current.get(instrumentKey);

    // Return cached if fresh
    if (cached && now - cached.fetchedAt <= LTP_CACHE_TTL_MS) {
      return cached;
    }

    try {
      const response = await upstoxAPI.getLtpQuote(instrumentKey);
      const ltpData: LTPCache = {
        ltp: response?.last_price ?? null,
        prevClose: response?.prev_close_price ?? null,
        fetchedAt: now,
      };
      ltpCacheRef.current.set(instrumentKey, ltpData);
      return ltpData;
    } catch (error) {
      if (!isNetworkError(error)) console.error(`[useWatchlist] Failed to fetch LTP for ${instrumentKey}:`, error);
      return { ltp: null, prevClose: null, fetchedAt: now };
    }
  }, []);

  // Fetch LTPs for all watchlist items
  const fetchAllLTPs = useCallback(async () => {
    if (!watchlistItems.length || !isMountedRef.current) return;

    const updatedItems: WatchlistItemWithPrice[] = await Promise.all(
      watchlistItems.map(async (item) => {
        const ltpData = await fetchLTP(item.instrument_key);
        return {
          ...item,
          ltp: ltpData.ltp,
          prevClose: ltpData.prevClose,
          loading: false,
        };
      })
    );

    if (isMountedRef.current) {
      setItemsWithPrices(updatedItems);
    }
  }, [watchlistItems, fetchLTP]);

  // Poll for live prices
  useEffect(() => {
    if (!watchlistItems.length || !isAuthenticated) {
      if (itemsWithPrices.length > 0) {
        setItemsWithPrices([]);
      }
      return;
    }

    // Initial fetch
    fetchAllLTPs();

    // Set up polling
    const intervalId = setInterval(fetchAllLTPs, LTP_POLL_INTERVAL_MS);

    return () => {
      clearInterval(intervalId);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlistItems, isAuthenticated]);

  // Cleanup on unmount
  useEffect(() => {
    isMountedRef.current = true;
    return () => {
      isMountedRef.current = false;
    };
  }, []);

  return {
    // Data
    items: itemsWithPrices,
    isLoading,
    isError,
    error,

    // Mutations
    addToWatchlist: addMutation.mutateAsync,
    removeFromWatchlist: removeMutation.mutateAsync,
    reorderWatchlist: reorderMutation.mutateAsync,
    checkInWatchlist,

    // Mutation states
    isAdding: addMutation.isPending,
    isRemoving: removeMutation.isPending,
    isReordering: reorderMutation.isPending,
  };
}
