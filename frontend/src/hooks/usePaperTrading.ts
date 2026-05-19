/**
 * Paper Trading React Query Hooks
 * =================================
 * TanStack Query v5 hooks for all paper trading API interactions.
 * Follows the exact staleTime / retry patterns used across the rest of the app.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryOptions,
} from '@tanstack/react-query';
import { paperTradingAPI } from '@/lib/api';
import type {
  ClosePositionRequest,
  ConvertPositionRequest,
  CreatePortfolioRequest,
  OrdersQueryParams,
  OutcomesQueryParams,
  PlaceOrderRequest,
  PnlSnapshotsQueryParams,
  PositionsQueryParams,
  TransactionType,
  UpdatePortfolioSettingsRequest,
} from '@/types/paper_trading';

// ──────────────────────────────────────────────────────────────────────────────
// Query Keys
// ──────────────────────────────────────────────────────────────────────────────

export const paperTradingKeys = {
  all: ['paper-trading'] as const,
  portfolio: () => [...paperTradingKeys.all, 'portfolio'] as const,
  positions: (params?: PositionsQueryParams) =>
    [...paperTradingKeys.all, 'positions', params] as const,
  positionDetail: (positionId: string) =>
    [...paperTradingKeys.all, 'positions', positionId] as const,
  orders: (params?: OrdersQueryParams) =>
    [...paperTradingKeys.all, 'orders', params] as const,
  qtySuggestion: (suggestionId: string) =>
    [...paperTradingKeys.all, 'qty-suggestion', suggestionId] as const,
  outcomes: (params?: OutcomesQueryParams) =>
    [...paperTradingKeys.all, 'outcomes', params] as const,
  outcomeStats: () => [...paperTradingKeys.all, 'outcome-stats'] as const,
  pnlSnapshots: (params?: PnlSnapshotsQueryParams) =>
    [...paperTradingKeys.all, 'pnl-snapshots', params] as const,
  priceTargets: (symbol: string | null, direction: TransactionType) =>
    [...paperTradingKeys.all, 'price-targets', symbol, direction] as const,
};

// ──────────────────────────────────────────────────────────────────────────────
// Portfolio
// ──────────────────────────────────────────────────────────────────────────────

export function usePortfolioSummary(
  options?: Partial<UseQueryOptions<Awaited<ReturnType<typeof paperTradingAPI.getMyPortfolio>>>>
) {
  return useQuery({
    queryKey: paperTradingKeys.portfolio(),
    queryFn: () => paperTradingAPI.getMyPortfolio(),
    staleTime: 30_000,
    retry: (failureCount, error: any) => {
      // 404 = no portfolio yet — don't retry, return null
      if (error?.statusCode === 404) return false;
      return failureCount < 2;
    },
    ...options,
  });
}

export function useCreatePortfolio() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: CreatePortfolioRequest) =>
      paperTradingAPI.createPortfolio(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
    },
  });
}

export function useUpdatePortfolioSettings() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: UpdatePortfolioSettingsRequest) =>
      paperTradingAPI.updatePortfolioSettings(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
    },
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Positions
// ──────────────────────────────────────────────────────────────────────────────

export function usePositions(params?: PositionsQueryParams, enabled = true) {
  return useQuery({
    queryKey: paperTradingKeys.positions(params),
    queryFn: () => paperTradingAPI.getPositions(params),
    staleTime: 10_000,
    refetchInterval: 30_000,
    enabled,
  });
}

export function usePositionDetail(positionId: string, enabled = true) {
  return useQuery({
    queryKey: paperTradingKeys.positionDetail(positionId),
    queryFn: () => paperTradingAPI.getPositionDetail(positionId),
    staleTime: 10_000,
    enabled: enabled && !!positionId,
  });
}

export function useClosePosition() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      positionId,
      payload,
    }: {
      positionId: string;
      payload: ClosePositionRequest;
    }) => paperTradingAPI.closePosition(positionId, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.positions() });
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.orders() });
    },
  });
}

export function useConvertPosition() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      positionId,
      payload,
    }: {
      positionId: string;
      payload: ConvertPositionRequest;
    }) => paperTradingAPI.convertPosition(positionId, payload),
    onSuccess: (_data, { positionId }) => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.positions() });
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.positionDetail(positionId) });
    },
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Orders
// ──────────────────────────────────────────────────────────────────────────────

export function useOrders(params?: OrdersQueryParams) {
  return useQuery({
    queryKey: paperTradingKeys.orders(params),
    queryFn: () => paperTradingAPI.getOrders(params),
    staleTime: 15_000,
  });
}

export function usePlaceOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: PlaceOrderRequest) => paperTradingAPI.placeOrder(payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.orders() });
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.positions() });
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.portfolio() });
    },
  });
}

export function useCancelOrder() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (orderId: string) => paperTradingAPI.cancelOrder(orderId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: paperTradingKeys.orders() });
    },
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Quantity Suggestion
// ──────────────────────────────────────────────────────────────────────────────

export function useQtySuggestion(suggestionId: string, enabled = true) {
  return useQuery({
    queryKey: paperTradingKeys.qtySuggestion(suggestionId),
    queryFn: () => paperTradingAPI.getQtySuggestion(suggestionId),
    staleTime: 60_000,
    enabled: enabled && !!suggestionId,
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Outcomes (admin / dev)
// ──────────────────────────────────────────────────────────────────────────────

export function useOutcomes(params?: OutcomesQueryParams) {
  return useQuery({
    queryKey: paperTradingKeys.outcomes(params),
    queryFn: () => paperTradingAPI.getOutcomes(params),
    staleTime: 60_000,
  });
}

export function useOutcomeStats() {
  return useQuery({
    queryKey: paperTradingKeys.outcomeStats(),
    queryFn: () => paperTradingAPI.getOutcomeStats(),
    staleTime: 120_000,
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// P&L Snapshots
// ──────────────────────────────────────────────────────────────────────────────

export function usePnlSnapshots(params?: PnlSnapshotsQueryParams) {
  return useQuery({
    queryKey: paperTradingKeys.pnlSnapshots(params),
    queryFn: () => paperTradingAPI.getPnlSnapshots(params),
    staleTime: 300_000,
  });
}

// ──────────────────────────────────────────────────────────────────────────────
// Price Targets (ML-assisted SL/TP for manual trades)
// ──────────────────────────────────────────────────────────────────────────────

/**
 * Fetch ML-computed stop-loss and take-profit levels for a manual trade.
 *
 * Fires only when a valid symbol and a positive entry price are available.
 * staleTime matches the backend cache TTL (4 hours) so repeated opens of the
 * same manual trade modal are served instantly from the React Query cache.
 *
 * retry: false — a 404 (no OHLCV data) is a permanent condition for that
 * symbol, not a transient error. Retrying would just delay the failure UX.
 */
export function usePriceTargets(
  symbol: string | null,
  direction: TransactionType,
  entryPrice: number,
  enabled = true,
) {
  return useQuery({
    queryKey: paperTradingKeys.priceTargets(symbol, direction),
    queryFn: () => paperTradingAPI.getPriceTargets(symbol!, direction, entryPrice),
    enabled: enabled && !!symbol && entryPrice > 0,
    staleTime: 14_400_000,   // 4 hours — matches backend cache TTL
    retry: false,
    refetchOnWindowFocus: false,
  });
}
