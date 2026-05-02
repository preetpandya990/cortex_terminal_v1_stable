"use client";

import Link from "next/link";
import { ShieldAlert } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Renders the Admin nav link only for users with admin role.
 * Placed in the root layout — renders nothing for non-admins.
 */
export function AdminNavLink() {
  const { isAdmin, isAuthReady } = useAuth();

  if (!isAuthReady || !isAdmin) return null;

  return (
    <Link
      href="/admin/audit"
      className="inline-flex items-center gap-1.5 rounded-full bg-rose-50 px-3 py-1 text-sm font-semibold text-rose-600 transition-colors hover:bg-rose-100 hover:text-rose-700"
    >
      <ShieldAlert className="h-3.5 w-3.5" />
      Admin
    </Link>
  );
}
