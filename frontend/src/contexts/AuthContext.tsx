'use client';

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { setAccessToken as setAPIAccessToken } from '@/lib/api-client';
import { decodeRole, hasMinimumRole, type UserRole } from '@/lib/jwt';

const SESSION_ENDED_KEY = 'auth:session_ended';

export interface UserProfile {
  id: number;
  username: string;
  email: string;
  full_name: string | null;
  role: UserRole;
  is_active: boolean;
}

interface AuthContextType {
  accessToken: string | null;
  user: UserProfile | null;
  isAuthenticated: boolean;
  isAuthReady: boolean;
  isLoading: boolean;
  role: UserRole;
  isAdmin: boolean;
  login: (token: string) => void;
  logout: () => Promise<void>;
  refreshToken: () => Promise<boolean>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [user, setUser] = useState<UserProfile | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const refreshInFlightRef = useRef<Promise<boolean> | null>(null);

  const fetchUserProfile = useCallback(async (token: string) => {
    try {
      const response = await fetch('/api/auth/me', {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (response.ok) {
        const profile: UserProfile = await response.json();
        setUser(profile);
      }
    } catch {
      // Non-critical — auth still functions without the profile
    }
  }, []);

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
        if (refreshTimeoutRef.current) clearTimeout(refreshTimeoutRef.current);
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

    refreshInFlightRef.current = promise;
    return promise;
  }, []);

  const login = useCallback(
    (token: string) => {
      try { sessionStorage.removeItem(SESSION_ENDED_KEY); } catch { /* SSR / private browsing */ }
      setAccessToken(token);
      setAPIAccessToken(token);
    },
    [],
  );

  const logout = useCallback(async () => {
    if (refreshTimeoutRef.current) {
      clearTimeout(refreshTimeoutRef.current);
      refreshTimeoutRef.current = null;
    }

    try { sessionStorage.setItem(SESSION_ENDED_KEY, '1'); } catch { /* SSR / private browsing */ }

    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
        headers: accessToken ? { Authorization: `Bearer ${accessToken}` } : {},
      });
    } finally {
      setAccessToken(null);
      setAPIAccessToken(null);
      setUser(null);
    }
  }, [accessToken]);

  // Fetch user profile whenever we acquire (or lose) an access token
  useEffect(() => {
    if (accessToken) {
      fetchUserProfile(accessToken);
    } else {
      setUser(null);
    }
  }, [accessToken, fetchUserProfile]);

  useEffect(() => {
    const initAuth = async () => {
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

    const handleAuthRequired = () => {
      setAccessToken(null);
      setAPIAccessToken(null);
      setUser(null);
    };
    window.addEventListener('auth:required', handleAuthRequired);

    return () => {
      window.removeEventListener('auth:required', handleAuthRequired);
      if (refreshTimeoutRef.current) clearTimeout(refreshTimeoutRef.current);
    };
  }, [refreshToken]);

  const role = decodeRole(accessToken);

  const value: AuthContextType = {
    accessToken,
    user,
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
