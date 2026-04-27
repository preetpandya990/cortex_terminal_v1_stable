import axios, { AxiosError, AxiosInstance, InternalAxiosRequestConfig } from 'axios';

/**
 * Production-grade API client with:
 * - Automatic token injection
 * - Silent token refresh on 401
 * - Request/response logging
 * - Error handling
 */

// Use Next.js API routes as BFF proxy
export const API_BASE_URL = '/api/v1';

let accessToken: string | null = null;
let tokenRefreshPromise: Promise<string | null> | null = null;

/**
 * Set access token for API requests
 */
export function setAccessToken(token: string | null) {
  accessToken = token;
}

/**
 * Get current access token
 */
export function getAccessToken(): string | null {
  return accessToken;
}

/**
 * Refresh access token
 */
async function refreshAccessToken(): Promise<string | null> {
  if (tokenRefreshPromise) return tokenRefreshPromise;

  tokenRefreshPromise = (async () => {
    try {
      const response = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) {
        accessToken = null;
        return null;
      }

      const data = await response.json();
      accessToken = data.access_token;
      return accessToken;
    } catch {
      accessToken = null;
      return null;
    } finally {
      tokenRefreshPromise = null;
    }
  })();

  return tokenRefreshPromise;
}

/**
 * Create axios instance
 */
export const api: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  withCredentials: true, // Include cookies
});

// Inject the bearer token on every outgoing request.
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    if (accessToken && !config.headers.Authorization) {
      config.headers.Authorization = `Bearer ${accessToken}`;
    }
    return config;
  },
  (error) => Promise.reject(error),
);

// Handle 401s with silent token refresh; let all other errors pass through
// to callers without logging (api.ts already handles error logging).
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    if (error.response?.status === 401 && originalRequest && !originalRequest._retry) {
      originalRequest._retry = true;

      const newToken = await refreshAccessToken();

      if (newToken) {
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return api(originalRequest);
      }

      // Refresh failed — notify the app so auth state can be cleared.
      console.error('[API Client] Session expired — re-authentication required');
      window.dispatchEvent(new CustomEvent('auth:required'));
    }

    return Promise.reject(error);
  },
);

/**
 * API error class
 */
export class APIError extends Error {
  statusCode?: number;
  errorCode?: string;
  detail?: string;

  constructor(message: string, options?: { statusCode?: number; errorCode?: string; detail?: string }) {
    super(message);
    this.name = 'APIError';
    this.statusCode = options?.statusCode;
    this.errorCode = options?.errorCode;
    this.detail = options?.detail;
  }
}

/**
 * Convert axios error to APIError
 */
export function toAPIError(error: unknown, fallbackMessage: string): APIError {
  if (error instanceof AxiosError) {
    const detail = error.response?.data?.detail || error.response?.data?.message;
    return new APIError(detail || error.message || fallbackMessage, {
      statusCode: error.response?.status,
      errorCode: error.code,
      detail,
    });
  }

  if (error instanceof Error) {
    return new APIError(error.message);
  }

  return new APIError(fallbackMessage);
}

/**
 * Helper to make typed requests
 */
export async function requestData<T>(
  request: Promise<any>,
  fallbackMessage: string
): Promise<T> {
  try {
    const response = await request;
    return response.data;
  } catch (error) {
    throw toAPIError(error, fallbackMessage);
  }
}
