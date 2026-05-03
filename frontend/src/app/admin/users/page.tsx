"use client";

import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type FormEvent,
  type ChangeEvent,
} from "react";
import {
  Plus,
  Search,
  Trash2,
  Loader2,
  AlertCircle,
  RefreshCw,
  Eye,
  EyeOff,
  Users,
  ShieldCheck,
  Briefcase,
  X,
} from "lucide-react";
import { formatDistanceToNow, parseISO } from "date-fns";
import { useAuth } from "@/contexts/AuthContext";
import { useToast } from "@/components/ui/toast";
import {
  useAdminUsers,
  useCreateUser,
  useUpdateUser,
  useDeleteUser,
  extractApiError,
  type AdminUser,
  type UserRole,
} from "@/hooks/useAdminUsers";
import { cn } from "@/lib/utils";

// ── Constants ──────────────────────────────────────────────────────────────────

const ROLE_META: Record<UserRole, { label: string; classes: string }> = {
  admin: {
    label: "Admin",
    classes: "bg-rose-50 text-rose-700 border border-rose-200",
  },
  trader: {
    label: "Trader",
    classes: "bg-blue-50 text-blue-700 border border-blue-200",
  },
  viewer: {
    label: "Viewer",
    classes: "bg-slate-100 text-slate-600 border border-slate-200",
  },
};

const ROLE_OPTIONS: { value: UserRole; label: string }[] = [
  { value: "viewer", label: "Viewer" },
  { value: "trader", label: "Trader" },
  { value: "admin", label: "Admin" },
];

// ── Helpers ────────────────────────────────────────────────────────────────────

function avatarInitial(user: AdminUser): string {
  return (user.full_name?.[0] ?? user.username[0]).toUpperCase();
}

function fmtDate(iso: string | null): string {
  if (!iso) return "Never";
  try {
    return formatDistanceToNow(parseISO(iso), { addSuffix: true });
  } catch {
    return "—";
  }
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function RoleBadge({ role }: { role: UserRole }) {
  const meta = ROLE_META[role];
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-semibold",
        meta.classes,
      )}
    >
      {meta.label}
    </span>
  );
}

function StatusToggle({
  checked,
  onChange,
  disabled,
  pending,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  pending?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => !disabled && !pending && onChange(!checked)}
      disabled={disabled || pending}
      className={cn(
        "relative inline-flex h-5 w-9 flex-shrink-0 cursor-pointer rounded-full border-2 border-transparent",
        "transition-colors duration-200 ease-in-out",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-1",
        checked ? "bg-emerald-500" : "bg-slate-200",
        (disabled || pending) && "cursor-not-allowed opacity-40",
      )}
      title={disabled ? "Cannot change your own status" : undefined}
    >
      {pending ? (
        <span className="absolute inset-0 flex items-center justify-center">
          <Loader2 className="h-3 w-3 animate-spin text-white" />
        </span>
      ) : (
        <span
          className={cn(
            "pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow",
            "transform transition-transform duration-200 ease-in-out",
            checked ? "translate-x-4" : "translate-x-0",
          )}
        />
      )}
    </button>
  );
}

// ── Create User Modal ──────────────────────────────────────────────────────────

interface CreateModalProps {
  open: boolean;
  onClose: () => void;
}

