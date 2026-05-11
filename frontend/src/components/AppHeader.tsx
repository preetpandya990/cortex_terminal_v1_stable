'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { ChevronDown, LogOut, Settings, Workflow } from 'lucide-react';
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
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const displayName = user?.username ?? role;
  const initial = (user?.username?.[0] ?? role[0]).toUpperCase();

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') { setOpen(false); triggerRef.current?.focus(); } };
    const onClick = (e: MouseEvent) => {
      if (!menuRef.current?.contains(e.target as Node) && !triggerRef.current?.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onClick);
    return () => { document.removeEventListener('keydown', onKey); document.removeEventListener('mousedown', onClick); };
  }, [open]);

  // Trap focus inside menu when open
  const handleMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (!open) return;
    const items = menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
    if (!items?.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.key === 'ArrowDown') { e.preventDefault(); (document.activeElement === last ? first : (document.activeElement as HTMLElement)?.nextElementSibling as HTMLElement ?? first)?.focus(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); (document.activeElement === first ? last : (document.activeElement as HTMLElement)?.previousElementSibling as HTMLElement ?? last)?.focus(); }
    if (e.key === 'Tab') setOpen(false);
  };

  const handleLogout = async () => {
    setOpen(false);
    await logout();
    router.push('/login');
  };

  return (
    <div className="relative">
      {/* Avatar trigger button */}
      <button
        ref={triggerRef}
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="true"
        aria-expanded={open}
        aria-label={`User menu for ${displayName}`}
        className="flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 pr-2.5 transition-colors hover:bg-slate-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-1"
      >
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
        <ChevronDown
          className={cn('h-3 w-3 text-slate-400 transition-transform duration-150', open && 'rotate-180')}
          aria-hidden="true"
        />
      </button>

      {/* Dropdown menu */}
      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label="User menu"
          onKeyDown={handleMenuKeyDown}
          className={cn(
            'absolute right-0 top-full z-50 mt-1.5 w-44 overflow-hidden',
            'rounded-lg border border-slate-200 bg-white shadow-lg shadow-slate-200/60',
            'animate-in fade-in-0 zoom-in-95 duration-100',
          )}
        >
          <div className="py-1">
            <Link
              href="/settings"
              role="menuitem"
              tabIndex={0}
              onClick={() => setOpen(false)}
              className="flex items-center gap-2.5 px-3.5 py-2 text-sm text-slate-700 transition-colors hover:bg-slate-50 focus-visible:bg-slate-50 focus-visible:outline-none"
            >
              <Settings className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
              Profile &amp; Settings
            </Link>
            {!isAdmin && (
              <Link
                href="/strategies"
                role="menuitem"
                tabIndex={0}
                onClick={() => setOpen(false)}
                className="flex items-center gap-2.5 px-3.5 py-2 text-sm text-slate-700 transition-colors hover:bg-slate-50 focus-visible:bg-slate-50 focus-visible:outline-none"
              >
                <Workflow className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                My Strategies
              </Link>
            )}
          </div>
          <div className="border-t border-slate-100 py-1">
            <button
              role="menuitem"
              tabIndex={0}
              onClick={handleLogout}
              className="flex w-full items-center gap-2.5 px-3.5 py-2 text-sm text-rose-600 transition-colors hover:bg-rose-50 focus-visible:bg-rose-50 focus-visible:outline-none"
            >
              <LogOut className="h-3.5 w-3.5" aria-hidden="true" />
              Sign out
            </button>
          </div>
        </div>
      )}
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
