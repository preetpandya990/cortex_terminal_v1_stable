/**
 * Hook for fetching and managing events
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from '@/contexts/AuthContext';
import type {
  ProcessedEventsResponse,
  EventDetail,
  EventFilters,
} from "@/types/events";

/**
 * Fetch processed events with filters
 */
export function useEvents(filters: EventFilters = {}) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<ProcessedEventsResponse>({
    queryKey: ["events", filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.symbol) params.append("symbol", filters.symbol);
      if (filters.event_type) params.append("event_type", filters.event_type);
      if (filters.min_impact !== undefined) {
        params.append("min_impact", filters.min_impact.toString());
      }
      if (filters.page) params.append("page", filters.page.toString());
      if (filters.limit) params.append("limit", filters.limit.toString());
      
      const response = await api.get<ProcessedEventsResponse>(
        `intelligence/events?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 30000,
  });
}

/**
 * Fetch a single event by ID
 */
export function useEvent(eventId: string) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<EventDetail>({
    queryKey: ["event", eventId],
    queryFn: async () => {
      const response = await api.get<EventDetail>(
        `intelligence/events/${eventId}`
      );
      return response.data;
    },
    enabled: isAuthenticated && !!eventId,
  });
}
