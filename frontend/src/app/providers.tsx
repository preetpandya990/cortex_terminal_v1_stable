'use client';

/**
 * Cortex Terminal V1 - Application Providers
 *
 * Client-side providers for React Query, Authentication, and other context providers.
 * This component wraps the application to provide global state management.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, useEffect } from 'react';
import { HealthCheckWrapper } from '@/components/HealthCheckWrapper';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { setAccessToken } from '@/lib/api-client';

/**
 * Sync auth token with API client
 */
function AuthSync({ children }: { children: React.ReactNode }) {
  const { accessToken } = useAuth();

  useEffect(() => {
    setAccessToken(accessToken);
  }, [accessToken]);

  return <>{children}</>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000, // 1 minute - data considered fresh
            refetchOnWindowFocus: false, // Disable refetch on window focus for trading app stability
            retry: 2, // Retry failed requests twice
          },
          mutations: {
            retry: 1, // Retry failed mutations once
          },
        },
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <AuthSync>
          <HealthCheckWrapper>{children}</HealthCheckWrapper>
        </AuthSync>
      </AuthProvider>
    </QueryClientProvider>
  );
}
