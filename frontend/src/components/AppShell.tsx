'use client';

import { useEffect } from 'react';
import { usePathname, useRouter } from 'next/navigation';
import { AppHeader } from '@/components/AppHeader';
import { useAuth } from '@/contexts/AuthContext';

// Paths that render their own full-screen layout — no nav, no auth guard
const STANDALONE_PATHS = new Set(['/login']);

function PageSkeleton() {
  return (
    <div className="animate-pulse space-y-6 pt-2" aria-hidden="true">
      <div className="space-y-2">
        <div className="h-7 w-52 rounded-lg bg-slate-100" />
        <div className="h-4 w-80 rounded bg-slate-100" />
      </div>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        {[1, 2, 3].map((i) => (
          <div key={i} className="h-28 rounded-xl bg-slate-100" />
        ))}
      </div>
      <div className="h-64 rounded-2xl bg-slate-100" />
    </div>
  );
}

function AuthGuard({ children }: { children: React.ReactNode }) {
  const { isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (!isAuthReady || isAuthenticated) return;
    const next = pathname !== '/' ? `?next=${encodeURIComponent(pathname)}` : '';
    router.replace(`/login${next}`);
  }, [isAuthReady, isAuthenticated, pathname, router]);

  if (!isAuthReady) return <PageSkeleton />;
  if (!isAuthenticated) return null;
  return <>{children}</>;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (STANDALONE_PATHS.has(pathname)) {
    return <>{children}</>;
  }

  return (
    <div className="min-h-screen bg-[radial-gradient(1200px_400px_at_top,_#f1f5f9,_transparent)]">
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl px-4 py-10">
        <AuthGuard>{children}</AuthGuard>
      </main>
    </div>
  );
}
