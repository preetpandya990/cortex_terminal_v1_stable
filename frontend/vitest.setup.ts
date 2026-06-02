/**
 * Vitest global setup — runs before every test module.
 *
 * Responsibilities:
 *   1. Register @testing-library/jest-dom custom matchers.
 *   2. Auto-cleanup rendered React trees after each test.
 *   3. Provide module-level mocks for Next.js internals and
 *      canvas-backed libraries that cannot run in jsdom.
 *
 * Module mocks declared here with vi.mock() are hoisted by Vitest before
 * any import resolution in the test file, ensuring the mock takes effect
 * even for indirect transitive imports.
 */

// Extend Vitest's expect with jest-dom matchers.
// We use namespace import + explicit extend instead of '@testing-library/jest-dom/vitest'
// to avoid peer-dependency resolution ambiguity in this npm workspace:
// jest-dom is hoisted to the root node_modules which has no 'vitest' peer, so the
// /vitest entry would throw "Cannot find package 'vitest'".
// The /matchers entry exports named functions with no vitest dependency — safe to use.
import * as matchers from '@testing-library/jest-dom/matchers';
import { expect, afterEach, vi } from 'vitest';
expect.extend(matchers);

import { cleanup } from '@testing-library/react';

// ── DOM cleanup ────────────────────────────────────────────────────────────────
// Remove all rendered components from the DOM after each test.
// Prevents state bleed between tests and keeps memory usage flat.
afterEach(() => {
  cleanup();
});

// ── Next.js navigation ─────────────────────────────────────────────────────────
// next/navigation hooks are no-ops in the test environment.
// Individual test files may override these with vi.mocked(useRouter).mockReturnValue(...)
// for tests that exercise route-change behaviour.
vi.mock('next/navigation', () => ({
  useRouter: vi.fn(() => ({
    push:     vi.fn(),
    replace:  vi.fn(),
    prefetch: vi.fn(),
    back:     vi.fn(),
    forward:  vi.fn(),
    refresh:  vi.fn(),
  })),
  usePathname:    vi.fn(() => '/'),
  useSearchParams: vi.fn(() => new URLSearchParams()),
  useParams:       vi.fn(() => ({})),
}));

// ── lightweight-charts ─────────────────────────────────────────────────────────
// lightweight-charts uses the Canvas 2D API internally which is not available
// in jsdom.  Replace the entire module with a minimal stub so that chart
// components can be imported and rendered without throwing.
vi.mock('lightweight-charts', () => ({
  createChart: vi.fn(() => ({
    addSeries:    vi.fn(() => ({ setData: vi.fn(), applyOptions: vi.fn() })),
    applyOptions: vi.fn(),
    resize:       vi.fn(),
    remove:       vi.fn(),
    timeScale:    vi.fn(() => ({ fitContent: vi.fn(), applyOptions: vi.fn() })),
    subscribeCrosshairMove: vi.fn(),
    unsubscribeCrosshairMove: vi.fn(),
  })),
  ColorType:               { Solid: 'solid', VerticalGradient: 'vertical_gradient' },
  CrosshairMode:           { Normal: 0,  Hidden: 1 },
  LineStyle:               { Solid: 0,  Dashed: 1 },
  LastPriceAnimationMode:  { Disabled: 0 },
  LineType:                { Simple: 0 },
  PriceScaleMode:          { Normal: 0 },
  MismatchDirection:       { NearestLeft: -1, None: 0, NearestRight: 1 },
}));

// ── ResizeObserver ─────────────────────────────────────────────────────────────
// ResizeObserver is used by chart containers and TanStack Query DevTools.
// jsdom does not implement it — provide a no-op polyfill.
globalThis.ResizeObserver = class ResizeObserver {
  observe()   {}
  unobserve() {}
  disconnect() {}
};

// ── IntersectionObserver ───────────────────────────────────────────────────────
// Used by some lazy-loading components.  Stub to prevent test errors.
globalThis.IntersectionObserver = class IntersectionObserver {
  observe()    {}
  unobserve()  {}
  disconnect() {}
  readonly root            = null;
  readonly rootMargin      = '';
  readonly thresholds: readonly number[] = [];
  takeRecords(): IntersectionObserverEntry[] { return []; }
} as unknown as typeof IntersectionObserver;
