/**
 * Real-time models hook with WebSocket integration
 * 
 * Combines polling with WebSocket updates for drift alerts.
 * Requirements: 21.1, 21.2
 */
import { useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";
import { useModels, useDriftReports } from "./useModels";
import { useCAIWebSocket } from "./useCAIWebSocket";
import type { ModelFilters } from "@/types/models";

export function useModelsRealtime(filters: ModelFilters = {}) {
  const queryClient = useQueryClient();
  const modelsQuery = useModels(filters);
  const driftReportsQuery = useDriftReports();

  // Handle incoming drift alert updates from WebSocket
  const handleDriftAlert = useCallback((alert: any) => {
    // Invalidate drift reports to refetch
    queryClient.invalidateQueries({ queryKey: ["drift-reports"] });
    
    // Also invalidate models query as drift may affect model state
    queryClient.invalidateQueries({ queryKey: ["models"] });
  }, [queryClient]);

  // Connect to WebSocket for real-time updates
  const { status: wsStatus } = useCAIWebSocket({
    onModelAlert: handleDriftAlert,
    autoConnect: true,
  });

  return {
    models: modelsQuery,
    driftReports: driftReportsQuery,
    wsStatus,
  };
}
