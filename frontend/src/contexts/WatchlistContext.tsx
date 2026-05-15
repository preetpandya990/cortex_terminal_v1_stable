'use client'

/**
 * WatchlistContext — singleton provider for watchlist state and live prices.
 *
 * Replaces the standalone useWatchlist hook instances that previously ran in
 * both page.tsx and DetailPane.tsx, doubling REST LTP polling.  This provider
 * runs exactly once (in providers.tsx), owns a single WebSocket connection to
 * the backend market-feed endpoint, and fans live prices out to any consumer
 * via the PriceFeed external store + useLtp hook.
 *
 * Responsibilities:
 *   1. Watchlist CRUD — React Query fetch + add/remove/reorder mutations
 *   2. Market-feed WS — one connection, auth-gated, reconnects automatically
 *   3. Subscription ref-counting — tracks which instruments need live prices
 *      (watchlist items + any instrument currently open in DetailPane)
 *   4. PriceFeed updates — writes every incoming ltpc tick to the store
 *   5. Upstream health tracking — surfaces Upstox WS status (connected /
 *      reconnecting) so the UI can display live/stale indicators without
 *      additional polling
 *
 * Upstream health protocol:
 *   The backend sends {type:"upstream_status", status:"connected"|"reconnecting"}
 *   on auth success (immediate) and again each time the Upstox WS connects or
 *   drops.  This context forwards that status through `upstreamStatus` and
 *   exposes `lastTickAt` so consumers can independently detect staleness.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { useAuth } from '@/contexts/AuthContext'
import { priceFeed } from '@/lib/price-feed'
import {
  isNetworkError,
  watchlistAPI,
  WS_BASE_URL,
  type WatchlistItem,
  type WatchlistItemCreate,
} from '@/lib/api'

// ── Types ─────────────────────────────────────────────────────────────────────

/**
 * Status of the upstream Upstox WebSocket connection.
 *
 * 'connected'    — Upstox WS is live; price ticks are flowing.
 * 'reconnecting' — Upstox WS dropped or is still connecting; data may be stale.
 * 'unknown'      — initial state before the first upstream_status frame arrives.
 */
export type UpstreamStatus = 'connected' | 'reconnecting' | 'unknown'

interface WatchlistContextValue {
  // Items (prices sourced from PriceFeed — not REST polling)
  items:     WatchlistItem[]
  isLoading: boolean
  isError:   boolean
  error:     unknown

  // Mutations — identical public API to the old useWatchlist hook
  addToWatchlist:      (item: WatchlistItemCreate) => Promise<void>
  removeFromWatchlist: (itemId: number)            => Promise<void>
  reorderWatchlist:    (args: { itemId: number; newPosition: number }) => Promise<void>
  checkInWatchlist:    (instrumentKey: string)     => Promise<{ in_watchlist: boolean; item_id: number | null }>

  // Mutation states
  isAdding:    boolean
  isRemoving:  boolean
  isReordering: boolean

  // Market-feed subscription management — used by DetailPane
  subscribeInstrument:   (instrumentKey: string) => void
  unsubscribeInstrument: (instrumentKey: string) => void

  // ── Feed health — for UI indicators ─────────────────────────────────────────
  /** True when the browser↔backend WebSocket is open and authenticated. */
  isMarketFeedConnected: boolean
  /**
   * Status of the backend↔Upstox upstream WebSocket.
   * 'connected' means live ticks are flowing; 'reconnecting' means data may
   * be stale.  'unknown' is the initial state before the first status frame.
   */
  upstreamStatus: UpstreamStatus
  /**
   * Ref containing the Unix timestamp (ms) of the last received ltpc tick,
   * or null if no tick has arrived yet this session.  Read via .current —
   * updating it does NOT trigger a re-render (use for staleness calculations
   * in derived components).
   */
  lastTickAt: React.MutableRefObject<number | null>
}

// ── Context ───────────────────────────────────────────────────────────────────

const WatchlistContext = createContext<WatchlistContextValue | null>(null)

// ── Provider ──────────────────────────────────────────────────────────────────

