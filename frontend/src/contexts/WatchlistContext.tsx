'use client'

/**
 * WatchlistContext — singleton provider for watchlist state and live prices.
 *
 * Replaces the standalone useWatchlist hook instances that previously ran in
 * both page.tsx and DetailPane.tsx, doubling REST LTP polling. This provider
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
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
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

  // Feed health — for UI indicators
  isMarketFeedConnected: boolean
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
  const [isMarketFeedConnected, setIsMarketFeedConnected] = useState(false)

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

  // ── Subscribe watchlist items when they load / change ────────────────────

  useEffect(() => {
    if (!watchlistItems.length) return
    watchlistItems.forEach((item) => subscribeInstrument(item.instrument_key))
    return () => {
      watchlistItems.forEach((item) => unsubscribeInstrument(item.instrument_key))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchlistItems])

  // ── WebSocket connection ─────────────────────────────────────────────────

  const buildWsUrl = useCallback((): string | null => {
    if (!accessToken) return null
    const base = WS_BASE_URL.replace(/\/$/, '')
    return `${base}/upstox/market-feed/ws?token=${encodeURIComponent(accessToken)}`
  }, [accessToken])

  const connectWs = useCallback(() => {
    const url = buildWsUrl()
    if (!url || !shouldConnectRef.current) return
    if (wsRef.current?.readyState === WebSocket.OPEN) return

    const ws = new WebSocket(url)
    wsRef.current = ws

    ws.onopen = () => {
      reconnectAttempt.current = 0
      setIsMarketFeedConnected(true)

      // Re-subscribe all instruments with active ref counts
      const activeKeys = [...subCountsRef.current.keys()]
      if (activeKeys.length > 0) {
        ws.send(JSON.stringify({ type: 'sub', instrument_keys: activeKeys }))
      }
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data as string)
        if (data.type === 'ltpc') {
          priceFeed.update(data.instrument_key, data.ltp, data.cp, data.ts)
        }
        // ping/subscribed/unsubscribed/error events are informational — no action needed
      } catch {
        // Malformed frame — discard
      }
    }

    ws.onerror = () => {
      setIsMarketFeedConnected(false)
    }

    ws.onclose = () => {
      wsRef.current = null
      setIsMarketFeedConnected(false)

      if (!shouldConnectRef.current) return

      // Exponential backoff with jitter, max 30 s
      const attempt = reconnectAttempt.current
      reconnectAttempt.current = attempt + 1
      const base  = Math.min(1000 * Math.pow(2, attempt), 30_000)
      const jitter = base * 0.25 * (Math.random() * 2 - 1)
      reconnectTimeout.current = setTimeout(connectWs, base + jitter)
    }
  }, [buildWsUrl])

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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, isAuthReady, accessToken])

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
