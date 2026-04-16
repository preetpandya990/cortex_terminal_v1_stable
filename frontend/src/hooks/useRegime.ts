/**
 * Hook for fetching regime detection data
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from '@/contexts/AuthContext';
import type {
  CurrentRegime,
  RegimeHistory,
  ActiveStrategiesResponse,
} from "@/types/regime";

/**
/**
 * Fetch current market regime
 */
export function useCurrentRegime(symbol?: string) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<CurrentRegime>({
    queryKey: ["regime", symbol],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (symbol) params.append("symbol", symbol);
      
      const response = await api.get<CurrentRegime>(
        `strategy/regime?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 30000,
  });
}

/**
/**
 * Fetch regime history
 */
export function useRegimeHistory(symbol?: string, days: number = 1) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<RegimeHistory>({
    queryKey: ["regime-history", symbol, days],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (symbol) params.append("symbol", symbol);
      params.append("days", days.toString());
      
      const response = await api.get<RegimeHistory>(
        `strategy/regime/history?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 60000,
  });
}

/**
/**
 * Fetch active trading strategies
 */
export function useActiveStrategies(symbol?: string) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<ActiveStrategiesResponse>({
    queryKey: ["strategies", symbol],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (symbol) params.append("symbol", symbol);
      
      const response = await api.get<ActiveStrategiesResponse>(
        `strategy/strategies?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 30000,
  });
}
