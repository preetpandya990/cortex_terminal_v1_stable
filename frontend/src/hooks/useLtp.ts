/**
 * useLtp — live last-traded-price hook backed by the PriceFeed external store.
 *
 * Uses React 18 useSyncExternalStore for:
 *   - Concurrent Mode safety (no tearing between renders)
 *   - Strict Mode safety (no double-subscribe bugs)
 *   - SSR safety (server snapshot always returns null)
 *
 * The subscription is scoped to a single instrument key — this component only
 * re-renders when ITS instrument's price changes, not on every tick globally.
 *
 * Usage:
 *   const snapshot = useLtp("NSE_EQ|INE002A01018")
 *   snapshot?.ltp   // last traded price
 *   snapshot?.cp    // previous session close
 *   snapshot?.ts    // server timestamp ms
 */

import { useSyncExternalStore } from 'react'
import { priceFeed, type PriceSnapshot } from '@/lib/price-feed'

export function useLtp(instrumentKey: string): PriceSnapshot | null {
  return useSyncExternalStore(
    (callback) => priceFeed.subscribe(instrumentKey, callback),
    ()         => priceFeed.getSnapshot(instrumentKey),
    ()         => priceFeed.getServerSnapshot(instrumentKey),
  )
}
