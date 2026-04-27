/**
 * React Query hook for verdict analysis data
 */
import { useQuery } from '@tanstack/react-query';
import { caiAPI } from '@/lib/api';
import { useAuth } from '@/contexts/AuthContext';
import type { VerdictResponse } from '@/types/analysis';

export function useVerdictAnalysis(symbol: string) {
  const { isAuthenticated, isAuthReady } = useAuth();

  return useQuery<VerdictResponse>({
    queryKey: ['verdict-analysis', symbol],
    queryFn: () => caiAPI.getVerdictAnalysis(symbol),
    enabled: isAuthReady && isAuthenticated && !!symbol,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });
}
