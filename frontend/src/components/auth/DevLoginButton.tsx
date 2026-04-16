'use client';

import { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';

export function DevLoginButton() {
  const { login, isAuthenticated, logout } = useAuth();
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleDevLogin = async () => {
    setIsLoading(true);
    setError(null);

    try {
      const response = await fetch('/api/auth/dev-login', {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) {
        const data = await response.json();
        throw new Error(data.error || 'Login failed');
      }

      const data = await response.json();
      login(data.access_token);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed');
      console.error('[DevLogin] Error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleLogout = async () => {
    setIsLoading(true);
    try {
      await logout();
    } catch (err) {
      console.error('[DevLogin] Logout error:', err);
    } finally {
      setIsLoading(false);
    }
  };

  if (isAuthenticated) {
    return (
      <button
        onClick={handleLogout}
        disabled={isLoading}
        className="px-4 py-2 bg-red-600 text-white rounded hover:bg-red-700 disabled:opacity-50"
      >
        {isLoading ? 'Logging out...' : 'Logout'}
      </button>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      <button
        onClick={handleDevLogin}
        disabled={isLoading}
        className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 disabled:opacity-50"
      >
        {isLoading ? 'Logging in...' : 'Dev Login'}
      </button>
      {error && (
        <p className="text-sm text-red-600">{error}</p>
      )}
    </div>
  );
}
