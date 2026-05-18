import { useQuery } from '@tanstack/react-query';
import { fundamentalsAPI } from '@/lib/api';
import { useAuth } from '@/contexts/AuthContext';
import type { FundamentalsFullResponse } from '@/types/fundamentals';

export function useFundamentals(instrumentKey: string | null) {
  const { isAuthenticated, isAuthReady } = useAuth();

  return useQuery<FundamentalsFullResponse>({
    queryKey: ['fundamentals', instrumentKey],
    queryFn: () => fundamentalsAPI.get(instrumentKey!),
    enabled: isAuthReady && isAuthenticated && !!instrumentKey,
    staleTime: 1000 * 60 * 60 * 6,  // 6h — fundamentals are slow-moving
    gcTime:    1000 * 60 * 60 * 24, // 24h in-memory cache
    retry: 1,
  });
}
