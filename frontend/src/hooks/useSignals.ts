/**
 * Hook for fetching and managing trading signals
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from '@/contexts/AuthContext';
import type {
  TradingSignalsResponse,
  TradingSignal,
  SignalFilters,
  SignalAuditResponse,
} from "@/types/signals";

/**
 * Fetch trading signals with filters
 */
export function useSignals(filters: SignalFilters = {}) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<TradingSignalsResponse>({
    queryKey: ["signals", filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.symbol) params.append("symbol", filters.symbol);
      if (filters.signal_type) params.append("signal_type", filters.signal_type);
      if (filters.min_confidence !== undefined) {
        params.append("min_confidence", filters.min_confidence.toString());
      }
      if (filters.page) params.append("page", filters.page.toString());
      if (filters.limit) params.append("limit", filters.limit.toString());
      
      const response = await api.get<TradingSignalsResponse>(
        `fusion/signals?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 30000,
  });
}

/**
 * Fetch a single signal by ID
 */
export function useSignal(signalId: string) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<TradingSignal>({
    queryKey: ["signal", signalId],
    queryFn: async () => {
      const response = await api.get<TradingSignal>(
        `fusion/signals/${signalId}`
      );
      return response.data;
    },
    enabled: isAuthenticated && !!signalId,
  });
}

/**
 * Fetch signal audit trail
 */
export function useSignalAudit(signalId: string) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<SignalAuditResponse>({
    queryKey: ["signal-audit", signalId],
    queryFn: async () => {
      const response = await api.get<SignalAuditResponse>(
        `fusion/signals/${signalId}/audit`
      );
      return response.data;
    },
    enabled: isAuthenticated && !!signalId,
  });
}
