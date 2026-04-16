/**
 * Hook for fetching and managing ML models
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api-client";
import { useAuth } from '@/contexts/AuthContext';
import type {
  ModelsResponse,
  DriftReportsResponse,
  ModelFilters,
  UpdateModelStateRequest,
} from "@/types/models";

/**
/**
 * Fetch ML models with filters
 */
export function useModels(filters: ModelFilters = {}) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<ModelsResponse>({
    queryKey: ["models", filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.state) params.append("state", filters.state);
      if (filters.page) params.append("page", filters.page.toString());
      if (filters.limit) params.append("limit", filters.limit.toString());
      
      const response = await api.get<ModelsResponse>(
        `governance/models?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 60000,
  });
}

/**
/**
 * Fetch drift reports
 */
export function useDriftReports(filters: { model_id?: number; drift_only?: boolean } = {}) {
  const { isAuthenticated } = useAuth();
  
  return useQuery<DriftReportsResponse>({
    queryKey: ["drift-reports", filters],
    queryFn: async () => {
      const params = new URLSearchParams();
      if (filters.model_id) params.append("model_id", filters.model_id.toString());
      if (filters.drift_only) params.append("drift_only", "true");
      
      const response = await api.get<DriftReportsResponse>(
        `governance/drift-reports?${params.toString()}`
      );
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 60000,
  });
}

/**
 * Update model state mutation
 */
export function useUpdateModelState() {
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  
  return useMutation({
    mutationFn: async ({
      modelId,
      data,
    }: {
      modelId: number;
      data: UpdateModelStateRequest;
    }) => {
      const response = await api.post(
        `governance/models/${modelId}/state`,
        data
      );
      return response.data;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });
}
