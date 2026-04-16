/**
 * React Query hook for verdict analysis data
 */
import { useQuery } from '@tanstack/react-query';
import { caiAPI } from '@/lib/api-client';
import type { VerdictResponse } from '@/types/analysis';

export function useVerdictAnalysis(symbol: string) {
  return useQuery<VerdictResponse>({
    queryKey: ['verdict-analysis', symbol],
    queryFn: () => caiAPI.getVerdictAnalysis(symbol),
    enabled: !!symbol,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });
}
