'use client';

/**
 * Cortex Terminal V1 - Application Providers
 *
 * Client-side providers for React Query, Authentication, Toast Notifications, and Error Boundaries.
 * This component wraps the application to provide global state management.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState, useEffect } from 'react';
import { HealthCheckWrapper } from '@/components/HealthCheckWrapper';
import { AuthProvider, useAuth } from '@/contexts/AuthContext';
import { ChartPreferencesProvider } from '@/contexts/ChartPreferencesContext';
import { WatchlistProvider } from '@/contexts/WatchlistContext';
import { ToastProvider } from '@/components/ui/toast';
import { ErrorBoundary } from '@/components/ui/error-boundary';
import { setAccessToken } from '@/lib/api-client';
import { setAuthToken } from '@/lib/api';

/**
 * Sync auth token with both API clients.
 * api-client.ts (BFF proxy) and api.ts (direct backend) are separate axios
 * instances — both must receive the token whenever auth state changes.
 */
function AuthSync({ children }: { children: React.ReactNode }) {
  const { accessToken } = useAuth();

  useEffect(() => {
    setAccessToken(accessToken);   // api-client.ts instance
    setAuthToken(accessToken);     // api.ts instance (used by upstoxAPI, mlAPI, etc.)
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
    <ErrorBoundary
      onError={(error, errorInfo) => {
        // Log to monitoring service (Sentry, DataDog, etc.)
        console.error('Application Error:', error, errorInfo);
        // TODO: Send to error monitoring service
      }}
    >
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <WatchlistProvider>
          <ChartPreferencesProvider>
            <ToastProvider>
              <AuthSync>
                <HealthCheckWrapper>{children}</HealthCheckWrapper>
              </AuthSync>
            </ToastProvider>
          </ChartPreferencesProvider>
          </WatchlistProvider>
        </AuthProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
