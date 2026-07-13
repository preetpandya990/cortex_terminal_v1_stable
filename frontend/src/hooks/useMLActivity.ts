/**
 * useMLActivity — ML Pipeline Activity State Machine
 * ====================================================
 *
 * Tracks the real-time lifecycle of every correlation attempt the backend
 * ML pipeline makes, from the moment it picks up a scanner anomaly or news
 * event through to consensus reached (→ completed) or rejected.
 *
 * State model:
 *   Map<correlation_id, MLActivityItem> — O(1) lookup for in-place mutations
 *   triggered by WebSocket messages arriving out of order.
 *
 * Message flow:
 *   correlation_started  → INSERT  (status: processing)
 *   new_suggestion       → MUTATE  (status: completed, enrich with direction/score)
 *   correlation_completed→ MUTATE  (attach per-agent latencies; arrives after
 *                           new_suggestion, so the row already exists)
 *   correlation_rejected → MUTATE  (status: rejected, enrich with reason)
 *
 * Eviction:
 *   Items older than EVICTION_TTL_MS are purged on a 30-second interval so
 *   the feed never grows unbounded across a full trading session.
 *
 * Design invariants:
 *   • All state mutations go through useReducer — no direct setState calls.
 *   • handleMessage is stable (useCallback with no deps) — safe to pass as
 *     a WebSocket onMessage callback without triggering reconnects.
 *   • sortedItems is memoised — re-computed only when the Map reference changes.
 */

import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import type { CorrelationActivityItem } from '@/lib/api';
import type { WebSocketMessage, TriggerType } from './useWebSocket';

// ── Constants ────────────────────────────────────────────────────────────────

/** Items are evicted after 10 minutes of inactivity. */
const EVICTION_TTL_MS = 10 * 60 * 1000;

/** Hard cap on feed length — oldest item dropped when exceeded. */
const MAX_FEED_SIZE = 50;

/** Eviction check runs every 30 seconds. */
const EVICTION_INTERVAL_MS = 30_000;

// ── Types ─────────────────────────────────────────────────────────────────────

export type ActivityStatus = 'processing' | 'completed' | 'rejected';

export interface MLActivityItem {
  correlation_id: string;
  symbol: string;
  /** Human-readable NSE ticker; may equal symbol when instrument_key is unavailable. */
  trading_symbol: string;
  trigger_type: TriggerType;
  status: ActivityStatus;
  /** Unix timestamp (ms) when the pipeline started. */
  started_at: number;
  /** Unix timestamp (ms) when the pipeline resolved. undefined while processing. */
  resolved_at?: number;
  // ── completed fields ──────────────────────────────────────────────────────
  direction?: 'BUY' | 'SELL';
  consensus_score?: number;
  confidence_level?: 'HIGH' | 'MEDIUM' | 'LOW';
  // ── rejected fields ───────────────────────────────────────────────────────
  rejection_reason?: string;
  // ── latency telemetry (from correlation_completed) ──────────────────────────
  latencies?: { scanner_ms?: number; ai_ms?: number; ml_ms?: number };
}

// ── Reducer ──────────────────────────────────────────────────────────────────

type ActivityAction =
  | { type: 'SEED'; items: MLActivityItem[] }
  | { type: 'STARTED';   item: MLActivityItem }
  | {
      type:             'COMPLETED';
      correlation_id:   string;
      direction:        'BUY' | 'SELL';
      consensus_score:  number;
      confidence_level: 'HIGH' | 'MEDIUM' | 'LOW';
      resolved_at:      number;
    }
  | {
      type:             'REJECTED';
      correlation_id:   string;
      symbol:           string;
      trading_symbol:   string;
      trigger_type:     TriggerType;
      rejection_reason: string;
      consensus_score:  number;
      resolved_at:      number;
    }
  | {
      type:           'LATENCIES';
      correlation_id: string;
      latencies:      { scanner_ms?: number; ai_ms?: number; ml_ms?: number };
    }
  | { type: 'EVICT'; cutoff: number };

