/**
 * Real-time regime hook with WebSocket integration
 * 
 * Combines polling with WebSocket updates for optimal real-time performance.
 * Requirements: 21.1, 21.3
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { useCurrentRegime } from "./useRegime";
import { useCAIWebSocket } from "./useCAIWebSocket";
import type { CurrentRegime } from "@/types/regime";

export function useRegimeRealtime(symbol: string) {
  const queryClient = useQueryClient();
  const regimeQuery = useCurrentRegime(symbol);

  // Handle incoming regime updates from WebSocket
  const handleRegimeUpdate = useCallback((regime: CurrentRegime) => {
    // Only update if it's for the current symbol
    if (regime.symbol !== symbol) {
      return;
    }

    // Update the regime query cache
    queryClient.setQueryData(
      ["regime", "current", symbol],
      regime
    );
  }, [queryClient, symbol]);

  // Connect to WebSocket for real-time updates
  const { status: wsStatus } = useCAIWebSocket({
    onRegime: handleRegimeUpdate,
    autoConnect: true,
  });

  return {
    ...regimeQuery,
    wsStatus,
  };
}
