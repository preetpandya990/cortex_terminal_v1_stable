'use client';

import { usePathname } from 'next/navigation';
import { AppHeader } from '@/components/AppHeader';

// Paths that render their own full-screen layout — no nav, no page wrapper
const STANDALONE_PATHS = new Set(['/login']);

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (STANDALONE_PATHS.has(pathname)) {
    return <>{children}</>;
  }

  return (
    <div className="min-h-screen bg-[radial-gradient(1200px_400px_at_top,_#f1f5f9,_transparent)]">
      <AppHeader />
      <main className="mx-auto w-full max-w-7xl px-4 py-10">{children}</main>
    </div>
  );
}
