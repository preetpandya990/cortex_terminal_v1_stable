'use client';

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from 'react';
import { setAccessToken as setAPIAccessToken } from '@/lib/api-client';

interface AuthContextType {
  accessToken: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  login: (token: string) => void;
  logout: () => Promise<void>;
  refreshToken: () => Promise<boolean>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [accessToken, setAccessToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const refreshTimeoutRef = useRef<NodeJS.Timeout | null>(null);

  const refreshToken = useCallback(async (): Promise<boolean> => {
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
      refreshTimeoutRef.current = setTimeout(() => {
        refreshToken();
      }, refreshIn);

      return true;
    } catch (error) {
      setAccessToken(null);
      setAPIAccessToken(null);
      return false;
    }
  }, []);

  const login = useCallback((token: string) => {
    setAccessToken(token);
    setAPIAccessToken(token);
  }, []);

  const logout = useCallback(async () => {
    try {
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
        refreshTimeoutRef.current = null;
      }

      await fetch('/api/auth/logout', {
        method: 'POST',
        credentials: 'include',
        headers: accessToken ? { 'Authorization': `Bearer ${accessToken}` } : {},
      });

      setAccessToken(null);
      setAPIAccessToken(null);
    } catch (error) {
      setAccessToken(null);
      setAPIAccessToken(null);
    }
  }, [accessToken]);

  useEffect(() => {
    const initAuth = async () => {
      setIsLoading(true);
      await refreshToken();
      setIsLoading(false);
    };

    initAuth();

    return () => {
      if (refreshTimeoutRef.current) {
        clearTimeout(refreshTimeoutRef.current);
      }
    };
  }, [refreshToken]);

  const value: AuthContextType = {
    accessToken,
    isAuthenticated: !!accessToken,
    isLoading,
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
