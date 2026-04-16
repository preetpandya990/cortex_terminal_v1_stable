/**
 * React Query hook for ML analysis data
 */
import { useQuery } from '@tanstack/react-query';
import { caiAPI } from '@/lib/api-client';
import type { MLAnalysisResponse } from '@/types/analysis';

export function useMLAnalysis(symbol: string) {
  return useQuery<MLAnalysisResponse>({
    queryKey: ['ml-analysis', symbol],
    queryFn: () => caiAPI.getMLAnalysis(symbol),
    enabled: !!symbol,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });
}
