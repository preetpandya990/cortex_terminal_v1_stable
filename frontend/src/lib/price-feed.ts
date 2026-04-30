/**
 * PriceFeed — framework-agnostic external store for live instrument prices.
 *
 * Compatible with React 18 useSyncExternalStore (Concurrent Mode safe).
 * Prices are stored in a plain Map — no React state — so updates never
 * trigger React re-renders directly. Components subscribe per instrument key
 * and are notified only when their specific instrument's price changes.
 *
 * Batching (Option C — frontend half):
 *   Multiple update() calls within a single animation frame are coalesced into
 *   one notify pass via requestAnimationFrame. This prevents scheduling more
 *   React renders than the browser can paint when many instruments tick at once.
 *   Falls back to queueMicrotask in non-browser environments (SSR, tests).
 */

export type PriceSnapshot = {
  ltp: number | null
  cp:  number | null  // previous session close
  ts:  number         // server timestamp ms
}

class PriceFeed {
  private readonly prices    = new Map<string, PriceSnapshot>()
  private readonly listeners = new Map<string, Set<() => void>>()
  private flushScheduled     = false

  /**
   * Called by WatchlistProvider on every incoming ltpc WS message.
   * Stores the snapshot and schedules a batched listener notification.
   */
  update(instrumentKey: string, ltp: number, cp: number, ts: number): void {
    this.prices.set(instrumentKey, { ltp, cp, ts })
    this.scheduleFlush()
  }

  /**
   * useSyncExternalStore `subscribe` argument — scoped to one instrument key.
   * Returns the unsubscribe function.
   */
  subscribe(instrumentKey: string, callback: () => void): () => void {
    if (!this.listeners.has(instrumentKey)) {
      this.listeners.set(instrumentKey, new Set())
    }
    this.listeners.get(instrumentKey)!.add(callback)
    return () => {
      this.listeners.get(instrumentKey)?.delete(callback)
      if (this.listeners.get(instrumentKey)?.size === 0) {
        this.listeners.delete(instrumentKey)
      }
    }
  }

  /**
   * useSyncExternalStore `getSnapshot` argument.
   * Returns the latest snapshot for the instrument, or null if not yet received.
   */
  getSnapshot(instrumentKey: string): PriceSnapshot | null {
    return this.prices.get(instrumentKey) ?? null
  }

  /**
   * useSyncExternalStore `getServerSnapshot` argument — always null.
   * Prices are real-time client-only data; SSR never has live prices.
   */
  getServerSnapshot(_instrumentKey: string): null {
    return null
  }

  /**
   * Remove all stored prices for an instrument.
   * Called when an instrument is fully unsubscribed from the market feed.
   */
  evict(instrumentKey: string): void {
    this.prices.delete(instrumentKey)
    this.listeners.delete(instrumentKey)
  }

  private scheduleFlush(): void {
    if (this.flushScheduled) return
    this.flushScheduled = true

    if (typeof requestAnimationFrame !== 'undefined') {
      // Browser: batch within one animation frame (~16ms at 60 fps)
      requestAnimationFrame(() => {
        this.flushScheduled = false
        this.notifyAll()
      })
    } else {
      // SSR / test environment: flush after current microtask queue
      queueMicrotask(() => {
        this.flushScheduled = false
        this.notifyAll()
      })
    }
  }

  private notifyAll(): void {
    this.listeners.forEach((callbacks) => {
      callbacks.forEach((cb) => cb())
    })
  }
}

/**
 * Module-level singleton — one PriceFeed instance for the entire app lifetime.
 * Imported directly by useLtp and WatchlistProvider; no React context needed.
 */
export const priceFeed = new PriceFeed()
