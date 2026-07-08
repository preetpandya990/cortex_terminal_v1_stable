/**
 * AIExplanationPanel Component Tests
 * ===================================
 * Verifies the typewriter integration: narrative text reveals progressively
 * via requestAnimationFrame, sources/disclaimer are held back until typing
 * completes, and the panel still renders its other states (skeleton, weak
 * signal, failed) untouched by that change.
 *
 * requestAnimationFrame is mocked with a deterministic, manually-flushed
 * queue — see useTypewriter.test.ts for the rationale (jsdom has no native
 * rAF, and real timer-driven frames make assertions flaky).
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { AIExplanationPanel } from '@/components/AIExplanationPanel';
import type { ExplanationData } from '@/types/analysis';

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

beforeEach(() => {
  originalRaf = window.requestAnimationFrame;
  originalCancelRaf = window.cancelAnimationFrame;
  raf = installFakeRaf();
});

afterEach(() => {
  window.requestAnimationFrame = originalRaf;
  window.cancelAnimationFrame = originalCancelRaf;
});

function makeData(overrides: Partial<ExplanationData> = {}): ExplanationData {
  return {
    available: true,
    summary: 'Short summary.',
    full_explanation:
      '### What the models saw\n' +
      'Momentum and volume both confirm the breakout thesis for this name.\n\n' +
      '### Key risks\n' +
      'Earnings are due in three sessions, which could invalidate the setup.',
    model: 'gemini-2.5-flash',
    generated_at: '2026-07-03T10:00:00Z',
    sources: [{ source_name: 'Reuters', as_of: '2026-07-03T09:00:00Z', source_url: 'https://example.com' }],
    context_type: 'suggestion_explanation',
    signal_direction: null,
    signal_generated_at: null,
    ...overrides,
  };
}

describe('AIExplanationPanel — typewriter integration', () => {
  it('reveals the narrative progressively and holds back sources/disclaimer until complete', () => {
    const data = makeData({
      full_explanation:
        '### What the models saw\n' +
        'a'.repeat(60) +
        '\n\n### Key risks\n' +
        'b'.repeat(60),
    });

    render(<AIExplanationPanel data={data} isLoading={false} />);

    // First heading is present immediately; body text starts empty.
    expect(screen.getByText('What the models saw')).toBeInTheDocument();
    expect(screen.queryByText('Key risks')).not.toBeInTheDocument();
    expect(screen.queryByText('Sources')).not.toBeInTheDocument();

    // Advance the animation partway through the first section.
    act(() => raf.tick(0));
    act(() => raf.tick(50));
    const partial = screen.getByText(/^a+$/);
    expect(partial.textContent!.length).toBeGreaterThan(0);
    expect(partial.textContent!.length).toBeLessThan(60);

    // Run the animation to completion (well past the total character count
    // at the default reveal rate).
    act(() => raf.tick(5000));

    expect(screen.getByText('Key risks')).toBeInTheDocument();
    expect(screen.getByText('a'.repeat(60))).toBeInTheDocument();
    expect(screen.getByText('b'.repeat(60))).toBeInTheDocument();
    expect(screen.getByText('Sources')).toBeInTheDocument();
    expect(screen.getByText('Reuters')).toBeInTheDocument();
  });

  it('renders legacy (non-sectioned) explanations as a single block', () => {
    const data = makeData({ full_explanation: 'Plain narrative with no headers at all.' });
    render(<AIExplanationPanel data={data} isLoading={false} />);

    act(() => raf.tick(0));
    act(() => raf.tick(5000));

    expect(screen.getByText('Plain narrative with no headers at all.')).toBeInTheDocument();
  });

  it('shows the skeleton while no explanation text has arrived', () => {
    render(
      <AIExplanationPanel
        data={{ ...makeData(), available: false, full_explanation: null }}
        isLoading={false}
      />,
    );
    expect(screen.getByRole('status', { name: /generating explanation/i })).toBeInTheDocument();
  });

  it('shows the weak-signal CTA and never mounts the typewriter for it', () => {
    render(
      <AIExplanationPanel
        data={{ ...makeData(), weak_signal: true, consensus_score: 42, full_explanation: null }}
        isLoading={false}
      />,
    );
    expect(screen.getByText(/confidence below AI analysis threshold/i)).toBeInTheDocument();
  });

  it('shows the failed state for a permanently failed explanation', () => {
    render(<AIExplanationPanel data={{ ...makeData(), failed: true }} isLoading={false} />);
    expect(screen.getByText('Analysis unavailable for this signal.')).toBeInTheDocument();
  });
});

// ── WS8 additions ─────────────────────────────────────────────────────────────

describe('AIExplanationPanel — source URL sanitization (WS8)', () => {
  it('renders a javascript: source_url as plain text, never an href', () => {
    const data = makeData({
      sources: [
        {
          source_name: 'Malicious Feed',
          as_of: '2026-07-03T09:00:00Z',
          source_url: 'javascript:alert(1)',
        },
      ],
    });
    render(<AIExplanationPanel data={data} isLoading={false} />);
    act(() => raf.tick(0));
    act(() => raf.tick(60_000)); // finish typing so SourcesList mounts

    expect(screen.getByText('Malicious Feed')).toBeInTheDocument();
    expect(
      screen.queryByLabelText('Open source: Malicious Feed'),
    ).not.toBeInTheDocument(); // no anchor rendered
  });

  it('renders an https source_url as a link', () => {
    render(<AIExplanationPanel data={makeData()} isLoading={false} />);
    act(() => raf.tick(0));
    act(() => raf.tick(60_000));

    const link = screen.getByLabelText('Open source: Reuters');
    expect(link).toHaveAttribute('href', 'https://example.com');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  });
});

describe('AIExplanationPanel — banners (WS8)', () => {
  it('shows the direction-mismatch banner when the backend flags it', () => {
    render(
      <AIExplanationPanel
        data={makeData({ direction_mismatch: true })}
        isLoading={false}
      />,
    );
    expect(
      screen.getByText(/Signal has changed since this explanation/),
    ).toBeInTheDocument();
  });

  it('no mismatch banner when the flag is absent or false', () => {
    render(
      <AIExplanationPanel
        data={makeData({ direction_mismatch: false })}
        isLoading={false}
      />,
    );
    expect(
      screen.queryByText(/Signal has changed since this explanation/),
    ).not.toBeInTheDocument();
  });

  it('staleness banner is age-based: fresh signal → no banner', () => {
    render(
      <AIExplanationPanel
        data={makeData({
          signal_direction: 'BUY',
          signal_generated_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
        })}
        isLoading={false}
      />,
    );
    expect(screen.queryByText(/Based on/)).not.toBeInTheDocument();
  });

  it('staleness banner appears for a signal older than one hour', () => {
    render(
      <AIExplanationPanel
        data={makeData({
          signal_direction: 'BUY',
          signal_generated_at: new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString(),
        })}
        isLoading={false}
      />,
    );
    expect(screen.getByText(/Based on/)).toBeInTheDocument();
  });
});

describe('AIExplanationPanel — failed state retry (WS8)', () => {
  it('renders a retry button when a handler is provided', () => {
    let called = 0;
    render(
      <AIExplanationPanel
        data={{ ...makeData(), failed: true, suggestion_id: 'sid-1' }}
        isLoading={false}
        onRequestExplanation={async () => { called += 1; }}
      />,
    );
    const button = screen.getByLabelText('Retry AI explanation generation');
    act(() => { button.click(); });
    expect(called).toBe(1);
  });

  it('no retry button without a handler', () => {
    render(
      <AIExplanationPanel data={{ ...makeData(), failed: true }} isLoading={false} />,
    );
    expect(
      screen.queryByLabelText('Retry AI explanation generation'),
    ).not.toBeInTheDocument();
  });
});
