"use client";

import { useEffect, useRef } from "react";

/**
 * Selects all interactive elements that can receive keyboard focus, excluding
 * elements that are hidden, disabled, or explicitly removed from the tab order.
 */
const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
  "details > summary",
  "audio[controls]",
  "video[controls]",
  "[contenteditable]:not([contenteditable='false'])",
].join(",");

function getFocusableElements(container: HTMLElement): HTMLElement[] {
  const all = Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  );
  return all.filter((el) => {
    // Exclude elements with a hidden ancestor
    if (el.closest('[aria-hidden="true"]')) return false;
    // Exclude visually hidden elements
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  });
}

interface UseFocusTrapOptions {
  /** Whether the trap is currently active. */
  active: boolean;
  /** Ref to the container element to trap focus within. */
  containerRef: React.RefObject<HTMLElement | null>;
  /** Optional CSS selector for the element to receive initial focus. */
  initialFocusSelector?: string;
  /** Called when Escape is pressed while the trap is active. */
  onEscape: () => void;
  /**
   * When true, skips restoring focus to the trigger element on deactivation.
   * Useful when the caller handles focus restoration manually.
   */
  skipRestoration?: boolean;
}

/**
 * WAI-ARIA 1.2 compliant focus trap.
 *
 * Behaviour:
 * - On activation: captures the current active element as the trigger, then
 *   moves focus to `initialFocusSelector` or the first focusable descendant.
 * - While active: Tab/Shift+Tab wrap focus within the container; Escape calls
 *   `onEscape`.
 * - On deactivation: restores focus to the captured trigger (unless
 *   `skipRestoration` is true).
 */
export function useFocusTrap({
  active,
  containerRef,
  initialFocusSelector,
  onEscape,
  skipRestoration = false,
}: UseFocusTrapOptions): void {
  // Keep a stable ref to the escape callback to avoid re-registering listeners.
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  });

  // Captured trigger — the element that was focused before the trap activated.
  const triggerRef = useRef<Element | null>(null);

  useEffect(() => {
    if (!active) {
      // Restore focus when the trap deactivates.
      if (!skipRestoration && triggerRef.current instanceof HTMLElement) {
        const trigger = triggerRef.current;
        requestAnimationFrame(() => {
          trigger.focus();
        });
      }
      triggerRef.current = null;
      return;
    }

    // Capture the current active element as the trigger.
    triggerRef.current = document.activeElement;

    // Move focus into the container.
    requestAnimationFrame(() => {
      const container = containerRef.current;
      if (!container) return;

      if (initialFocusSelector) {
        const target = container.querySelector<HTMLElement>(initialFocusSelector);
        if (target) {
          target.focus();
          return;
        }
      }

      const focusable = getFocusableElements(container);
      focusable[0]?.focus();
    });

    function handleKeyDown(e: KeyboardEvent): void {
      const container = containerRef.current;
      if (!container) return;

      if (e.key === "Escape") {
        e.stopPropagation();
        onEscapeRef.current();
        return;
      }

      if (e.key !== "Tab") return;

      const focusable = getFocusableElements(container);
      if (focusable.length === 0) {
        e.preventDefault();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const activeEl = document.activeElement;

      const isInsideContainer = container.contains(activeEl);

      if (e.shiftKey) {
        // Shift+Tab: if focus is on the first element (or outside), wrap to last.
        if (!isInsideContainer || activeEl === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        // Tab: if focus is on the last element (or outside), wrap to first.
        if (!isInsideContainer || activeEl === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // containerRef and skipRestoration are stable; initialFocusSelector changes
    // should re-run the focus-move but not re-register the keydown listener.
  }, [active]);
}
