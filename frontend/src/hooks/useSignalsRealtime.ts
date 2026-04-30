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
    // Normalise signal_id to string — the backend guarantees a string, but guard
    // against any future stub payloads that send a raw integer.
    const incomingId = String(signal.signal_id);

    queryClient.setQueryData(
      ["signals", filters],
      (oldData: any) => {
        if (!oldData) return oldData;

        // Filter: skip if signal doesn't match active filters
        if (filters.symbol && signal.symbol !== filters.symbol) return oldData;
        if (filters.signal_type && signal.signal_type !== filters.signal_type) return oldData;
        if (filters.min_confidence !== undefined && signal.calibrated_confidence < filters.min_confidence) return oldData;

        const existing: TradingSignal[] = oldData.signals || [];

        // Deduplicate: skip if this signal_id is already in the cache.
        // Guards against double-publish from any server-side code path.
        if (existing.some((s) => String(s.signal_id) === incomingId)) {
          return oldData;
        }

        const updated = [signal, ...existing];
        if (updated.length > (filters.limit || 50)) {
          updated.pop();
        }

        return {
          ...oldData,
          signals: updated,
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
