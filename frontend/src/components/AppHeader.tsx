'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { LogOut } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { AdminNavLink } from '@/components/admin/AdminNavLink';
import { cn } from '@/lib/utils';

const NAV_LINKS = [
  { href: '/', label: 'Dashboard' },
  { href: '/hawk-eye-radar', label: 'Hawk-Eye Radar' },
  { href: '/scanner', label: 'Scanner' },
  { href: '/cortex-ai', label: 'Cortex AI' },
] as const;

function CortexLogo() {
  return (
    <svg className="h-5 w-5" viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M10 2L3 6v4l7 4 7-4V6L10 2z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M3 14l7 4 7-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path
        d="M3 10l7 4 7-4"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function UserBadge() {
  const { user, role, isAdmin, logout } = useAuth();
  const router = useRouter();

  const displayName = user?.username ?? role;
  const initial = (user?.username?.[0] ?? role[0]).toUpperCase();

  const handleLogout = async () => {
    await logout();
    router.push('/login');
  };

  return (
    <div className="flex items-center gap-2">
      {/* Avatar + name */}
      <div className="flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 pr-3">
        <span
          className="flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full bg-slate-800 text-[10px] font-bold text-white"
          aria-hidden="true"
        >
          {initial}
        </span>
        <span className="text-xs font-medium text-slate-700">{displayName}</span>
        {isAdmin && (
          <span className="rounded bg-rose-50 px-1.5 py-px text-[9px] font-bold uppercase tracking-wider text-rose-600">
            Admin
          </span>
        )}
      </div>

      {/* Sign out */}
      <button
        onClick={handleLogout}
        className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-xs font-medium text-slate-500 transition-colors hover:bg-slate-100 hover:text-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-1"
        title="Sign out"
        aria-label="Sign out"
      >
        <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
        <span className="hidden sm:inline">Sign out</span>
      </button>
    </div>
  );
}

export function AppHeader() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const pathname = usePathname();

  return (
    <header className="sticky top-0 z-40 border-b border-slate-200/80 bg-white/80 backdrop-blur-sm">
      <div className="mx-auto flex h-14 w-full max-w-7xl items-center justify-between px-4">
        {/* Logo */}
        <Link
          href="/"
          className="flex items-center gap-2.5 text-slate-900 transition-opacity hover:opacity-80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2 rounded-sm"
          aria-label="Cortex Terminal — home"
        >
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-slate-900 text-white">
            <CortexLogo />
          </span>
          <span className="text-sm font-semibold tracking-tight">Cortex Terminal</span>
          <span className="hidden rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500 sm:inline">
            v1
          </span>
        </Link>

        {/* Primary nav — only when authenticated */}
        {isAuthReady && isAuthenticated && (
          <nav
            className="hidden items-center gap-0.5 md:flex"
            aria-label="Primary navigation"
          >
            {NAV_LINKS.map(({ href, label }) => {
              const isActive =
                href === '/' ? pathname === '/' : pathname.startsWith(href);
              return (
                <Link
                  key={href}
                  href={href}
                  className={cn(
                    'rounded-md px-3 py-1.5 text-sm font-medium transition-colors',
                    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-1',
                    isActive
                      ? 'bg-slate-100 text-slate-900'
                      : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900',
                  )}
                  aria-current={isActive ? 'page' : undefined}
                >
                  {label}
                </Link>
              );
            })}
            <AdminNavLink />
          </nav>
        )}

        {/* Right: loading skeleton or user badge */}
        <div className="flex items-center">
          {!isAuthReady && (
            <div className="h-7 w-36 animate-pulse rounded-full bg-slate-100" aria-hidden="true" />
          )}
          {isAuthReady && isAuthenticated && <UserBadge />}
        </div>
      </div>
    </header>
  );
}
