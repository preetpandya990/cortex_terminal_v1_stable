/**
 * React Query hook for AI analysis data
 */
import { useQuery } from '@tanstack/react-query';
import { caiAPI } from '@/lib/api-client';
import type { AIAnalysisResponse } from '@/types/analysis';

export function useAIAnalysis(symbol: string) {
  return useQuery<AIAnalysisResponse>({
    queryKey: ['ai-analysis', symbol],
    queryFn: () => caiAPI.getAIAnalysis(symbol),
    enabled: !!symbol,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });
}
