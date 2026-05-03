'use client';

import { useAuth } from '@/contexts/AuthContext';

/**
 * Compact authentication status indicator.
 * Used in contexts where a minimal signal is needed (e.g. status bars, diagnostics).
 * Full user display lives in AppHeader.
 */
export function AuthStatus() {
  const { isAuthenticated, isLoading, user } = useAuth();

  if (isLoading) {
    return (
      <div className="flex items-center gap-1.5 text-xs text-slate-500">
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-amber-400" />
        Connecting…
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5 text-xs">
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          isAuthenticated ? 'bg-emerald-500' : 'bg-red-500'
        }`}
      />
      <span className={isAuthenticated ? 'text-emerald-700' : 'text-red-600'}>
        {isAuthenticated ? (user?.username ?? 'Authenticated') : 'Unauthenticated'}
      </span>
    </div>
  );
}
