/**
 * Market Regime hooks
 *
 * All data is fetched via the Next.js BFF proxy (/api/v1/strategy/...) which
 * forwards to the FastAPI backend. Computation is DB-driven — no mock data.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from "@/contexts/AuthContext";
import type {
  MarketOverview,
  IndexConstituentsResponse,
  InstrumentRegime,
} from "@/types/regime";

/**
 * Fetch the market overview — Nifty 50 macro regime + all 10 index regimes.
 * Refreshes every 15 minutes (matches backend cache TTL).
 */
export function useMarketOverview() {
  const { isAuthenticated } = useAuth();

  return useQuery<MarketOverview>({
    queryKey: ["regime", "overview"],
    queryFn: async () => {
      const res = await api.get<MarketOverview>("strategy/market/overview");
      return res.data;
    },
    enabled: isAuthenticated,
    staleTime: 15 * 60 * 1000,
    refetchInterval: 15 * 60 * 1000,
    retry: 2,
  });
}

/**
 * Fetch constituent stocks for the selected Nifty index.
 * Refreshes every 5 minutes.
 */
export function useIndexConstituents(indexKey: string | null) {
  const { isAuthenticated } = useAuth();

  return useQuery<IndexConstituentsResponse>({
    queryKey: ["regime", "index", indexKey],
    queryFn: async () => {
      const res = await api.get<IndexConstituentsResponse>(
        `strategy/index/${indexKey}/constituents`
      );
      return res.data;
    },
    enabled: isAuthenticated && !!indexKey,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    retry: 2,
  });
}

/**
 * Fetch detailed regime for a single instrument (used in the detail pane).
 * Refreshes every 5 minutes.
 */
export function useInstrumentRegime(instrumentKey: string | null) {
  const { isAuthenticated } = useAuth();

  return useQuery<InstrumentRegime>({
    queryKey: ["regime", "instrument", instrumentKey],
    queryFn: async () => {
      const encoded = encodeURIComponent(instrumentKey!);
      const res = await api.get<InstrumentRegime>(
        `strategy/instrument/${encoded}/regime`
      );
      return res.data;
    },
    enabled: isAuthenticated && !!instrumentKey,
    staleTime: 5 * 60 * 1000,
    refetchInterval: 5 * 60 * 1000,
    retry: 1,
  });
}
