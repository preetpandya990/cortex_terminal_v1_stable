'use client';

/**
 * useDragReorder
 *
 * Native Pointer Events drag-and-drop hook for reorderable grids/lists.
 * No external library required.
 *
 * Design:
 * - Drag initiates only from a designated grip handle (via `getGripHandlers`).
 * - Global window listeners track movement after the grip is pressed, so the
 *   cursor can roam freely without losing the drag context.
 * - `document.elementFromPoint` + a `data-drag-id` attribute on each card
 *   identifies the drop target during movement.
 * - A 5 px movement threshold prevents accidental drags on click.
 * - `clickPreventedRef` suppresses the ghost click that browsers fire after
 *   a pointerup, preventing the drop target from also registering a click.
 * - Cleanup (window listener removal) is guaranteed in pointerup, pointercancel,
 *   and hook unmount.
 */

import { useState, useRef, useCallback, useEffect } from 'react';

export interface UseDragReorderReturn {
  /** ID of the card currently being dragged, or null. */
  draggingId: number | null;
  /** ID of the card the pointer is currently hovering over, or null. */
  overId: number | null;
  /**
   * Ref that is true during the synthetic click event that fires immediately
   * after a drag ends. Use this to suppress accidental click navigation on
   * the drop-target card.
   */
  clickPreventedRef: React.RefObject<boolean>;
  /**
   * Returns pointer event handlers to attach to the grip handle element of
   * the card with the given numeric `id`.
   */
  getGripHandlers: (id: number) => {
    onPointerDown: (e: React.PointerEvent<HTMLElement>) => void;
  };
}

const DRAG_THRESHOLD_PX = 5;

export function useDragReorder(
  onReorder: (draggedId: number, targetId: number) => Promise<void> | void,
): UseDragReorderReturn {
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [overId, setOverId] = useState<number | null>(null);

  // Mutable drag state stored in a single ref to avoid stale closures inside
  // the global window handlers (which close over this ref, not React state).
  const dragStateRef = useRef<{
    active: boolean;
    id: number | null;
    overId: number | null;
    startX: number;
    startY: number;
  }>({
    active: false,
    id: null,
    overId: null,
    startX: 0,
    startY: 0,
  });

  // True during the synthetic click event that browsers fire after pointerup.
  const clickPreventedRef = useRef(false);

  // Always refers to the latest onReorder callback without being a dep of the
  // global handlers (prevents listener churn on every parent re-render).
  const onReorderRef = useRef(onReorder);
  useEffect(() => { onReorderRef.current = onReorder; }, [onReorder]);

  // Stored so we can remove exactly the same function reference on cleanup.
  const handlersRef = useRef<{
    move: ((e: PointerEvent) => void) | null;
    up: ((e: PointerEvent) => void) | null;
    cancel: (() => void) | null;
  }>({ move: null, up: null, cancel: null });

  const detachGlobalHandlers = useCallback(() => {
    const { move, up, cancel } = handlersRef.current;
    if (move) window.removeEventListener('pointermove', move);
    if (up) window.removeEventListener('pointerup', up);
    if (cancel) window.removeEventListener('pointercancel', cancel);
    handlersRef.current = { move: null, up: null, cancel: null };
  }, []);

  const resetDragState = useCallback(() => {
    dragStateRef.current = { active: false, id: null, overId: null, startX: 0, startY: 0 };
    setDraggingId(null);
    setOverId(null);
  }, []);

  // Detach listeners and reset on unmount to prevent leaks.
  useEffect(() => {
    return () => {
      detachGlobalHandlers();
    };
  }, [detachGlobalHandlers]);

  const getGripHandlers = useCallback(
    (id: number) => ({
      onPointerDown: (e: React.PointerEvent<HTMLElement>) => {
        // Only respond to the primary (left) mouse button.
        if (e.button !== 0) return;

        // Prevent the browser from triggering text selection or image drag.
        e.preventDefault();
        // Do not stop propagation — let any parent scroll/focus logic still run.

        const state = dragStateRef.current;
        state.id = id;
        state.startX = e.clientX;
        state.startY = e.clientY;
        state.active = false;
        state.overId = null;

        // ── Global pointermove ──────────────────────────────────────────────
        const onMove = (ev: PointerEvent) => {
          const s = dragStateRef.current;
          if (s.id === null) return;

          const dx = Math.abs(ev.clientX - s.startX);
          const dy = Math.abs(ev.clientY - s.startY);

          // Activate drag only once the threshold is crossed.
          if (!s.active) {
            if (dx < DRAG_THRESHOLD_PX && dy < DRAG_THRESHOLD_PX) return;
            s.active = true;
            setDraggingId(s.id);
          }

          // Identify the card under the cursor via data attribute.
          const el = document.elementFromPoint(ev.clientX, ev.clientY);
          const card = el?.closest('[data-drag-id]') as HTMLElement | null;
          const targetId = card ? Number(card.dataset.dragId) : null;

          if (targetId !== null && targetId !== s.id) {
            if (targetId !== s.overId) {
              s.overId = targetId;
              setOverId(targetId);
            }
          } else if (s.overId !== null) {
            s.overId = null;
            setOverId(null);
          }
        };

        // ── Global pointerup ────────────────────────────────────────────────
        const onUp = () => {
          const s = dragStateRef.current;
          const wasActive = s.active;
          const sourceId = s.id;
          const dropTargetId = s.overId;

          // Suppress the synthetic click that fires immediately after pointerup
          // on the element under the cursor (the drop-target card).
          if (wasActive && dropTargetId !== null) {
            clickPreventedRef.current = true;
            // Clear the flag after the click event fires in the same task.
            queueMicrotask(() => { clickPreventedRef.current = false; });
          }

          detachGlobalHandlers();
          resetDragState();

          if (wasActive && sourceId !== null && dropTargetId !== null && sourceId !== dropTargetId) {
            void onReorderRef.current(sourceId, dropTargetId);
          }
        };

        // ── Global pointercancel ────────────────────────────────────────────
        const onCancel = () => {
          detachGlobalHandlers();
          resetDragState();
        };

        handlersRef.current = { move: onMove, up: onUp, cancel: onCancel };
        window.addEventListener('pointermove', onMove);
        window.addEventListener('pointerup', onUp);
        window.addEventListener('pointercancel', onCancel);
      },
    }),
    [detachGlobalHandlers, resetDragState],
  );

  return { draggingId, overId, clickPreventedRef, getGripHandlers };
}
