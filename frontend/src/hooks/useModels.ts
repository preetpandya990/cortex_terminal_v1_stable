import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/contexts/AuthContext";
import { api } from "@/lib/api-client";
import type {
  DriftReportsResponse,
  GovernanceSummary,
  ModelFilters,
  ModelsResponse,
  UpdateModelStateRequest,
} from "@/types/models";

const STALE_TIME = 30_000; // 30 s — governance data doesn't change every second

// ── Query keys ────────────────────────────────────────────────────────────────

export const governanceKeys = {
  all:          ["governance"] as const,
  summary:      () => [...governanceKeys.all, "summary"] as const,
  models:       (filters: ModelFilters) => [...governanceKeys.all, "models", filters] as const,
  driftReports: (filters: { modelId?: number; hours?: number }) =>
    [...governanceKeys.all, "drift-reports", filters] as const,
};

// ── Governance summary ────────────────────────────────────────────────────────

export function useGovernanceSummary() {
  const { isAuthenticated } = useAuth();

  return useQuery<GovernanceSummary>({
    queryKey:      governanceKeys.summary(),
    queryFn:       async () => (await api.get<GovernanceSummary>("governance/summary")).data,
    enabled:       isAuthenticated,
    staleTime:     STALE_TIME,
    refetchInterval: 60_000,
  });
}

// ── Model list ────────────────────────────────────────────────────────────────

export function useModels(filters: ModelFilters = {}) {
  const { isAuthenticated } = useAuth();

  return useQuery<ModelsResponse>({
    queryKey: governanceKeys.models(filters),
    queryFn:  async () => {
      const params = new URLSearchParams();
      if (filters.state)  params.set("state",  filters.state);
      if (filters.page)   params.set("page",   String(filters.page));
      if (filters.limit)  params.set("limit",  String(filters.limit));
      return (await api.get<ModelsResponse>(`governance/models?${params}`)).data;
    },
    enabled:       isAuthenticated,
    staleTime:     STALE_TIME,
    refetchInterval: 60_000,
  });
}

// ── Drift reports (7-day window for sparklines) ───────────────────────────────

export function useDriftReports(filters: { modelId?: number; hours?: number } = {}) {
  const { isAuthenticated } = useAuth();
  const hours = filters.hours ?? 168; // 7 days default — enough for sparklines

  return useQuery<DriftReportsResponse>({
    queryKey: governanceKeys.driftReports({ modelId: filters.modelId, hours }),
    queryFn:  async () => {
      const params = new URLSearchParams({ hours: String(hours), limit: "2000" });
      if (filters.modelId) params.set("model_id", String(filters.modelId));
      return (await api.get<DriftReportsResponse>(`governance/drift-reports?${params}`)).data;
    },
    enabled:       isAuthenticated,
    staleTime:     STALE_TIME,
    refetchInterval: 60_000,
  });
}

// ── Mutations ─────────────────────────────────────────────────────────────────

export function useUpdateModelState() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ modelId, data }: { modelId: number; data: UpdateModelStateRequest }) =>
      (await api.post(`governance/models/${modelId}/state`, data)).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: governanceKeys.all });
    },
  });
}

export function usePromoteModel() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ modelName, targetState }: { modelName: string; targetState: string }) =>
      (await api.post(`governance/models/${encodeURIComponent(modelName)}/promote`, { target_state: targetState })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: governanceKeys.all });
    },
  });
}

export function useTriggerDriftCheck() {
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async ({ modelId, lookbackHours = 24 }: { modelId: number; lookbackHours?: number }) =>
      (await api.post(`governance/drift/check/${modelId}`, { lookback_hours: lookbackHours })).data,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: governanceKeys.driftReports({}) });
      queryClient.invalidateQueries({ queryKey: governanceKeys.summary() });
    },
  });
}
