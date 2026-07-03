/**
 * AI Processing Queue — React Query hooks
 * ==========================================
 * Status polling + per-category dispatch mutations for the 3 demand-driven
 * Tier-2 Gemini queues (sentiment, forecast, classification). Mirrors
 * useWorkerControl.ts's query-key / mutation-invalidation pattern exactly.
 *
 * Degraded state is driven by the parent page's useWorkerHealth().isDegraded
 * (same contract as TasksGrid/TaskCard) rather than re-detecting it here.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AxiosError } from 'axios'
import { api } from '@/lib/api-client'
import { extractWorkerError } from '@/hooks/useWorkerControl'
import type { AIProcessingCategory, AIProcessingStatusResponse, DispatchResult } from '@/types/ai_processing'

// ── Query keys ─────────────────────────────────────────────────────────────────

export const aiProcessingKeys = {
  all:    ['ai_processing']           as const,
  status: ['ai_processing', 'status'] as const,
} as const

// ── Queries ────────────────────────────────────────────────────────────────────

export function useAIProcessingStatus() {
  return useQuery<AIProcessingStatusResponse, AxiosError>({
    queryKey: aiProcessingKeys.status,
    queryFn: async () => {
      const { data } = await api.get<AIProcessingStatusResponse>('/admin/ai-processing/status')
      return data
    },
    refetchInterval: 15_000,
  })
}

// ── Mutations ──────────────────────────────────────────────────────────────────

export function useDispatchCategory() {
  const qc = useQueryClient()
  return useMutation<DispatchResult, AxiosError, AIProcessingCategory>({
    mutationFn: async (category) => {
      const { data } = await api.post<DispatchResult>(`/admin/ai-processing/${category}/dispatch`)
      return data
    },
    onSettled: () => qc.invalidateQueries({ queryKey: aiProcessingKeys.status }),
  })
}

export interface DispatchAllResult {
  category: AIProcessingCategory
  success: boolean
  result?: DispatchResult
  error?: string
}

/**
 * Dispatch all 3 categories concurrently via Promise.allSettled — one
 * category's failure never blocks or masks the other two. There is
 * deliberately no backend /dispatch-all endpoint (see admin_ai_processing.py).
 */
export function useDispatchAll() {
  const dispatch = useDispatchCategory()

  const dispatchAll = async (): Promise<DispatchAllResult[]> => {
    const categories: AIProcessingCategory[] = ['sentiment', 'forecast', 'classification']
    const settled = await Promise.allSettled(
      categories.map((category) => dispatch.mutateAsync(category)),
    )
    return settled.map((outcome, i) => {
      const category = categories[i]
      if (outcome.status === 'fulfilled') {
        return { category, success: true, result: outcome.value }
      }
      return { category, success: false, error: extractWorkerError(outcome.reason, 'Dispatch failed') }
    })
  }

  return { dispatchAll, isPending: dispatch.isPending }
}