export function WatchlistProvider({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isAuthReady, accessToken } = useAuth()
  const queryClient = useQueryClient()

  // Subscription ref counts: instrument_key → number of active subscribers
  const subCountsRef = useRef(new Map<string, number>())

  // WebSocket instance and connection state (ref-based to avoid render loops)
  const wsRef            = useRef<WebSocket | null>(null)
  const reconnectTimeout = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectAttempt = useRef(0)
  const shouldConnectRef = useRef(false)
  // Always points to the latest connectWs — prevents stale closures in onclose callbacks
  const connectWsRef     = useRef<() => void>(() => {})
  const [isMarketFeedConnected, setIsMarketFeedConnected] = useState(false)

  // ── Upstream health state ─────────────────────────────────────────────────
  // upstreamStatus drives UI indicators (live / reconnecting badge).
  const [upstreamStatus, setUpstreamStatus] = useState<UpstreamStatus>('unknown')
  // lastTickAt is a ref — updating it must not cause re-renders on every tick
  // (potentially 4 ticks/s × N instruments).  Consumers that need reactivity
  // can read it in their own RAF or polling loop.
  const lastTickAt = useRef<number | null>(null)

  // Token in a ref — synchronously updated each render (before effects fire) so
  // every connectWs() call uses the latest value.  NEVER added to connection-effect
  // deps — doing so would reconnect the feed on every 15-minute JWT rotation.
  const accessTokenRef = useRef(accessToken)
  accessTokenRef.current = accessToken

  // ── Watchlist fetch ──────────────────────────────────────────────────────

  const {
    data: watchlistItems = [],
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['watchlist'],
    queryFn:  watchlistAPI.getWatchlist,
    enabled:  isAuthenticated && isAuthReady,
    staleTime: 30_000,
    refetchOnWindowFocus: true,
  })

  // ── Mutations ────────────────────────────────────────────────────────────

  const addMutation = useMutation({
    mutationFn: (item: WatchlistItemCreate) => watchlistAPI.addToWatchlist(item),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  })

  const removeMutation = useMutation({
    mutationFn: (itemId: number) => watchlistAPI.removeFromWatchlist(itemId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  })

  const reorderMutation = useMutation({
    mutationFn: ({ itemId, newPosition }: { itemId: number; newPosition: number }) =>
      watchlistAPI.reorderWatchlist({ item_id: itemId, new_position: newPosition }),

    onMutate: async ({ itemId, newPosition }) => {
      await queryClient.cancelQueries({ queryKey: ['watchlist'] })
      const previousItems = queryClient.getQueryData<WatchlistItem[]>(['watchlist'])

      if (previousItems) {
        const sorted = [...previousItems].sort((a, b) => a.position - b.position)
        const sourceIdx = sorted.findIndex((i) => i.id === itemId)
        const targetIdx = sorted.findIndex((i) => i.position === newPosition)

        if (sourceIdx !== -1 && targetIdx !== -1 && sourceIdx !== targetIdx) {
          const [moved] = sorted.splice(sourceIdx, 1)
          sorted.splice(targetIdx, 0, moved)
          queryClient.setQueryData<WatchlistItem[]>(
            ['watchlist'],
            sorted.map((item, idx) => ({ ...item, position: idx + 1 })),
          )
        }
      }
      return { previousItems }
    },

    onError: (_err, _vars, context) => {
      if (context?.previousItems) {
        queryClient.setQueryData<WatchlistItem[]>(['watchlist'], context.previousItems)
      }
    },

    onSettled: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  })

  const checkInWatchlist = useCallback(
    async (instrumentKey: string) => {
      if (!isAuthenticated) return { in_watchlist: false, item_id: null }
      return watchlistAPI.checkInWatchlist(instrumentKey)
    },
    [isAuthenticated],
  )

  // ── Subscription management ──────────────────────────────────────────────

  const sendWs = useCallback((data: object) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  const subscribeInstrument = useCallback((instrumentKey: string) => {
    const counts = subCountsRef.current
    const prev   = counts.get(instrumentKey) ?? 0
    counts.set(instrumentKey, prev + 1)
    if (prev === 0) {
      sendWs({ type: 'sub', instrument_keys: [instrumentKey] })
    }
  }, [sendWs])

  const unsubscribeInstrument = useCallback((instrumentKey: string) => {
    const counts = subCountsRef.current
    const prev   = counts.get(instrumentKey) ?? 1
    const next   = Math.max(0, prev - 1)
    if (next === 0) {
      counts.delete(instrumentKey)
      priceFeed.evict(instrumentKey)
      sendWs({ type: 'unsub', instrument_keys: [instrumentKey] })
    } else {
      counts.set(instrumentKey, next)
    }
  }, [sendWs])

  // ── Subscribe watchlist items — delta-only to avoid evicting live prices ─────
  // Tracks the previous item set and only subscribes/unsubscribes the diff.
  // The old approach subscribed then cleaned up ALL items on every list change,
  // which caused priceFeed.evict() to wipe live prices for items still present.
  const prevItemKeysRef = useRef<Set<string>>(new Set())

  useEffect(() => {
    const newKeys  = new Set(watchlistItems.map((i) => i.instrument_key))
    const prevKeys = prevItemKeysRef.current

    // Subscribe newly added keys
    newKeys.forEach((key) => {
      if (!prevKeys.has(key)) subscribeInstrument(key)
    })

    // Unsubscribe removed keys (evicts priceFeed data and sends WS unsub)
    prevKeys.forEach((key) => {
      if (!newKeys.has(key)) unsubscribeInstrument(key)
    })

    prevItemKeysRef.current = newKeys

    return () => {
      // Full cleanup on provider unmount only
      prevItemKeysRef.current.forEach((key) => unsubscribeInstrument(key))
      prevItemKeysRef.current = new Set()
    }
  }, [watchlistItems, subscribeInstrument, unsubscribeInstrument])

  // ── Seed priceFeed from last daily close when market is closed / on page load
  // Prevents watchlist cards from showing "—" until the first live tick arrives.
  // Only seeds entries that have no live tick yet — live ticks always win.
  useEffect(() => {
    if (!watchlistItems.length) return
    watchlistItems.forEach((item) => {
      if (item.last_close == null) return
      const cp = item.prev_close ?? item.last_close
      priceFeed.seed(item.instrument_key, item.last_close, cp)
    })
  }, [watchlistItems])

  // ── WebSocket connection ─────────────────────────────────────────────────

  const wsUrl = `${WS_BASE_URL.replace(/\/$/, '')}/upstox/market-feed/ws`

  const connectWs = useCallback(() => {
    if (!shouldConnectRef.current) return
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      // In-band auth — token never placed in the URL (server logs / browser history).
      const currentToken = accessTokenRef.current
      if (currentToken) {
        ws.send(JSON.stringify({ type: 'auth', token: currentToken }))
      } else {
        console.warn('[MarketFeedWS] No token available for in-band auth')
      }
      // Connection is not considered "live" until the server confirms auth below.
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data as string)

        // ── Auth confirmation ───────────────────────────────────────────────
        // Only now is the session considered live.  Re-subscribe all instruments
        // that accumulated in subCountsRef while the WS was down or reconnecting.
        if (data.type === 'connected') {
          reconnectAttempt.current = 0
          setIsMarketFeedConnected(true)
          const activeKeys = [...subCountsRef.current.keys()]
          if (activeKeys.length > 0) {
            ws.send(JSON.stringify({ type: 'sub', instrument_keys: activeKeys }))
          }
          return
        }

        // ── Upstream health status ──────────────────────────────────────────
        // Sent by the backend immediately after auth (snapshot of current state)
        // and again each time the Upstox WS connects or drops.
        if (data.type === 'upstream_status') {
          const status = data.status as UpstreamStatus
          setUpstreamStatus(status)
          return
        }

        // ── Protocol-level acknowledgements ────────────────────────────────
        if (data.type === 'reauthed') return

        // ── Live price tick ─────────────────────────────────────────────────
        if (data.type === 'ltpc') {
          lastTickAt.current = Date.now()
          priceFeed.update(data.instrument_key, data.ltp, data.cp, data.ts)
          return
        }

        // ping / subscribed / unsubscribed / error frames — informational only
      } catch {
        // Malformed frame — discard silently
      }
    }

    ws.onerror = () => {
      setIsMarketFeedConnected(false)
    }

    ws.onclose = () => {
      // If a new connection was already established (e.g. effect re-ran while this
      // socket was still closing), don't touch wsRef or schedule a reconnect.
      if (wsRef.current !== ws) return

      wsRef.current = null
      setIsMarketFeedConnected(false)

      if (!shouldConnectRef.current) return

      // Exponential backoff with jitter, max 30 s
      const attempt = reconnectAttempt.current
      reconnectAttempt.current = attempt + 1
      const base   = Math.min(1000 * Math.pow(2, attempt), 30_000)
      const jitter = base * 0.25 * (Math.random() * 2 - 1)
      reconnectTimeout.current = setTimeout(() => connectWsRef.current(), base + jitter)
    }
  }, [wsUrl])
  // Keep ref in sync — runs synchronously during render, before any effect fires
  connectWsRef.current = connectWs

  // ── Connection lifecycle effect ───────────────────────────────────────────
  // NOTE: `accessToken` is intentionally absent from deps.
  // Token rotation is handled by the dedicated reauth effect below.
  useEffect(() => {
    if (!isAuthenticated || !isAuthReady || !accessToken) {
      shouldConnectRef.current = false
      if (wsRef.current) {
        wsRef.current.close(1000, 'Logged out')
        wsRef.current = null
      }
      return
    }

    shouldConnectRef.current = true
    connectWs()

    return () => {
      shouldConnectRef.current = false
      if (reconnectTimeout.current) clearTimeout(reconnectTimeout.current)
      if (wsRef.current) {
        wsRef.current.close(1000, 'Provider unmount')
        wsRef.current = null
      }
      setIsMarketFeedConnected(false)
    }
  // `accessToken` intentionally absent — handled by the reauth effect below.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, isAuthReady])

  // ── Token rotation effect ─────────────────────────────────────────────────
  // When the JWT rotates (background refresh), send a reauth frame in-band so
  // the market-feed stream stays connected without interruption.
  // If the connection is not open at that moment, accessTokenRef is already
  // updated and the next connectWs() call will use the fresh token.
  useEffect(() => {
    if (!accessToken) return
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ type: 'reauth', token: accessToken }))
    }
  }, [accessToken])

  // ── Exposed mutations ────────────────────────────────────────────────────

  const addToWatchlist = useCallback(
    async (item: WatchlistItemCreate) => {
      await addMutation.mutateAsync(item)
    },
    [addMutation],
  )

  const removeFromWatchlist = useCallback(
    async (itemId: number) => {
      await removeMutation.mutateAsync(itemId)
    },
    [removeMutation],
  )

  const reorderWatchlist = useCallback(
    async (args: { itemId: number; newPosition: number }) => {
      await reorderMutation.mutateAsync(args)
    },
    [reorderMutation],
  )

  // ── Context value ────────────────────────────────────────────────────────

  const value: WatchlistContextValue = {
    items:     watchlistItems,
    isLoading,
    isError,
    error,

    addToWatchlist,
    removeFromWatchlist,
    reorderWatchlist,
    checkInWatchlist,

    isAdding:    addMutation.isPending,
    isRemoving:  removeMutation.isPending,
    isReordering: reorderMutation.isPending,

    subscribeInstrument,
    unsubscribeInstrument,
    isMarketFeedConnected,
    upstreamStatus,
    lastTickAt,
  }

  return (
    <WatchlistContext.Provider value={value}>
      {children}
    </WatchlistContext.Provider>
  )
}

// ── Consumer hook ─────────────────────────────────────────────────────────────

export function useWatchlistContext(): WatchlistContextValue {
  const ctx = useContext(WatchlistContext)
  if (!ctx) throw new Error('[WatchlistContext] Must be used within WatchlistProvider')
  return ctx
}
