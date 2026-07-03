/**
 * Worker Control — React Query hooks
 * ====================================
 * All mutations invalidate the shared tasks query key on settle so
 * the grid reflects new state within one poll cycle.
 *
 * Degraded detection: the main API returns 503 + { degraded: true } when
 * the worker sidecar is unreachable.  isDegraded is surfaced as a first-class
 * boolean so components can disable controls without inspecting raw errors.
 */

import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AxiosError } from 'axios'
import { api } from '@/lib/api-client'
import type {
  TaskActionResponse,
  WorkerHealthResponse,
  WorkerTasksResponse,
} from '@/types/worker_control'

// ── Query keys ─────────────────────────────────────────────────────────────────

export const workerKeys = {
  all:    ['worker']           as const,
  health: ['worker', 'health'] as const,
  tasks:  ['worker', 'tasks']  as const,
} as const

// ── Helpers ────────────────────────────────────────────────────────────────────

function isDegradedError(error: unknown): boolean {
  return (
    error instanceof AxiosError &&
    error.response?.status === 503 &&
    error.response?.data?.degraded === true
  )
}

export function extractWorkerError(err: unknown, fallback = 'Operation failed'): string {
  if (err instanceof AxiosError) {
    return err.response?.data?.detail ?? err.response?.data?.error ?? fallback
  }
  return fallback
}

// ── Queries ────────────────────────────────────────────────────────────────────

export function useWorkerHealth() {
  const query = useQuery<WorkerHealthResponse, AxiosError>({
    queryKey: workerKeys.health,
    queryFn: async () => {
      const { data } = await api.get<WorkerHealthResponse>('/admin/worker/health')
      return data
    },
    refetchInterval: 15_000,
    retry: (failureCount, error) => !isDegradedError(error) && failureCount < 1,
  })

  return { ...query, isDegraded: isDegradedError(query.error) }
}

export function useWorkerTasks() {
  return useQuery<WorkerTasksResponse, AxiosError>({
    queryKey: workerKeys.tasks,
    queryFn: async () => {
      const { data } = await api.get<WorkerTasksResponse>('/admin/worker/tasks')
      return data
    },
    refetchInterval: 10_000,
    placeholderData: keepPreviousData,
    retry: (failureCount, error) => !isDegradedError(error) && failureCount < 1,
  })
}

// ── Mutations ──────────────────────────────────────────────────────────────────

export function usePauseTask() {
  const qc = useQueryClient()
  return useMutation<TaskActionResponse, AxiosError, string>({
    mutationFn: async (name) => {
      const { data } = await api.post<TaskActionResponse>(`/admin/worker/tasks/${name}/pause`)
      return data
    },
    onSettled: () => qc.invalidateQueries({ queryKey: workerKeys.tasks }),
  })
}

export function useResumeTask() {
  const qc = useQueryClient()
  return useMutation<TaskActionResponse, AxiosError, string>({
    mutationFn: async (name) => {
      const { data } = await api.post<TaskActionResponse>(`/admin/worker/tasks/${name}/resume`)
      return data
    },
    onSettled: () => qc.invalidateQueries({ queryKey: workerKeys.tasks }),
  })
}

export function useTriggerTask() {
  const qc = useQueryClient()
  return useMutation<TaskActionResponse, AxiosError, string>({
    mutationFn: async (name) => {
      const { data } = await api.post<TaskActionResponse>(`/admin/worker/tasks/${name}/trigger`)
      return data
    },
    onSettled: () => qc.invalidateQueries({ queryKey: workerKeys.tasks }),
  })
}

export function useRestartTask() {
  const qc = useQueryClient()
  return useMutation<TaskActionResponse, AxiosError, string>({
    mutationFn: async (name) => {
      const { data } = await api.post<TaskActionResponse>(`/admin/worker/tasks/${name}/restart`)
      return data
    },
    onSettled: () => qc.invalidateQueries({ queryKey: workerKeys.tasks }),
  })
}
