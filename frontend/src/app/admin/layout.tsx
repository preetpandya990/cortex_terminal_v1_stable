"use client";

import { useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import Link from "next/link";
import { BarChart3, Brain, ShieldAlert } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const NAV_ITEMS = [
  {
    href: "/admin/audit",
    label: "Trade Audit",
    icon: BarChart3,
    description: "ML feedback & outcome analysis",
  },
  {
    href: "/admin/governance",
    label: "ML Governance",
    icon: Brain,
    description: "Model registry & deployment",
  },
];

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { isAuthReady, isAuthenticated, isAdmin } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isAuthReady) return;
    if (!isAuthenticated || !isAdmin) {
      router.replace("/");
    }
  }, [isAuthReady, isAuthenticated, isAdmin, router]);

  // Render nothing until auth is resolved to prevent flash
  if (!isAuthReady || !isAuthenticated || !isAdmin) return null;

  return (
    <div className="flex min-h-[calc(100vh-73px)] gap-6">
      {/* Sidebar */}
      <aside className="w-56 flex-shrink-0">
        <div className="sticky top-6 rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
          <div className="mb-3 flex items-center gap-2 px-2 py-1">
            <ShieldAlert className="h-4 w-4 text-rose-500" />
            <span className="text-xs font-bold uppercase tracking-widest text-slate-500">
              Admin
            </span>
          </div>
          <nav className="space-y-0.5">
            {NAV_ITEMS.map(({ href, label, icon: Icon, description }) => {
              const active = pathname === href || pathname.startsWith(href + "/");
              return (
                <Link
                  key={href}
                  href={href}
                  className={`group flex items-start gap-3 rounded-xl px-3 py-2.5 transition-colors ${
                    active
                      ? "bg-blue-50 text-blue-700"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`}
                >
                  <Icon
                    className={`mt-0.5 h-4 w-4 flex-shrink-0 ${
                      active ? "text-blue-600" : "text-slate-400 group-hover:text-slate-600"
                    }`}
                  />
                  <div>
                    <div className={`text-sm font-semibold leading-tight ${active ? "text-blue-700" : ""}`}>
                      {label}
                    </div>
                    <div className="mt-0.5 text-[10px] leading-tight text-slate-400">
                      {description}
                    </div>
                  </div>
                </Link>
              );
            })}
          </nav>
        </div>
      </aside>

      {/* Page content */}
      <main className="min-w-0 flex-1">{children}</main>
    </div>
  );
}