function mlActivityReducer(
  state: Map<string, MLActivityItem>,
  action: ActivityAction,
): Map<string, MLActivityItem> {
  switch (action.type) {
    case 'SEED': {
      // Merge seed items into current state.  Live WS events take precedence —
      // if a correlation_id already exists (arrived via WS before the REST fetch
      // completed), the existing entry wins.
      if (action.items.length === 0) return state;
      const next = new Map(state);
      for (const item of action.items) {
        if (!next.has(item.correlation_id)) {
          next.set(item.correlation_id, item);
        }
      }
      return next;
    }

    case 'STARTED': {
      const next = new Map(state);
      next.set(action.item.correlation_id, action.item);
      // Enforce hard cap — evict the oldest item when exceeded.
      if (next.size > MAX_FEED_SIZE) {
        let oldestKey: string | null = null;
        let oldestTs = Infinity;
        for (const [key, item] of next) {
          if (item.started_at < oldestTs) {
            oldestTs = item.started_at;
            oldestKey = key;
          }
        }
        if (oldestKey !== null) next.delete(oldestKey);
      }
      return next;
    }

    case 'COMPLETED': {
      const existing = state.get(action.correlation_id);
      if (!existing) return state; // No matching started event — discard.
      const next = new Map(state);
      next.set(action.correlation_id, {
        ...existing,
        status:           'completed',
        direction:        action.direction,
        consensus_score:  action.consensus_score,
        confidence_level: action.confidence_level,
        resolved_at:      action.resolved_at,
      });
      return next;
    }

    case 'REJECTED': {
      const next = new Map(state);
      const existing = state.get(action.correlation_id);
      if (existing) {
        // Update the in-flight processing row.
        next.set(action.correlation_id, {
          ...existing,
          status:           'rejected',
          rejection_reason: action.rejection_reason,
          resolved_at:      action.resolved_at,
        });
      } else {
        // REJECTED arrived before STARTED (rare race) — synthesise a row so the
        // feed stays coherent rather than silently dropping the event.
        next.set(action.correlation_id, {
          correlation_id:   action.correlation_id,
          symbol:           action.symbol,
          trading_symbol:   action.trading_symbol,
          trigger_type:     action.trigger_type,
          status:           'rejected',
          started_at:       action.resolved_at,
          resolved_at:      action.resolved_at,
          rejection_reason: action.rejection_reason,
        });
      }
      return next;
    }

    case 'LATENCIES': {
      const existing = state.get(action.correlation_id);
      if (!existing) return state; // No matching row (e.g. evicted already) — discard.
      const next = new Map(state);
      next.set(action.correlation_id, { ...existing, latencies: action.latencies });
      return next;
    }

    case 'EVICT': {
      let changed = false;
      const next = new Map(state);
      for (const [key, item] of next) {
        if (item.started_at < action.cutoff) {
          next.delete(key);
          changed = true;
        }
      }
      // Return same reference when nothing was evicted — avoids a memoised
      // re-sort of the items array.
      return changed ? next : state;
    }
  }
}

// ── Hook ─────────────────────────────────────────────────────────────────────

// ── Seed helper ───────────────────────────────────────────────────────────────

/**
 * Convert a REST `CorrelationActivityItem` (historical, from the DB) into the
 * internal `MLActivityItem` shape used by the reducer.
 */
function fromRestItem(item: CorrelationActivityItem): MLActivityItem {
  return {
    correlation_id:   item.correlation_id,
    symbol:           item.symbol,
    trading_symbol:   item.trading_symbol ?? item.symbol,
    trigger_type:     item.trigger_type,
    status:           item.status,
    started_at:       new Date(item.started_at).getTime(),
    resolved_at:      item.resolved_at ? new Date(item.resolved_at).getTime() : undefined,
    direction:        item.direction ?? undefined,
    consensus_score:  item.consensus_score ?? undefined,
    confidence_level: (item.confidence_level ?? undefined) as MLActivityItem['confidence_level'],
    rejection_reason: item.rejection_reason ?? undefined,
  };
}

