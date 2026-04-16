import { useAuth } from '@/contexts/AuthContext';
import { useCallback } from 'react';

/**
 * Hook for making authenticated fetch requests
 * Automatically includes Authorization header with access token
 */
export function useAuthFetch() {
  const { accessToken } = useAuth();

  const authFetch = useCallback(
    async (url: string, options?: RequestInit): Promise<Response> => {
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        ...(options?.headers as Record<string, string>),
      };

      if (accessToken) {
        headers['Authorization'] = `Bearer ${accessToken}`;
      }

      return fetch(url, {
        ...options,
        headers,
      });
    },
    [accessToken]
  );

  return authFetch;
}
