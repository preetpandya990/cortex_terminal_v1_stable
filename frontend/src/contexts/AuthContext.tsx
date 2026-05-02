'use client';

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { setAccessToken as setAPIAccessToken } from '@/lib/api-client';
import { decodeRole, hasMinimumRole, type UserRole } from '@/lib/jwt';

// sessionStorage key written on explicit logout to prevent silent re-auth
// within the same browser tab session. Cleared on the next successful login.
const SESSION_ENDED_KEY = 'auth:session_ended';

interface AuthContextType {
  accessToken: string | null;
  isAuthenticated: boolean;
  /** True once the initial token-refresh attempt has completed (regardless of outcome).
   *  Use this alongside isAuthenticated to prevent queries firing during the boot window. */
  isAuthReady: boolean;
  isLoading: boolean;
  /** Role decoded from the JWT access token payload. Defaults to "viewer" when unauthenticated. */
  role: UserRole;
  /** Convenience: true when role === "admin". */
  isAdmin: boolean;
  login: (token: string) => void;
  logout: () => Promise<void>;
  refreshToken: () => Promise<boolean>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  // Deduplicates concurrent refresh calls (React 18 Strict Mode double-invoke,
  // parallel RSC requests). Set synchronously before the first await so that
  // any second caller immediately receives the same in-flight promise.
  const refreshInFlightRef = useRef<Promise<boolean> | null>(null);

  const refreshToken = useCallback(async (): Promise<boolean> => {
    if (refreshInFlightRef.current) {
      return refreshInFlightRef.current;
    }

    const promise = (async (): Promise<boolean> => {
      try {
        const response = await fetch('/api/auth/refresh', {
          method: 'POST',
          credentials: 'include',
        });

        if (!response.ok) {
          setAccessToken(null);
          setAPIAccessToken(null);
          return false;
        }

        const data = await response.json();
        setAccessToken(data.access_token);
        setAPIAccessToken(data.access_token);

        const refreshIn = (data.expires_in - 60) * 1000;
        if (refreshTimeoutRef.current) {
          clearTimeout(refreshTimeoutRef.current);
        }
        refreshTimeoutRef.current = setTimeout(() => refreshToken(), refreshIn);

        return true;
      } catch {
        setAccessToken(null);
        setAPIAccessToken(null);
        return false;
      } finally {
        refreshInFlightRef.current = null;
      }
    })();

    // Assigned synchronously — before the first await inside the async IIFE —
    // so any concurrent caller sees a non-null ref and joins the same promise.
    refreshInFlightRef.current = promise;
    return promise;
  }, []);

  const login = useCallback((token: string) => {
    // Clear any logout signal so a re-login in the same tab session works normally.
    try { sessionStorage.removeItem(SESSION_ENDED_KEY); } catch { /* SSR / private browsing */ }
    setAccessToken(token);
    setAPIAccessToken(token);
  }, []);

  const logout = useCallback(async () => {
    if (refreshTimeoutRef.current) {
      clearTimeout(refreshTimeoutRef.current);
      refreshTimeoutRef.current = null;
    }

    // Signal this tab that the user explicitly ended their session.
    // AuthProvider reads this on its next mount and skips silent refresh,
    // keeping the user on the login screen rather than re-authenticating.
    try { sessionStorage.setItem(SESSION_ENDED_KEY, '1'); } catch { /* SSR / private browsing */ }

    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      });
    } finally {
      // Always clear in-memory state regardless of network outcome.
      setAccessToken(null);
      setAPIAccessToken(null);
    }
  }, [accessToken]);

  useEffect(() => {
    const initAuth = async () => {
      // Honour an explicit logout that happened earlier in this tab session.
      // The flag is written by logout() and cleared here so the next
      // user-initiated login (same tab) works without interference.
      let skipSilentRefresh = false;
      try {
        if (sessionStorage.getItem(SESSION_ENDED_KEY)) {
          sessionStorage.removeItem(SESSION_ENDED_KEY);
          skipSilentRefresh = true;
        }
      } catch { /* SSR / private browsing */ }

      setIsLoading(true);
      if (!skipSilentRefresh) {
        await refreshToken();
      }
      setIsLoading(false);
    };

    initAuth();

    // api-client.ts dispatches this when a 401 occurs and silent refresh fails.
    // Clear the in-memory token so the UI immediately reflects the deauth.
    const handleAuthRequired = () => {
      setAccessToken(null);
      setAPIAccessToken(null);
    };
    window.addEventListener('auth:required', handleAuthRequired);

    return () => {
      window.removeEventListener('auth:required', handleAuthRequired);
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
    };
  }, [refreshToken]);

  const role = decodeRole(accessToken);

  const value: AuthContextType = {
    accessToken,
    isAuthenticated: !!accessToken,
    isAuthReady: !isLoading,
    isLoading,
    role,
    isAdmin: hasMinimumRole(role, 'admin'),
    login,
    logout,
    refreshToken,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
