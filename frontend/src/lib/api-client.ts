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
  // If refresh is already in progress, wait for it
  if (tokenRefreshPromise) {
    return tokenRefreshPromise;
  }

  tokenRefreshPromise = (async () => {
    try {
      const response = await fetch('/api/auth/refresh', {
        method: 'POST',
        credentials: 'include',
      });

      if (!response.ok) {
        console.error('[API Client] Token refresh failed:', response.status);
        accessToken = null;
        return null;
      }

      const data = await response.json();
      accessToken = data.access_token;
      console.log('[API Client] Token refreshed successfully');
      return accessToken;
    } catch (error) {
      console.error('[API Client] Token refresh error:', error);
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

/**
 * Request interceptor - inject auth token
 */
api.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // Inject access token if available
    if (accessToken && !config.headers.Authorization) {
      config.headers.Authorization = `Bearer ${accessToken}`;
    }

    // Log request
    console.log('[API Request]', {
      method: config.method?.toUpperCase(),
      url: config.url,
      params: config.params,
    });

    return config;
  },
  (error) => {
    console.error('[API Request Error]', error);
    return Promise.reject(error);
  }
);

/**
 * Response interceptor - handle 401 and retry with refresh
 */
api.interceptors.response.use(
  (response) => {
    // Log successful response
    console.log('[API Response]', {
      method: response.config.method?.toUpperCase(),
      url: response.config.url,
      status: response.status,
    });
    return response;
  },
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & { _retry?: boolean };

    // Log error
    console.error('[API Error]', {
      method: originalRequest?.method?.toUpperCase(),
      url: originalRequest?.url,
      status: error.response?.status,
      message: error.message,
    });

    // Handle 401 Unauthorized - try to refresh token
    if (error.response?.status === 401 && originalRequest && !originalRequest._retry) {
      originalRequest._retry = true;

      console.log('[API Client] 401 detected, attempting token refresh...');

      const newToken = await refreshAccessToken();

      if (newToken) {
        // Retry original request with new token
        originalRequest.headers.Authorization = `Bearer ${newToken}`;
        return api(originalRequest);
      } else {
        // Refresh failed - redirect to login or emit event
        console.error('[API Client] Token refresh failed, user needs to re-authenticate');
        // Emit custom event for auth failure
        window.dispatchEvent(new CustomEvent('auth:required'));
      }
    }

    return Promise.reject(error);
  }
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
