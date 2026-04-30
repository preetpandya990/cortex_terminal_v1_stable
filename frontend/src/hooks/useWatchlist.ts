'use client'

/**
 * useWatchlist — thin context consumer hook.
 *
 * Previously contained all watchlist state, LTP polling timers, and a
 * per-instance cache. All of that now lives in WatchlistProvider (singleton).
 * This hook is the unchanged public API surface — all call sites work without
 * modification.
 */

import { useWatchlistContext } from '@/contexts/WatchlistContext'

export function useWatchlist() {
  return useWatchlistContext()
}

// Re-export types consumed by call sites
export type { WatchlistItem, WatchlistItemCreate } from '@/lib/api'
