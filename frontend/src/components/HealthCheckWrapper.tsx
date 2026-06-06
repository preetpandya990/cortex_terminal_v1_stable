'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import type { HealthCheckResponse, HealthCheckState } from '@/types/health';

// Configuration constants
const HEALTH_CHECK_TIMEOUT = 5000; // 5 seconds client-side timeout
const INITIAL_RETRY_DELAY = 5000; // 5 seconds
const MAX_RETRY_DELAY = 60000; // 60 seconds
const MAX_CONSECUTIVE_FAILURES = 3; // Circuit breaker threshold
const SUCCESS_RESET_DELAY = 30000; // Reset failure count after 30s of success

/**
 * HealthCheckWrapper Component
 * 
 * Production-ready health monitoring with:
 * - Exponential backoff retry strategy
 * - Circuit breaker pattern to prevent cascade failures
 * - Graceful degradation (app remains functional during backend outages)
 * - User-dismissible alerts
 * - Automatic recovery detection
 * 
 * Architecture:
 * - Calls Next.js /api/health endpoint (not direct backend)
 * - Next.js API route proxies to FastAPI backend
 * - Client never directly accesses backend (security + CORS)
 */
export function HealthCheckWrapper({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<HealthCheckState>({
    status: 'healthy',
    lastCheckedAt: null,
    lastSuccessAt: null,
    consecutiveFailures: 0,
    isChecking: false,
  });

  const [dismissed, setDismissed] = useState(false);
  const retryTimerRef = useRef<NodeJS.Timeout | null>(null);
  const successResetTimerRef = useRef<NodeJS.Timeout | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  /**
   * Calculate retry delay using exponential backoff
   * Formula: min(INITIAL_DELAY * 2^failures, MAX_DELAY)
   */
  const getRetryDelay = useCallback((failures: number): number => {
    const delay = INITIAL_RETRY_DELAY * Math.pow(2, failures);
    return Math.min(delay, MAX_RETRY_DELAY);
  }, []);

  /**
   * Perform health check against Next.js API route
   */
  const runHealthCheck = useCallback(async (): Promise<void> => {
    // Cancel any in-flight request
    if (abortControllerRef.current) {
      abortControllerRef.current.abort(new DOMException('New health check started', 'AbortError'));
    }

    // Create new abort controller for this request
    const controller = new AbortController();
    abortControllerRef.current = controller;

    setState((prev) => ({ ...prev, isChecking: true }));

    try {
      const response = await fetch('/api/health', {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
        signal: controller.signal,
        cache: 'no-store',
      });

      const data: HealthCheckResponse = await response.json();
      const now = new Date();

      if (response.ok && data.status === 'healthy') {
        // Success - reset failure counter
        setState({
          status: 'healthy',
          lastCheckedAt: now,
          lastSuccessAt: now,
          consecutiveFailures: 0,
          isChecking: false,
        });

        // Clear any pending retry
        if (retryTimerRef.current) {
          clearTimeout(retryTimerRef.current);
          retryTimerRef.current = null;
        }

        // Reset dismissed state on recovery
        setDismissed(false);

        // Schedule next check after success period
        if (successResetTimerRef.current) {
          clearTimeout(successResetTimerRef.current);
        }
        successResetTimerRef.current = setTimeout(() => {
          runHealthCheck();
        }, SUCCESS_RESET_DELAY);
      } else {
        // Degraded or error state
        setState((prev) => ({
          status: data.status === 'degraded' ? 'degraded' : 'error',
          lastCheckedAt: now,
          lastSuccessAt: prev.lastSuccessAt,
          consecutiveFailures: prev.consecutiveFailures + 1,
          isChecking: false,
        }));
      }
    } catch (error) {
      // Network error, timeout, or aborted request.
      // DOMException (name='AbortError') is thrown when the signal is aborted —
      // check by name rather than instanceof to handle all browser environments.
      if ((error as { name?: string })?.name === 'AbortError') {
        return;
      }

      const now = new Date();
      setState((prev) => ({
        status: 'error',
        lastCheckedAt: now,
        lastSuccessAt: prev.lastSuccessAt,
        consecutiveFailures: prev.consecutiveFailures + 1,
        isChecking: false,
      }));
    }
  }, []);

  /**
   * Schedule retry with exponential backoff
   */
  const scheduleRetry = useCallback(() => {
    if (retryTimerRef.current) return; // Already scheduled

    const delay = getRetryDelay(state.consecutiveFailures);

    retryTimerRef.current = setTimeout(() => {
      retryTimerRef.current = null;
      runHealthCheck();
    }, delay);
  }, [state.consecutiveFailures, getRetryDelay, runHealthCheck]);

  /**
   * Initial health check on mount
   */
  useEffect(() => {
    runHealthCheck();

    return () => {
      // Cleanup on unmount
      if (retryTimerRef.current) {
        clearTimeout(retryTimerRef.current);
      }
      if (successResetTimerRef.current) {
        clearTimeout(successResetTimerRef.current);
      }
      // Do not call abort() on unmount — Turbopack's global error interceptor
      // surfaces AbortErrors before the local catch can suppress them, even
      // when the error is handled correctly.  React 18 silently ignores setState
      // on unmounted components, so the in-flight request completing is harmless.
      // Nulling the ref prevents runHealthCheck from aborting a stale controller.
      abortControllerRef.current = null;
    };
  }, [runHealthCheck]);

  /**
   * Auto-retry on failure (with circuit breaker)
   */
  useEffect(() => {
    if (state.status === 'healthy' || state.isChecking) return;

    // Circuit breaker: stop retrying after max failures
    if (state.consecutiveFailures >= MAX_CONSECUTIVE_FAILURES) {
      console.warn(
        `[Health Check] Circuit breaker open: ${state.consecutiveFailures} consecutive failures. Manual retry required.`
      );
      return;
    }

    scheduleRetry();
  }, [state.status, state.consecutiveFailures, state.isChecking, scheduleRetry]);

  /**
   * Manual retry handler
   */
  const handleManualRetry = useCallback(() => {
    // Clear any scheduled retry
    if (retryTimerRef.current) {
      clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }

    // Reset circuit breaker on manual retry
    setState((prev) => ({
      ...prev,
      consecutiveFailures: 0,
    }));

    runHealthCheck();
  }, [runHealthCheck]);

  /**
   * Dismiss banner handler
   */
  const handleDismiss = useCallback(() => {
    setDismissed(true);
  }, []);

  // Determine if banner should be shown
  const showBanner = (state.status === 'degraded' || state.status === 'error') && !dismissed;

  // Circuit breaker is open
  const circuitBreakerOpen = state.consecutiveFailures >= MAX_CONSECUTIVE_FAILURES;

  return (
    <>
      {showBanner && (
        <div
          role="alert"
          aria-live="polite"
          aria-atomic="true"
          className="sticky top-0 z-50 border-b bg-red-50 border-red-200"
        >
          <div className="container mx-auto px-4 py-3">
            <div className="flex items-start justify-between gap-4">
              <div className="flex-1 space-y-1">
                <div className="flex items-center gap-2">
                  <svg
                    className="h-5 w-5 text-red-600"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    aria-hidden="true"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                    />
                  </svg>
                  <strong className="text-sm font-semibold text-red-900">
                    {circuitBreakerOpen ? 'Backend Connection Lost' : 'Backend Degraded'}
                  </strong>
                </div>
                <p className="text-sm text-red-700">
                  {circuitBreakerOpen
                    ? 'Multiple connection attempts failed. Live data and trading features are unavailable. Please check your connection and try again.'
                    : 'Live data and trading features may be delayed or unavailable.'}
                  {state.lastCheckedAt && (
                    <span className="ml-1">
                      Last checked {state.lastCheckedAt.toLocaleTimeString()}.
                    </span>
                  )}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleManualRetry}
                  disabled={state.isChecking}
                  className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-700 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  aria-label="Retry connection to backend"
                >
                  {state.isChecking ? 'Checking...' : 'Retry'}
                </button>
                <button
                  type="button"
                  onClick={handleDismiss}
                  className="rounded-md border border-red-300 bg-white px-3 py-1.5 text-sm font-medium text-red-700 hover:bg-red-50 focus:outline-none focus:ring-2 focus:ring-red-500 focus:ring-offset-2 transition-colors"
                  aria-label="Dismiss health check warning"
                >
                  Dismiss
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
      {children}
    </>
  );
}
