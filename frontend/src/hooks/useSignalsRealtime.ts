/**
 * Real-time signals hook with WebSocket integration
 * 
 * Combines polling with WebSocket updates for optimal real-time performance.
 * Requirements: 21.1, 21.2
 */
import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useCallback } from "react";
import { useSignals } from "./useSignals";
import { useCAIWebSocket } from "./useCAIWebSocket";
import type { SignalFilters, TradingSignal } from "@/types/signals";

export function useSignalsRealtime(filters: SignalFilters = {}) {
  const queryClient = useQueryClient();
  const signalsQuery = useSignals(filters);

  // Handle incoming signal updates from WebSocket
  const handleSignalUpdate = useCallback((signal: TradingSignal) => {
    // Update the signals query cache with new signal
    queryClient.setQueryData(
      ["signals", filters],
      (oldData: any) => {
        if (!oldData) return oldData;

        // Check if signal matches current filters
        if (filters.symbol && signal.symbol !== filters.symbol) {
          return oldData;
        }
        if (filters.signal_type && signal.signal_type !== filters.signal_type) {
          return oldData;
        }
        if (filters.min_confidence && signal.confidence < filters.min_confidence) {
          return oldData;
        }

        // Add new signal to the beginning of the list
        const updatedSignals = [signal, ...(oldData.signals || [])];
        
        // Limit to reasonable cache size
        const maxCacheSize = filters.limit || 50;
        if (updatedSignals.length > maxCacheSize) {
          updatedSignals.pop();
        }

        return {
          ...oldData,
          signals: updatedSignals,
          total: oldData.total + 1,
        };
      }
    );
  }, [queryClient, filters]);

  // Connect to WebSocket for real-time updates
  const { status: wsStatus } = useCAIWebSocket({
    onSignal: handleSignalUpdate,
    autoConnect: true,
  });

  return {
    ...signalsQuery,
    wsStatus,
  };
}
