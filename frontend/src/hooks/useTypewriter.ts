'use client';

import { useEffect, useRef, useState } from 'react';

/**
 * Explanations already fully revealed once this session, so revisiting the
 * same content (e.g. navigating prev/next between suggestions and back)
 * renders instantly instead of retyping.  Module-scoped by design — it must
 * outlive individual component mounts/unmounts as the user browses.
 */
const typedCache = new Set<string>();

interface UseTypewriterOptions {
  /** Characters revealed per second of animation. */
  charsPerSecond?: number;
  /** When false, the full text is shown immediately and nothing animates. */
  enabled?: boolean;
}

interface UseTypewriterResult {
  /** Number of characters of `text` currently revealed. */
  revealedLength: number;
  /** True once `revealedLength === text.length`. */
  isComplete: boolean;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== 'undefined' &&
    window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
  );
}

/**
 * Reveals `text` progressively, character by character, to simulate a
 * streaming "typewriter" effect for content that actually arrives as a
 * single complete string (e.g. one-shot LLM responses pushed over SSE).
 *
 * Frame-rate independent: reveal count is derived from elapsed wall-clock
 * time via requestAnimationFrame, not a fixed-interval tick count, so
 * animation speed is consistent across displays and unaffected by dropped
 * frames.
 *
 * Content that was already fully revealed once this session (by exact
 * string match) renders instantly on subsequent mounts — the effect is for
 * *new* content, not a replay on every render.
 *
 * A text value that extends a previously-seen prefix (true incremental
 * streaming, should the backend ever support it) continues the existing
 * animation from its current position rather than restarting; any other
 * change resets and starts a fresh reveal.
 *
 * No-ops (renders full text immediately) when `enabled` is false or the
 * user has requested reduced motion.
 */
export function useTypewriter(
  text: string,
  { charsPerSecond = 600, enabled = true }: UseTypewriterOptions = {},
): UseTypewriterResult {
  const shouldAnimate = enabled && !prefersReducedMotion();

  const [prevText, setPrevText] = useState(text);
  const [revealedLength, setRevealedLength] = useState(() =>
    shouldAnimate && !typedCache.has(text) ? 0 : text.length,
  );

  // Derive the reveal starting point during render when `text` changes —
  // React's sanctioned pattern for resetting state in response to a prop
  // change, avoiding an extra effect-driven render pass. Resume in place for
  // true incremental growth of the same content; anything else (a
  // genuinely different string) starts a fresh reveal from zero.
  if (text !== prevText) {
    const isContinuation = shouldAnimate && text.startsWith(prevText) && revealedLength > 0;
    const nextRevealed =
      !shouldAnimate || typedCache.has(text)
        ? text.length
        : Math.min(isContinuation ? revealedLength : 0, text.length);

    setPrevText(text);
    setRevealedLength(nextRevealed);
  }

  // Mirrors `revealedLength` into a ref, read (never written) by the
  // animation effect below as its starting point — this lets that effect
  // omit `revealedLength` from its dependency array (avoiding a full
  // teardown/restart of the rAF loop on every frame) while still picking up
  // the render-phase-adjusted value. Ref writes belong in effects, not
  // render, so this runs in its own effect, guaranteed by React to run
  // before the effect declared after it.
  const revealedLengthRef = useRef(revealedLength);
  useEffect(() => {
    revealedLengthRef.current = revealedLength;
  }, [revealedLength]);

  // Animation loop — a genuine subscription to an external clock (rAF), so
  // updating state from within its callback (not the effect body itself) is
  // the correct pattern here.
  useEffect(() => {
    if (!shouldAnimate) return;

    const startLength = revealedLengthRef.current;
    if (startLength >= text.length) {
      if (text.length > 0) typedCache.add(text);
      return;
    }

    let rafId: number;
    let lastTimestamp: number | null = null;
    let charAccumulator = startLength;

    const step = (timestamp: number) => {
      if (lastTimestamp === null) lastTimestamp = timestamp;
      const deltaSeconds = (timestamp - lastTimestamp) / 1000;
      lastTimestamp = timestamp;

      charAccumulator = Math.min(text.length, charAccumulator + deltaSeconds * charsPerSecond);
      const nextLength = Math.floor(charAccumulator);

      if (nextLength > revealedLengthRef.current) {
        revealedLengthRef.current = nextLength;
        setRevealedLength(nextLength);
      }

      if (charAccumulator < text.length) {
        rafId = requestAnimationFrame(step);
      } else {
        typedCache.add(text);
      }
    };

    rafId = requestAnimationFrame(step);
    return () => cancelAnimationFrame(rafId);
  }, [text, shouldAnimate, charsPerSecond]);

  return {
    revealedLength: Math.min(revealedLength, text.length),
    isComplete: revealedLength >= text.length,
  };
}
