/**
 * Admin User Management — React Query hooks
 * ==========================================
 * All mutations invalidate the shared query key so the table stays in sync.
 */

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AxiosError } from 'axios';
import { api } from '@/lib/api-client';

// ── Types ──────────────────────────────────────────────────────────────────────

export type UserRole = 'viewer' | 'trader' | 'admin';

export interface AdminUser {
  id: number;
  username: string;
  email: string;
  full_name: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  last_login: string | null;
}

export interface UserListResponse {
  users: AdminUser[];
  total: number;
}

export interface CreateUserInput {
  username: string;
  email: string;
  password: string;
  full_name?: string | null;
  role: UserRole;
}

export interface UpdateUserInput {
  id: number;
  role?: UserRole;
  is_active?: boolean;
}

// ── Query keys ─────────────────────────────────────────────────────────────────

export const adminUserKeys = {
  all: ['admin-users'] as const,
  list: (search?: string) => [...adminUserKeys.all, search ?? ''] as const,
};

// ── Error extraction ───────────────────────────────────────────────────────────

export function extractApiError(err: unknown, fallback = 'Operation failed'): string {
  if (err instanceof AxiosError) {
    return err.response?.data?.detail ?? err.response?.data?.error ?? fallback;
  }
  return fallback;
}

// ── Hooks ──────────────────────────────────────────────────────────────────────

export function useAdminUsers(search?: string) {
  return useQuery<UserListResponse>({
    queryKey: adminUserKeys.list(search),
    queryFn: async () => {
      const params = new URLSearchParams();
      if (search?.trim()) params.set('search', search.trim());
      const { data } = await api.get<UserListResponse>(
        `/admin/users${params.size ? `?${params}` : ''}`,
      );
      return data;
    },
    staleTime: 30_000,
    retry: 1,
  });
}

export function useCreateUser() {
  const qc = useQueryClient();
  return useMutation<AdminUser, unknown, CreateUserInput>({
    mutationFn: async (input) => {
      const { data } = await api.post<AdminUser>('/admin/users', input);
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: adminUserKeys.all }),
  });
}

export function useUpdateUser() {
  const qc = useQueryClient();
  return useMutation<AdminUser, unknown, UpdateUserInput>({
    mutationFn: async ({ id, ...payload }) => {
      const { data } = await api.patch<AdminUser>(`/admin/users/${id}`, payload);
      return data;
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: adminUserKeys.all }),
  });
}

export function useDeleteUser() {
  const qc = useQueryClient();
  return useMutation<void, unknown, number>({
    mutationFn: async (id) => {
      await api.delete(`/admin/users/${id}`);
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: adminUserKeys.all }),
  });
}
