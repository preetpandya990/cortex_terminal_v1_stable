/**
 * useTypewriter Hook Tests
 * ========================
 * `requestAnimationFrame` is mocked with a deterministic, manually-flushed
 * queue (see `installFakeRaf`) instead of relying on jsdom's real timing —
 * jsdom does not implement rAF natively, and even where a shim exists, real
 * timer-driven animation frames make assertions flaky. Each test advances
 * the fake clock by an exact number of milliseconds and asserts the exact
 * resulting reveal length, so timing math is verified precisely.
 *
 * `typedCache` (the "already fully revealed this session" set) is a
 * module-level singleton, so every test uses a distinct text fixture to
 * avoid cross-test contamination.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useTypewriter, TYPED_CACHE_MAX_ENTRIES } from '@/hooks/useTypewriter';

// ── Fake requestAnimationFrame ──────────────────────────────────────────────

function installFakeRaf() {
  let now = 0;
  let nextId = 1;
  const pending = new Map<number, FrameRequestCallback>();

  window.requestAnimationFrame = ((cb: FrameRequestCallback) => {
    const id = nextId++;
    pending.set(id, cb);
    return id;
  }) as typeof window.requestAnimationFrame;

  window.cancelAnimationFrame = ((id: number) => {
    pending.delete(id);
  }) as typeof window.cancelAnimationFrame;

  return {
    /** Advances the fake clock and fires any callbacks scheduled before this call. */
    tick(ms: number) {
      now += ms;
      const due = Array.from(pending.entries());
      pending.clear();
      due.forEach(([, cb]) => cb(now));
    },
  };
}

let raf: ReturnType<typeof installFakeRaf>;
let originalRaf: typeof window.requestAnimationFrame;
let originalCancelRaf: typeof window.cancelAnimationFrame;
let originalMatchMedia: typeof window.matchMedia;

beforeEach(() => {
  originalRaf = window.requestAnimationFrame;
  originalCancelRaf = window.cancelAnimationFrame;
  originalMatchMedia = window.matchMedia;
  raf = installFakeRaf();
  // Default: motion is not reduced.
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as typeof window.matchMedia;
});

afterEach(() => {
  window.requestAnimationFrame = originalRaf;
  window.cancelAnimationFrame = originalCancelRaf;
  window.matchMedia = originalMatchMedia;
});

// ── Tests ────────────────────────────────────────────────────────────────────

