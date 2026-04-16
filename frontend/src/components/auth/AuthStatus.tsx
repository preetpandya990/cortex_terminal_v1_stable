'use client';

import { useAuth } from '@/contexts/AuthContext';

export function AuthStatus() {
  const { isAuthenticated, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-600">
        <div className="w-2 h-2 bg-yellow-500 rounded-full animate-pulse" />
        <span>Checking authentication...</span>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 text-sm">
      <div className={`w-2 h-2 rounded-full ${isAuthenticated ? 'bg-green-500' : 'bg-red-500'}`} />
      <span className={isAuthenticated ? 'text-green-600' : 'text-red-600'}>
        {isAuthenticated ? 'Authenticated' : 'Not authenticated'}
      </span>
    </div>
  );
}