function CreateUserModal({ open, onClose }: CreateModalProps) {
  const { mutateAsync: createUser, isPending } = useCreateUser();
  const { success, error: toastError } = useToast();

  const [form, setForm] = useState({
    username: "",
    email: "",
    password: "",
    full_name: "",
    role: "viewer" as UserRole,
  });
  const [showPassword, setShowPassword] = useState(false);
  const [errors, setErrors] = useState<Partial<Record<keyof typeof form, string>>>({});

  const reset = () => {
    setForm({ username: "", email: "", password: "", full_name: "", role: "viewer" });
    setErrors({});
    setShowPassword(false);
  };

  const handleClose = () => { reset(); onClose(); };

  const validate = (): boolean => {
    const next: typeof errors = {};
    if (!form.username.trim()) next.username = "Required";
    else if (!/^[a-zA-Z0-9_-]{3,50}$/.test(form.username))
      next.username = "3–50 chars, letters/digits/underscore/hyphen only";
    if (!form.email.trim()) next.email = "Required";
    else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.email))
      next.email = "Enter a valid email address";
    if (!form.password) next.password = "Required";
    else if (form.password.length < 8) next.password = "Minimum 8 characters";
    setErrors(next);
    return Object.keys(next).length === 0;
  };

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    if (!validate()) return;
    try {
      await createUser({
        username: form.username.trim(),
        email: form.email.trim(),
        password: form.password,
        full_name: form.full_name.trim() || null,
        role: form.role,
      });
      success("User created", `${form.username} has been added as ${form.role}.`);
      handleClose();
    } catch (err) {
      toastError("Failed to create user", extractApiError(err));
    }
  };

  const field = (key: keyof typeof form) => ({
    value: form[key],
    onChange: (e: ChangeEvent<HTMLInputElement | HTMLSelectElement>) => {
      setForm((f) => ({ ...f, [key]: e.target.value }));
      if (errors[key]) setErrors((prev) => ({ ...prev, [key]: undefined }));
    },
  });

  const inputCls = (hasError: boolean) =>
    cn(
      "block w-full rounded-lg border px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400",
      "outline-none transition-[border-color,box-shadow] duration-150",
      hasError
        ? "border-red-300 focus:border-red-400 focus:ring-2 focus:ring-red-100"
        : "border-slate-200 bg-slate-50 focus:border-slate-400 focus:bg-white focus:ring-2 focus:ring-slate-200",
    );

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm"
        onClick={handleClose}
      />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          className="relative w-full max-w-md rounded-2xl bg-white shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
            <div>
              <h2 className="text-base font-semibold text-slate-900">Create New User</h2>
              <p className="mt-0.5 text-xs text-slate-500">
                The user will be able to sign in immediately.
              </p>
            </div>
            <button
              type="button"
              onClick={handleClose}
              className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          {/* Form */}
          <form onSubmit={handleSubmit} noValidate className="space-y-4 px-6 py-5">
            {/* Username */}
            <div className="space-y-1">
              <label htmlFor="cu-username" className="block text-xs font-medium text-slate-700">
                Username <span className="text-red-500">*</span>
              </label>
              <input
                id="cu-username"
                type="text"
                autoComplete="off"
                autoCapitalize="none"
                placeholder="e.g. jane_doe"
                className={inputCls(!!errors.username)}
                disabled={isPending}
                aria-invalid={!!errors.username}
                {...field("username")}
              />
              {errors.username && (
                <p className="text-[11px] text-red-600">{errors.username}</p>
              )}
            </div>

            {/* Email */}
            <div className="space-y-1">
              <label htmlFor="cu-email" className="block text-xs font-medium text-slate-700">
                Email <span className="text-red-500">*</span>
              </label>
              <input
                id="cu-email"
                type="email"
                autoComplete="off"
                placeholder="jane@cortex.ai"
                className={inputCls(!!errors.email)}
                disabled={isPending}
                aria-invalid={!!errors.email}
                {...field("email")}
              />
              {errors.email && (
                <p className="text-[11px] text-red-600">{errors.email}</p>
              )}
            </div>

            {/* Full name */}
            <div className="space-y-1">
              <label htmlFor="cu-fullname" className="block text-xs font-medium text-slate-700">
                Full name <span className="text-slate-400">(optional)</span>
              </label>
              <input
                id="cu-fullname"
                type="text"
                placeholder="Jane Doe"
                className={inputCls(false)}
                disabled={isPending}
                {...field("full_name")}
              />
            </div>

            {/* Two-column: password + role */}
            <div className="grid grid-cols-2 gap-3">
              {/* Temporary password */}
              <div className="space-y-1">
                <label htmlFor="cu-password" className="block text-xs font-medium text-slate-700">
                  Temp. password <span className="text-red-500">*</span>
                </label>
                <div className="relative">
                  <input
                    id="cu-password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="new-password"
                    placeholder="Min. 8 chars"
                    className={cn(inputCls(!!errors.password), "pr-8")}
                    disabled={isPending}
                    aria-invalid={!!errors.password}
                    {...field("password")}
                  />
                  <button
                    type="button"
                    tabIndex={-1}
                    onClick={() => setShowPassword((v) => !v)}
                    className="absolute inset-y-0 right-0 flex items-center px-2 text-slate-400 hover:text-slate-600"
                    aria-label={showPassword ? "Hide" : "Show"}
                  >
                    {showPassword ? (
                      <EyeOff className="h-3.5 w-3.5" />
                    ) : (
                      <Eye className="h-3.5 w-3.5" />
                    )}
                  </button>
                </div>
                {errors.password && (
                  <p className="text-[11px] text-red-600">{errors.password}</p>
                )}
              </div>

              {/* Role */}
              <div className="space-y-1">
                <label htmlFor="cu-role" className="block text-xs font-medium text-slate-700">
                  Role
                </label>
                <select
                  id="cu-role"
                  className={cn(inputCls(false), "cursor-pointer")}
                  disabled={isPending}
                  {...field("role")}
                >
                  {ROLE_OPTIONS.map(({ value, label }) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {/* Footer */}
            <div className="flex justify-end gap-2 border-t border-slate-100 pt-4">
              <button
                type="button"
                onClick={handleClose}
                disabled={isPending}
                className="rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={isPending}
                className="flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-slate-800 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2"
              >
                {isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                Create user
              </button>
            </div>
          </form>
        </div>
      </div>
    </>
  );
}

// ── Delete Confirmation Modal ──────────────────────────────────────────────────

function DeleteUserModal({
  user,
  onClose,
}: {
  user: AdminUser | null;
  onClose: () => void;
}) {
  const { mutateAsync: deleteUser, isPending } = useDeleteUser();
  const { success, error: toastError } = useToast();

  const handleConfirm = async () => {
    if (!user) return;
    try {
      await deleteUser(user.id);
      success("User deleted", `${user.username} has been permanently removed.`);
      onClose();
    } catch (err) {
      toastError("Failed to delete user", extractApiError(err));
    }
  };

  if (!user) return null;

  return (
    <>
      <div className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm" onClick={onClose} />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div
          className="relative w-full max-w-sm rounded-2xl bg-white p-6 shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-full bg-red-50">
            <Trash2 className="h-5 w-5 text-red-600" />
          </div>
          <h3 className="text-base font-semibold text-slate-900">Delete user?</h3>
          <p className="mt-2 text-sm text-slate-500">
            <span className="font-semibold text-slate-700">{user.username}</span> and all
            associated data — portfolios, watchlists, trade outcomes — will be permanently
            deleted. This cannot be undone.
          </p>
          <div className="mt-6 flex justify-end gap-2">
            <button
              type="button"
              onClick={onClose}
              disabled={isPending}
              className="rounded-lg border border-slate-200 px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50 transition-colors"
            >
              Cancel
            </button>
            <button
              type="button"
              onClick={handleConfirm}
              disabled={isPending}
              className="flex items-center gap-2 rounded-lg bg-red-600 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-red-700 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-500 focus-visible:ring-offset-2"
            >
              {isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              Delete permanently
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

// ── User Table Row ─────────────────────────────────────────────────────────────

function UserRow({
  user,
  isSelf,
  onDelete,
}: {
  user: AdminUser;
  isSelf: boolean;
  onDelete: (user: AdminUser) => void;
}) {
  const { mutateAsync: updateUser } = useUpdateUser();
  const { success, error: toastError } = useToast();
  const [rolePending, setRolePending] = useState(false);
  const [statusPending, setStatusPending] = useState(false);

  const handleRoleChange = async (newRole: UserRole) => {
    if (newRole === user.role) return;
    setRolePending(true);
    try {
      await updateUser({ id: user.id, role: newRole });
      success("Role updated", `${user.username} is now ${newRole}.`);
    } catch (err) {
      toastError("Role change failed", extractApiError(err));
    } finally {
      setRolePending(false);
    }
  };

  const handleStatusToggle = async (newActive: boolean) => {
    setStatusPending(true);
    try {
      await updateUser({ id: user.id, is_active: newActive });
      success(
        newActive ? "User activated" : "User deactivated",
        `${user.username} has been ${newActive ? "re-activated" : "deactivated"}. All sessions revoked.`,
      );
    } catch (err) {
      toastError("Status change failed", extractApiError(err));
    } finally {
      setStatusPending(false);
    }
  };

  return (
    <tr className="group border-b border-slate-100 last:border-0 hover:bg-slate-50/60 transition-colors">
      {/* User identity */}
      <td className="py-3 pl-4 pr-3">
        <div className="flex items-center gap-3">
          <span
            className={cn(
              "flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-full text-[13px] font-bold",
              user.role === "admin"
                ? "bg-rose-100 text-rose-700"
                : user.role === "trader"
                ? "bg-blue-100 text-blue-700"
                : "bg-slate-100 text-slate-600",
            )}
            aria-hidden="true"
          >
            {avatarInitial(user)}
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5">
              <p className="text-sm font-semibold text-slate-900">{user.username}</p>
              {isSelf && (
                <span className="rounded bg-slate-100 px-1.5 py-px text-[9px] font-bold uppercase tracking-wider text-slate-500">
                  you
                </span>
              )}
            </div>
            <p className="truncate text-[11px] text-slate-500">{user.email}</p>
          </div>
        </div>
      </td>

      {/* Full name */}
      <td className="px-3 py-3">
        <span className="text-sm text-slate-600">{user.full_name ?? "—"}</span>
      </td>

      {/* Role (inline select) */}
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          {rolePending ? (
            <Loader2 className="h-4 w-4 animate-spin text-slate-400" />
          ) : (
            <RoleBadge role={user.role} />
          )}
          <select
            value={user.role}
            onChange={(e) => handleRoleChange(e.target.value as UserRole)}
            disabled={rolePending || isSelf}
            title={isSelf ? "You cannot change your own role" : "Change role"}
            className={cn(
              "rounded-md border border-transparent bg-transparent text-xs text-slate-500 cursor-pointer",
              "px-1 py-0.5 transition-colors focus:outline-none focus:ring-2 focus:ring-slate-300",
              "hover:border-slate-200 hover:bg-white",
              (rolePending || isSelf) && "cursor-not-allowed opacity-40",
            )}
            aria-label={`Role for ${user.username}`}
          >
            {ROLE_OPTIONS.map(({ value, label }) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
      </td>

      {/* Status toggle */}
      <td className="px-3 py-3">
        <div className="flex items-center gap-2">
          <StatusToggle
            checked={user.is_active}
            onChange={handleStatusToggle}
            disabled={isSelf}
            pending={statusPending}
          />
          <span className={cn("text-xs", user.is_active ? "text-emerald-600" : "text-slate-400")}>
            {user.is_active ? "Active" : "Inactive"}
          </span>
        </div>
      </td>

      {/* Last login */}
      <td className="px-3 py-3">
        <span className="text-xs text-slate-500">{fmtDate(user.last_login)}</span>
      </td>

      {/* Member since */}
      <td className="px-3 py-3">
        <span className="text-xs text-slate-500">{fmtDate(user.created_at)}</span>
      </td>

      {/* Actions */}
      <td className="py-3 pl-3 pr-4">
        <button
          type="button"
          onClick={() => onDelete(user)}
          disabled={isSelf}
          title={isSelf ? "Cannot delete your own account" : `Delete ${user.username}`}
          className={cn(
            "rounded-md p-1.5 text-slate-400 transition-colors",
            "hover:bg-red-50 hover:text-red-600",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-400",
            "opacity-0 group-hover:opacity-100",
            isSelf && "pointer-events-none opacity-0",
          )}
          aria-label={`Delete ${user.username}`}
        >
          <Trash2 className="h-4 w-4" />
        </button>
      </td>
    </tr>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function UsersPage() {
  const { user: currentUser } = useAuth();
  const { success, error: toastError } = useToast();

  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [userToDelete, setUserToDelete] = useState<AdminUser | null>(null);
  const debounceRef = useRef<NodeJS.Timeout | null>(null);

  const { data, isLoading, isError, refetch, isRefetching } = useAdminUsers(
    debouncedSearch || undefined,
  );

  // Debounce search input (300 ms)
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => setDebouncedSearch(search), 300);
    return () => { if (debounceRef.current) clearTimeout(debounceRef.current); };
  }, [search]);

  const users = data?.users ?? [];
  const total = data?.total ?? 0;

  const stats = {
    total,
    admins: users.filter((u) => u.role === "admin").length,
    traders: users.filter((u) => u.role === "trader").length,
    viewers: users.filter((u) => u.role === "viewer").length,
    inactive: users.filter((u) => !u.is_active).length,
  };

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">
            User Management
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Create accounts, assign roles, and control platform access.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowCreateModal(true)}
          className="flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2"
        >
          <Plus className="h-4 w-4" />
          Add user
        </button>
      </div>

      {/* Stats bar */}
      {!isLoading && !isError && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            { label: "Total users", value: stats.total, icon: Users, color: "text-slate-600 bg-slate-100" },
            { label: "Admins", value: stats.admins, icon: ShieldCheck, color: "text-rose-600 bg-rose-50" },
            { label: "Traders", value: stats.traders, icon: Briefcase, color: "text-blue-600 bg-blue-50" },
            { label: "Viewers", value: stats.viewers, icon: Eye, color: "text-slate-500 bg-slate-100" },
          ].map(({ label, value, icon: Icon, color }) => (
            <div
              key={label}
              className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3"
            >
              <div className={cn("flex h-8 w-8 items-center justify-center rounded-lg", color)}>
                <Icon className="h-4 w-4" />
              </div>
              <div>
                <p className="text-xl font-bold text-slate-900">{value}</p>
                <p className="text-[11px] text-slate-500">{label}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Search + refresh */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1 max-w-xs">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search by username or email…"
            className="block w-full rounded-lg border border-slate-200 bg-slate-50 py-2 pl-9 pr-3 text-sm text-slate-900 placeholder:text-slate-400 outline-none transition focus:border-slate-400 focus:bg-white focus:ring-2 focus:ring-slate-200"
          />
        </div>
        <button
          type="button"
          onClick={() => refetch()}
          disabled={isRefetching}
          title="Refresh"
          className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-600 transition-colors hover:bg-slate-50 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
        >
          <RefreshCw className={cn("h-4 w-4", isRefetching && "animate-spin")} />
          <span className="hidden sm:inline">Refresh</span>
        </button>
      </div>

      {/* Table */}
      <div className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
        {isLoading ? (
          <div className="flex items-center justify-center gap-3 py-16 text-slate-400">
            <Loader2 className="h-5 w-5 animate-spin" />
            <span className="text-sm">Loading users…</span>
          </div>
        ) : isError ? (
          <div className="flex flex-col items-center gap-3 py-16 text-center">
            <AlertCircle className="h-8 w-8 text-red-400" />
            <p className="text-sm font-medium text-slate-700">Failed to load users</p>
            <button
              type="button"
              onClick={() => refetch()}
              className="text-sm text-blue-600 underline hover:no-underline"
            >
              Try again
            </button>
          </div>
        ) : users.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-16 text-center">
            <Users className="h-8 w-8 text-slate-300" />
            <p className="text-sm font-medium text-slate-500">
              {debouncedSearch ? "No users match your search" : "No users found"}
            </p>
          </div>
        ) : (
          <table className="w-full text-left">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50/60">
                {["User", "Full name", "Role", "Status", "Last login", "Member since", ""].map(
                  (col) => (
                    <th
                      key={col}
                      className="px-3 py-2.5 text-[11px] font-semibold uppercase tracking-wider text-slate-500 first:pl-4 last:pr-4"
                    >
                      {col}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <UserRow
                  key={user.id}
                  user={user}
                  isSelf={user.id === currentUser?.id}
                  onDelete={setUserToDelete}
                />
              ))}
            </tbody>
          </table>
        )}

        {/* Footer row */}
        {!isLoading && !isError && users.length > 0 && (
          <div className="border-t border-slate-100 bg-slate-50/40 px-4 py-2.5">
            <p className="text-[11px] text-slate-400">
              {debouncedSearch
                ? `${users.length} result${users.length !== 1 ? "s" : ""} for "${debouncedSearch}"`
                : `${total} user${total !== 1 ? "s" : ""} total`}
              {stats.inactive > 0 && ` · ${stats.inactive} inactive`}
            </p>
          </div>
        )}
      </div>

      {/* Modals */}
      <CreateUserModal open={showCreateModal} onClose={() => setShowCreateModal(false)} />
      <DeleteUserModal user={userToDelete} onClose={() => setUserToDelete(null)} />
    </div>
  );
}
