/**
 * useAnalysisStream hook tests (WS8)
 * ==================================
 * The connection lifecycle bugs this suite pins:
 *  - the silent ~29-min token refresh used to recreate `connect`, wiping all
 *    four cards and tearing down the live EventSource;
 *  - reconnects gave up permanently after 3 attempts (~15s);
 *  - a single shared connected flag hid per-component refresher failures.
 *
 * EventSource is replaced with a scripted fake; timers are faked so backoff
 * and the freshness horizon are asserted exactly.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useAnalysisStream } from '@/components/AnalysisCardsSection';

// ── Fake EventSource ─────────────────────────────────────────────────────────

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  private listeners = new Map<string, ((e: MessageEvent) => void)[]>();

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type: string, cb: (e: MessageEvent) => void) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), cb]);
  }
  close() { this.closed = true; }

  // Test drivers
  open() { this.onopen?.(); }
  error() { this.onerror?.(); }
  emit(type: string, data: unknown) {
    for (const cb of this.listeners.get(type) ?? []) {
      cb({ data: JSON.stringify(data) } as MessageEvent);
    }
  }
}

function lastES(): FakeEventSource {
  return FakeEventSource.instances[FakeEventSource.instances.length - 1];
}

const UPDATE = {
  prediction: { available: true, direction: 'BUY', updated_at: '2026-07-06T10:00:00Z' },
  pattern: null,
  sentiment: null,
  explanation: null,
  instrument_key: 'NSE_EQ|X',
};

beforeEach(() => {
  vi.useFakeTimers();
  FakeEventSource.instances = [];
  vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('useAnalysisStream', () => {
  it('survives a silent token refresh without reconnecting or wiping state', () => {
    const { result, rerender } = renderHook(
      ({ token }) => useAnalysisStream('NSE_EQ|X', 'X', token, true),
      { initialProps: { token: 'token-A' } },
    );
    expect(FakeEventSource.instances).toHaveLength(1);

    act(() => { lastES().open(); lastES().emit('analysis_update', UPDATE); });
    expect(result.current.predictionData).not.toBeNull();

    // The ~29-min silent refresh: new access token, same instrument.
    rerender({ token: 'token-B' });

    expect(FakeEventSource.instances).toHaveLength(1);           // no reconnect
    expect(lastES().closed).toBe(false);                          // stream alive
    expect(result.current.predictionData).not.toBeNull();         // no wipe

    // A later reconnect must present the FRESH token.
    act(() => { lastES().error(); vi.advanceTimersByTime(5_000); });
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(lastES().url).toContain('token-B');
  });

  it('reconnects with exponential backoff and never gives up', () => {
    renderHook(() => useAnalysisStream('NSE_EQ|X', 'X', 't', true));
    expect(FakeEventSource.instances).toHaveLength(1);

    // 5s → 10s → 20s → 40s → 60s → 60s (cap), one new connection each.
    const delays = [5_000, 10_000, 20_000, 40_000, 60_000, 60_000];
    for (const [i, delay] of delays.entries()) {
      act(() => { lastES().error(); });
      act(() => { vi.advanceTimersByTime(delay - 1); });
      expect(FakeEventSource.instances).toHaveLength(i + 1);      // not yet
      act(() => { vi.advanceTimersByTime(1); });
      expect(FakeEventSource.instances).toHaveLength(i + 2);      // reconnected
    }

    // A successful open resets the backoff to the base delay.
    act(() => { lastES().open(); });
    act(() => { lastES().error(); vi.advanceTimersByTime(5_000); });
    expect(FakeEventSource.instances).toHaveLength(delays.length + 2);
  });

  it('changing instrument reconnects and resets state', () => {
    const { result, rerender } = renderHook(
      ({ key }) => useAnalysisStream(key, 'X', 't', true),
      { initialProps: { key: 'NSE_EQ|A' } },
    );
    act(() => { lastES().open(); lastES().emit('analysis_update', UPDATE); });
    expect(result.current.predictionData).not.toBeNull();

    rerender({ key: 'NSE_EQ|B' });
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[0].closed).toBe(true);
    expect(result.current.predictionData).toBeNull();
  });

  it('marks a component degraded on analysis_error and clears it on a fresh slice', () => {
    const { result } = renderHook(() => useAnalysisStream('NSE_EQ|X', 'X', 't', true));
    act(() => { lastES().open(); lastES().emit('analysis_update', UPDATE); });
    expect(result.current.isComponentLive('prediction')).toBe(true);

    act(() => {
      lastES().emit('analysis_error', {
        component: 'prediction', message: 'down', stale: true,
      });
    });
    expect(result.current.isComponentLive('prediction')).toBe(false);   // re-arms polling
    expect(result.current.isComponentLive('sentiment')).toBe(true);     // others untouched
    expect(result.current.degradedComponents).toEqual(['prediction']);

    // A genuinely fresher slice (updated_at after the degradation) clears it.
    act(() => {
      lastES().emit('analysis_update', {
        ...UPDATE,
        prediction: { available: true, direction: 'BUY', updated_at: new Date().toISOString() },
      });
    });
    expect(result.current.isComponentLive('prediction')).toBe(true);
    expect(result.current.degradedComponents).toEqual([]);
  });

  it('a connected-but-silent stream goes stale after the horizon', () => {
    const { result } = renderHook(() => useAnalysisStream('NSE_EQ|X', 'X', 't', true));
    act(() => { lastES().open(); lastES().emit('analysis_update', UPDATE); });
    expect(result.current.isComponentLive('prediction')).toBe(true);

    // 180s of silence (freshness tick fires every 30s and forces re-evaluation).
    act(() => { vi.advanceTimersByTime(181_000); });
    expect(result.current.isComponentLive('prediction')).toBe(false);
  });

  it('connected flips on open, before any data arrives', () => {
    const { result } = renderHook(() => useAnalysisStream('NSE_EQ|X', 'X', 't', true));
    expect(result.current.isConnected).toBe(false);
    act(() => { lastES().open(); });
    expect(result.current.isConnected).toBe(true);
  });
});
