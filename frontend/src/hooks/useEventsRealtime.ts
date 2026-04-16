/**
 * Real-time events hook with WebSocket integration
 *
 * Combines polling with WebSocket updates for high-impact events.
 * Requirements: 21.1, 21.2
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { useEvents } from "./useEvents";
import { useCAIWebSocket } from "./useCAIWebSocket";
import type { EventFilters, EventDetail } from "@/types/events";

export function useEventsRealtime(filters: EventFilters = {}) {
  const queryClient = useQueryClient();
  const eventsQuery = useEvents(filters);

  const handleEventUpdate = useCallback((event: EventDetail) => {
    queryClient.setQueryData(
      ["events", filters],
      (oldData: any) => {
        if (!oldData) return oldData;

        // Filter by symbol — events use affected_symbols[]
        if (filters.symbol && !event.affected_symbols.includes(filters.symbol)) {
          return oldData;
        }
        if (filters.event_type && event.event_type !== filters.event_type) {
          return oldData;
        }
        if (filters.min_impact && event.impact_score < filters.min_impact) {
          return oldData;
        }

        const updatedEvents = [event, ...(oldData.events || [])];
        const maxCacheSize = filters.limit || 50;
        if (updatedEvents.length > maxCacheSize) {
          updatedEvents.pop();
        }

        return {
          ...oldData,
          events: updatedEvents,
          total: oldData.total + 1,
        };
      }
    );
  }, [queryClient, filters]);

  const { status: wsStatus } = useCAIWebSocket({
    onEvent: handleEventUpdate,
    autoConnect: true,
  });

  return {
    ...eventsQuery,
    wsStatus,
  };
}
