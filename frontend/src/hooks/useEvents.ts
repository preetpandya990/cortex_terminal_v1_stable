/**
 * Hook for fetching market events from the intelligence API.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from "@/contexts/AuthContext";
import type { MarketEventsResponse, EventFilters } from "@/types/events";

export function useEvents(filters: EventFilters = {}) {
  const { isAuthenticated } = useAuth();

  return useQuery<MarketEventsResponse>({
    queryKey: ["events", filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.symbol)      params.append("symbol", filters.symbol);
      if (filters.event_type)  params.append("event_type", filters.event_type);
      if (filters.min_impact !== undefined) params.append("min_impact", String(filters.min_impact));
      if (filters.page)        params.append("page", String(filters.page));
      if (filters.limit)       params.append("limit", String(filters.limit));

      const response = await api.get<MarketEventsResponse>(
        `intelligence/events?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });
}
