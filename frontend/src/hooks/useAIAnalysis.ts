/**
 * React Query hook for AI analysis data
 */
import { useQuery } from '@tanstack/react-query';
import { caiAPI } from '@/lib/api';
import { useAuth } from '@/contexts/AuthContext';
import type { AIAnalysisResponse } from '@/types/analysis';

export function useAIAnalysis(symbol: string) {
  const { isAuthenticated, isAuthReady } = useAuth();

  return useQuery<AIAnalysisResponse>({
    queryKey: ['ai-analysis', symbol],
    queryFn: () => caiAPI.getAIAnalysis(symbol),
    enabled: isAuthReady && isAuthenticated && !!symbol,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
  });
}
