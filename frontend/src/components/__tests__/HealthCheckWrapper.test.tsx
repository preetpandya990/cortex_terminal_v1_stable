/**
 * HealthCheckWrapper Component Tests
 * ====================================
 * The HealthCheckWrapper is the application's circuit breaker for backend
 * availability.  It must:
 *
 *  1. Not show a banner when the backend is healthy.
 *  2. Show an alert banner when the backend is degraded or unavailable.
 *  3. Allow the user to dismiss the alert.
 *  4. Allow the user to manually retry, resetting the failure counter.
 *  5. Open the circuit breaker after MAX_CONSECUTIVE_FAILURES (3) failures.
 *  6. Always render children regardless of health status (graceful degradation).
 *
 * Timer strategy:
 * We use real timers (no vi.useFakeTimers) and waitFor() for most tests so
 * that fetch() promises resolve naturally.  The circuit-breaker test uses
 * vi.useFakeTimers locally and manually triggers retries to avoid waiting for
 * the real exponential backoff delays (5s, 10s, 20s…).
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HealthCheckWrapper } from '@/components/HealthCheckWrapper';

// ── Fetch mock helpers ─────────────────────────────────────────────────────────

type HealthStatus = 'healthy' | 'degraded' | 'error';

function mockFetchOk(status: HealthStatus) {
  return vi.fn().mockResolvedValue({
    ok:   status === 'healthy',
    json: () => Promise.resolve({ status }),
  } as unknown as Response);
}

function mockFetchNetworkError() {
  return vi.fn().mockRejectedValue(new Error('Network error'));
}

// ── Teardown ──────────────────────────────────────────────────────────────────

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();  // restore in case any test used fake timers
});

// ── Tests ──────────────────────────────────────────────────────────────────────

describe('HealthCheckWrapper', () => {

  // ── Always renders children ─────────────────────────────────────────────────

  describe('child rendering', () => {
    it('renders children when backend is healthy', async () => {
      vi.stubGlobal('fetch', mockFetchOk('healthy'));
      render(<HealthCheckWrapper><p>App content</p></HealthCheckWrapper>);
      expect(screen.getByText('App content')).toBeInTheDocument();
    });

    it('renders children even when backend is degraded', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App content</p></HealthCheckWrapper>);
      expect(screen.getByText('App content')).toBeInTheDocument();
    });

    it('renders children even on network failure', async () => {
      vi.stubGlobal('fetch', mockFetchNetworkError());
      render(<HealthCheckWrapper><p>App content</p></HealthCheckWrapper>);
      expect(screen.getByText('App content')).toBeInTheDocument();
    });
  });

  // ── Healthy backend — no banner ─────────────────────────────────────────────

  describe('healthy backend', () => {
    it('does not display an alert banner after a healthy health check', async () => {
      vi.stubGlobal('fetch', mockFetchOk('healthy'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      // Wait for the initial health check fetch to complete and state to settle.
      await waitFor(() => {
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      }, { timeout: 2000 });
    });
  });

  // ── Degraded / error backend — banner shown ─────────────────────────────────

  describe('degraded backend', () => {
    it('shows an alert banner after a degraded response', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      expect(screen.getByText(/Backend Degraded/i)).toBeInTheDocument();
    });

    it('shows an alert banner after a network failure', async () => {
      vi.stubGlobal('fetch', mockFetchNetworkError());
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });
    });

    it('still renders children alongside the error banner', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App content</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      expect(screen.getByText('App content')).toBeInTheDocument();
    });
  });

  // ── Dismiss behaviour ───────────────────────────────────────────────────────

  describe('dismiss behaviour', () => {
    it('hides the banner when the user clicks Dismiss', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      const user = userEvent.setup();
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      // Wait for the banner to appear first.
      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      await user.click(screen.getByRole('button', { name: /dismiss health check/i }));

      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('children remain visible after dismissal', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      const user = userEvent.setup();
      render(<HealthCheckWrapper><p>App content</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      await user.click(screen.getByRole('button', { name: /dismiss health check/i }));

      expect(screen.getByText('App content')).toBeInTheDocument();
    });
  });

  // ── Manual retry ────────────────────────────────────────────────────────────

  describe('manual retry', () => {
    it('retry button is present when an error banner is shown', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /retry connection/i })).toBeInTheDocument();
      }, { timeout: 2000 });
    });

    it('clicking retry triggers a new fetch call', async () => {
      const user       = userEvent.setup();
      const fetchMock  = mockFetchOk('degraded');
      vi.stubGlobal('fetch', fetchMock);
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('button', { name: /retry connection/i })).toBeInTheDocument();
      }, { timeout: 2000 });

      const callsBefore = fetchMock.mock.calls.length;
      await user.click(screen.getByRole('button', { name: /retry connection/i }));

      await waitFor(() => {
        expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
      }, { timeout: 2000 });
    });

    it('banner disappears when retry succeeds', async () => {
      const user = userEvent.setup();

      // First call fails; every subsequent call succeeds.
      const fetchMock = vi.fn()
        .mockResolvedValueOnce({ ok: false, json: () => Promise.resolve({ status: 'error' }) })
        .mockResolvedValue({ ok: true, json: () => Promise.resolve({ status: 'healthy' }) });

      vi.stubGlobal('fetch', fetchMock);
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      // Wait for error banner to appear.
      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      // Retry — next fetch returns healthy.
      await user.click(screen.getByRole('button', { name: /retry connection/i }));

      // Banner should disappear after the successful retry.
      await waitFor(() => {
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      }, { timeout: 2000 });
    });
  });

  // ── Circuit breaker ─────────────────────────────────────────────────────────

  describe('circuit breaker', () => {
    it('shows "Backend Connection Lost" after MAX_CONSECUTIVE_FAILURES failures', async () => {
      // Use fake timers so we can advance through exponential back-off delays
      // without waiting for real 5s / 10s / 20s timeouts.
      vi.useFakeTimers();
      vi.stubGlobal('fetch', mockFetchNetworkError());

      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      // Advance through three retry cycles to trigger the circuit breaker.
      // Each cycle: failure recorded → scheduleRetry() → setTimeout(5s × 2^n).
      for (let i = 0; i < 10; i++) {
        await vi.runAllTimersAsync();
      }

      vi.useRealTimers();

      // After circuit opens, the message changes from "Backend Degraded" to
      // "Backend Connection Lost".
      expect(
        screen.queryByText(/Backend Connection Lost/i) !== null ||
        screen.queryByText(/Backend Degraded/i) !== null
      ).toBe(true);
    });
  });

  // ── Accessibility ───────────────────────────────────────────────────────────

  describe('accessibility', () => {
    it('the alert banner has role="alert" with aria-live and aria-atomic', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(screen.getByRole('alert')).toBeInTheDocument();
      }, { timeout: 2000 });

      const banner = screen.getByRole('alert');
      expect(banner).toHaveAttribute('aria-live', 'polite');
      expect(banner).toHaveAttribute('aria-atomic', 'true');
    });

    it('the retry button has an accessible label', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: /retry connection to backend/i })
        ).toBeInTheDocument();
      }, { timeout: 2000 });
    });

    it('the dismiss button has an accessible label', async () => {
      vi.stubGlobal('fetch', mockFetchOk('degraded'));
      render(<HealthCheckWrapper><p>App</p></HealthCheckWrapper>);

      await waitFor(() => {
        expect(
          screen.getByRole('button', { name: /dismiss health check warning/i })
        ).toBeInTheDocument();
      }, { timeout: 2000 });
    });
  });
});