// ── Hook ─────────────────────────────────────────────────────────────────────

export interface UseMLActivityOptions {
  /** Historical items fetched from the REST endpoint to pre-populate the feed. */
  seedItems?: CorrelationActivityItem[];
}

export interface UseMLActivityReturn {
  /** Activity items sorted newest-first — stable reference between ticks. */
  items: MLActivityItem[];
  /** Pass this directly to useWebSocket's onMessage option. */
  handleMessage: (msg: WebSocketMessage) => void;
}

export function useMLActivity({ seedItems }: UseMLActivityOptions = {}): UseMLActivityReturn {
  const [state, dispatch] = useReducer(
    mlActivityReducer,
    seedItems,
    (seed) => {
      const initial = new Map<string, MLActivityItem>();
      seed?.forEach((item) => {
        const mapped = fromRestItem(item);
        initial.set(mapped.correlation_id, mapped);
      });
      return initial;
    },
  );

  // Apply seed items when the async REST fetch resolves.
  // A ref guards against re-seeding on every render and ensures WS events that
  // arrived before the fetch completed are not overwritten (the SEED reducer
  // action skips existing keys).
  const seededRef = useRef(false);
  useEffect(() => {
    if (!seedItems || seededRef.current) return;
    const mapped = seedItems.map(fromRestItem);
    if (mapped.length > 0) {
      dispatch({ type: 'SEED', items: mapped });
    }
    seededRef.current = true;
  }, [seedItems]);

  // Stable callback — intentionally no deps so this never changes reference
  // and does not trigger WebSocket reconnects when passed as onMessage.
  const handleMessage = useCallback((msg: WebSocketMessage) => {
    const now = Date.now();

    switch (msg.type) {
      case 'correlation_started':
        dispatch({
          type: 'STARTED',
          item: {
            correlation_id: msg.correlation_id,
            symbol:         msg.symbol,
            trading_symbol: msg.trading_symbol ?? msg.symbol,
            trigger_type:   msg.trigger_type,
            status:         'processing',
            started_at:     new Date(msg.started_at).getTime(),
          },
        });
        break;

      case 'new_suggestion':
        dispatch({
          type:             'COMPLETED',
          correlation_id:   msg.correlation_id,
          direction:        msg.signal_direction,
          consensus_score:  msg.consensus_score,
          confidence_level: msg.confidence_level,
          resolved_at:      now,
        });
        break;

      case 'correlation_completed':
        if (msg.latencies) {
          dispatch({
            type:           'LATENCIES',
            correlation_id: msg.correlation_id,
            latencies:      msg.latencies,
          });
        }
        break;

      case 'correlation_rejected':
        dispatch({
          type:             'REJECTED',
          correlation_id:   msg.correlation_id,
          symbol:           msg.symbol,
          trading_symbol:   msg.trading_symbol ?? msg.symbol,
          trigger_type:     msg.trigger_type,
          rejection_reason: msg.rejection_reason,
          consensus_score:  msg.consensus_score,
          resolved_at:      now,
        });
        break;

      default:
        break;
    }
  }, []);

  // Periodic eviction — removes items that have aged out of the TTL window.
  useEffect(() => {
    const id = setInterval(() => {
      dispatch({ type: 'EVICT', cutoff: Date.now() - EVICTION_TTL_MS });
    }, EVICTION_INTERVAL_MS);
    return () => clearInterval(id);
  }, []);

  // Sorted newest-first; recomputed only when the Map reference changes.
  const items = useMemo(
    () =>
      Array.from(state.values()).sort((a, b) => b.started_at - a.started_at),
    [state],
  );

  return { items, handleMessage };
}