describe('useTypewriter', () => {
  it('reveals characters progressively at the configured rate', () => {
    const text = 'a'.repeat(100); // unique-length fixture, not reused elsewhere
    const { result } = renderHook(() => useTypewriter(text, { charsPerSecond: 100 }));

    expect(result.current.revealedLength).toBe(0);
    expect(result.current.isComplete).toBe(false);

    // The first animation frame only establishes the starting timestamp (zero
    // elapsed time, matching real rAF semantics); subsequent frames measure
    // real elapsed time against it.
    act(() => raf.tick(0));
    act(() => raf.tick(250)); // 100 chars/sec * 0.25s = 25 chars
    expect(result.current.revealedLength).toBe(25);
    expect(result.current.isComplete).toBe(false);

    act(() => raf.tick(750)); // remaining 75 chars
    expect(result.current.revealedLength).toBe(100);
    expect(result.current.isComplete).toBe(true);
  });

  it('shows the full text immediately when disabled', () => {
    const text = 'b'.repeat(50);
    const { result } = renderHook(() => useTypewriter(text, { enabled: false }));

    expect(result.current.revealedLength).toBe(50);
    expect(result.current.isComplete).toBe(true);
  });

  it('shows the full text immediately when the user prefers reduced motion', () => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })) as typeof window.matchMedia;

    const text = 'c'.repeat(50);
    const { result } = renderHook(() => useTypewriter(text, { charsPerSecond: 100 }));

    expect(result.current.revealedLength).toBe(50);
    expect(result.current.isComplete).toBe(true);
  });

  it('renders previously fully-revealed content instantly on remount', () => {
    const text = 'd'.repeat(40);

    const first = renderHook(() => useTypewriter(text, { charsPerSecond: 1000 }));
    act(() => raf.tick(0)); // establish the starting timestamp
    act(() => raf.tick(1000)); // fully reveal and cache
    expect(first.result.current.isComplete).toBe(true);
    first.unmount();

    // Fresh hook instance, same content — should skip the animation entirely.
    const second = renderHook(() => useTypewriter(text, { charsPerSecond: 1000 }));
    expect(second.result.current.revealedLength).toBe(40);
    expect(second.result.current.isComplete).toBe(true);
  });

  it('resets to zero when the text changes to genuinely different content', () => {
    const textA = 'e'.repeat(30);
    const textB = 'f'.repeat(30); // same length, different content — not a prefix continuation

    const { result, rerender } = renderHook(({ text }) => useTypewriter(text, { charsPerSecond: 100 }), {
      initialProps: { text: textA },
    });

    act(() => raf.tick(0)); // establish the starting timestamp
    act(() => raf.tick(150)); // reveal 15 of 30 chars of textA
    expect(result.current.revealedLength).toBe(15);

    rerender({ text: textB });
    expect(result.current.revealedLength).toBe(0);
    expect(result.current.isComplete).toBe(false);

    // The text change tore down and re-established the rAF loop, so its
    // first frame again only establishes a fresh starting timestamp.
    act(() => raf.tick(0));
    act(() => raf.tick(300));
    expect(result.current.revealedLength).toBe(30);
  });

  describe('typed-cache eviction', () => {
    /** Fully reveals `text` in a throwaway hook instance, caching it. */
    function revealFully(text: string) {
      const { unmount } = renderHook(() => useTypewriter(text, { charsPerSecond: 1_000_000 }));
      act(() => raf.tick(0)); // establish the starting timestamp
      act(() => raf.tick(1000)); // more than enough to complete any fixture
      unmount();
    }

    /** True when a fresh mount of `text` renders instantly (cache hit). */
    function isCached(text: string): boolean {
      const { result, unmount } = renderHook(() => useTypewriter(text, { charsPerSecond: 1_000_000 }));
      const cached = result.current.revealedLength === text.length;
      unmount();
      return cached;
    }

    it('evicts the least-recently-revealed entry once the cap is exceeded', () => {
      const victim = 'evict-victim-' + 'v'.repeat(10);
      revealFully(victim);
      expect(isCached(victim)).toBe(true);

      // Note: isCached(victim) above re-marked it, so exactly
      // TYPED_CACHE_MAX_ENTRIES further inserts are needed to push it out.
      for (let i = 0; i < TYPED_CACHE_MAX_ENTRIES; i++) {
        revealFully(`evict-filler-${i}-` + 'w'.repeat(10));
      }

      expect(isCached(victim)).toBe(false);
    });

    it('re-revealing cached content refreshes its recency (LRU, not FIFO)', () => {
      const keeper = 'lru-keeper-' + 'k'.repeat(10);
      revealFully(keeper);

      // Fill to exactly the cap (keeper + cap-1 fillers), oldest-first = keeper.
      for (let i = 0; i < TYPED_CACHE_MAX_ENTRIES - 1; i++) {
        revealFully(`lru-filler-a-${i}-` + 'x'.repeat(10));
      }

      // Touch the keeper: instant render re-marks it as most recent.
      expect(isCached(keeper)).toBe(true);

      // One more insert exceeds the cap — with LRU it evicts the oldest
      // filler, not the freshly-touched keeper.
      revealFully('lru-filler-b-' + 'y'.repeat(10));

      expect(isCached(keeper)).toBe(true);
      expect(isCached('lru-filler-a-0-' + 'x'.repeat(10))).toBe(false);
    });
  });

  it('continues in place when the text grows as a superset of the current content (incremental streaming)', () => {
    const base = 'g'.repeat(20);
    const grown = base + 'h'.repeat(20); // extends `base` — a true streaming continuation

    const { result, rerender } = renderHook(({ text }) => useTypewriter(text, { charsPerSecond: 100 }), {
      initialProps: { text: base },
    });

    act(() => raf.tick(0)); // establish the starting timestamp
    act(() => raf.tick(100)); // reveal 10 of 20 chars
    expect(result.current.revealedLength).toBe(10);

    rerender({ text: grown });
    // Continuation: does not reset back to 0.
    expect(result.current.revealedLength).toBe(10);

    // The rAF loop was re-established for the grown text, so its first
    // frame again only establishes a fresh starting timestamp.
    act(() => raf.tick(0));
    act(() => raf.tick(300)); // reveal the remaining 30 chars (10 of base + 20 of the extension)
    expect(result.current.revealedLength).toBe(40);
    expect(result.current.isComplete).toBe(true);
  });
});
