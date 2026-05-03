'use client';

import { useState, useEffect, type FormEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Eye, EyeOff, AlertCircle, Loader2, Activity, BarChart2, TrendingUp, ScanLine } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { cn } from '@/lib/utils';

// ── Types ──────────────────────────────────────────────────────────────────────

interface Feature {
  icon: React.ElementType;
  title: string;
  description: string;
}

// ── Constants ──────────────────────────────────────────────────────────────────

const FEATURES: Feature[] = [
  {
    icon: Activity,
    title: 'Hawk-Eye Radar',
    description: 'Real-time AI signal generation across 2,551 NSE instruments.',
  },
  {
    icon: BarChart2,
    title: 'ML Ensemble',
    description: 'XGBoost + GRU predictions with walk-forward validation.',
  },
  {
    icon: TrendingUp,
    title: 'Paper Trading',
    description: 'Live P&L simulation with 500 ms real-time market refresh.',
  },
  {
    icon: ScanLine,
    title: 'Market Scanner',
    description: 'Multi-factor regime detection and momentum analysis.',
  },
];

// Read the ?next= redirect target safely on the client side.
// Avoids useSearchParams (which requires a Suspense boundary) while keeping
// the same behaviour.
function getRedirectTarget(): string {
  if (typeof window === 'undefined') return '/';
  try {
    const next = new URLSearchParams(window.location.search).get('next');
    // Reject open redirects — only allow same-origin paths
    if (next && next.startsWith('/') && !next.startsWith('//')) return next;
  } catch { /* ignore */ }
  return '/';
}

// ── Brand panel (left side) ────────────────────────────────────────────────────

function BrandPanel() {
  return (
    <div className="relative hidden lg:flex lg:w-[60%] flex-col justify-between overflow-hidden bg-[#050d1a] px-12 py-10 xl:px-16 xl:py-12">

      {/* Dot-grid texture */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0"
        style={{
          backgroundImage: 'radial-gradient(rgba(255,255,255,0.055) 1px, transparent 1px)',
          backgroundSize: '22px 22px',
        }}
      />

      {/* Ambient glow */}
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 left-1/2 -translate-x-1/2 h-[560px] w-[720px] rounded-full bg-blue-600/[0.09] blur-3xl"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute bottom-0 right-0 h-[300px] w-[400px] rounded-full bg-indigo-600/[0.06] blur-3xl"
      />

      {/* Logo lockup */}
      <div className="relative flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-blue-500/20 bg-blue-500/10">
          <CortexMark className="h-5 w-5 text-blue-400" />
        </div>
        <div>
          <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-white">Cortex</p>
          <p className="text-[9px] font-medium uppercase tracking-[0.28em] text-slate-500">
            Terminal · v1
          </p>
        </div>
      </div>

      {/* Hero copy + feature grid */}
      <div className="relative space-y-8">
        <div>
          <h1 className="text-[2.6rem] font-bold leading-[1.15] tracking-tight text-white xl:text-[3rem]">
            AI-native trading<br />
            intelligence for<br />
            <span className="text-blue-400">NSE markets.</span>
          </h1>
          <p className="mt-4 max-w-md text-[15px] leading-relaxed text-slate-400">
            Institutional-grade signal generation, ML ensemble predictions, and
            real-time portfolio simulation — purpose-built for professional traders.
          </p>
        </div>

        <div className="grid grid-cols-2 gap-3">
          {FEATURES.map(({ icon: Icon, title, description }) => (
            <div
              key={title}
              className="flex gap-3 rounded-xl border border-white/[0.06] bg-white/[0.03] p-4 backdrop-blur-sm"
            >
              <div className="mt-0.5 flex h-8 w-8 flex-shrink-0 items-center justify-center rounded-lg bg-blue-500/10">
                <Icon className="h-4 w-4 text-blue-400" aria-hidden="true" />
              </div>
              <div>
                <p className="text-[13px] font-semibold text-white">{title}</p>
                <p className="mt-0.5 text-[11px] leading-relaxed text-slate-500">
                  {description}
                </p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Footer */}
      <div className="relative flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden="true" />
          <span className="text-[11px] text-slate-500">All systems operational</span>
        </div>
        <span className="text-[11px] text-slate-600">
          © {new Date().getFullYear()} Cortex AI
        </span>
      </div>
    </div>
  );
}

// ── Login form (right side) ────────────────────────────────────────────────────

function LoginForm() {
  const { login, isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Redirect away if already authenticated (e.g. back-button after login)
  useEffect(() => {
    if (isAuthReady && isAuthenticated) {
      router.replace(getRedirectTarget());
    }
  }, [isAuthReady, isAuthenticated, router]);

  const handleSubmit = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!identifier.trim() || !password) return;

    setError(null);
    setIsLoading(true);

    try {
      const response = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ identifier: identifier.trim(), password }),
      });

      const data = await response.json();

      if (!response.ok) {
        setError(data.error ?? 'Invalid credentials. Please try again.');
        return;
      }

      login(data.access_token);
      router.replace(getRedirectTarget());
    } catch {
      setError('Unable to reach the server. Check your connection and try again.');
    } finally {
      setIsLoading(false);
    }
  };

  const handleDevLogin = async () => {
    setError(null);
    setIsLoading(true);
    try {
      const response = await fetch('/api/auth/dev-login', {
        method: 'POST',
        credentials: 'include',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? 'Dev login failed');
      login(data.access_token);
      router.replace('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Dev login failed.');
    } finally {
      setIsLoading(false);
    }
  };

  const hasError = error !== null;
  const inputBase =
    'block w-full rounded-lg border px-3.5 py-2.5 text-sm text-slate-900 placeholder:text-slate-400 outline-none transition-[border-color,box-shadow] duration-150';
  const inputNormal =
    'border-slate-200 bg-slate-50 focus:border-slate-400 focus:bg-white focus:ring-2 focus:ring-slate-200';
  const inputError =
    'border-red-300 bg-red-50/40 focus:border-red-400 focus:bg-white focus:ring-2 focus:ring-red-100';

  return (
    <div className="flex flex-1 flex-col items-center justify-center bg-white px-6 py-12 sm:px-10">

      {/* Mobile logo */}
      <div className="mb-8 flex items-center gap-2.5 lg:hidden">
        <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-900 text-white">
          <CortexMark className="h-4 w-4" />
        </span>
        <span className="text-[15px] font-semibold tracking-tight text-slate-900">
          Cortex Terminal
        </span>
      </div>

      <div className="w-full max-w-sm">

        {/* Heading */}
        <div className="mb-8">
          <h2 className="text-[1.6rem] font-semibold tracking-tight text-slate-900">
            Sign in
          </h2>
          <p className="mt-1.5 text-sm text-slate-500">Access your trading terminal</p>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="space-y-4" noValidate aria-label="Sign-in form">

          {/* Identifier field */}
          <div className="space-y-1.5">
            <label htmlFor="identifier" className="block text-sm font-medium text-slate-700">
              Username or email
            </label>
            <input
              id="identifier"
              name="identifier"
              type="text"
              autoComplete="username"
              autoCapitalize="none"
              autoCorrect="off"
              spellCheck="false"
              required
              value={identifier}
              onChange={(e) => { setIdentifier(e.target.value); setError(null); }}
              className={cn(inputBase, hasError ? inputError : inputNormal)}
              placeholder="trader or trader@cortex.ai"
              disabled={isLoading}
              aria-describedby={hasError ? 'login-error' : undefined}
              aria-invalid={hasError}
            />
          </div>

          {/* Password field */}
          <div className="space-y-1.5">
            <label htmlFor="password" className="block text-sm font-medium text-slate-700">
              Password
            </label>
            <div className="relative">
              <input
                id="password"
                name="password"
                type={showPassword ? 'text' : 'password'}
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => { setPassword(e.target.value); setError(null); }}
                className={cn(inputBase, 'pr-10', hasError ? inputError : inputNormal)}
                placeholder="••••••••"
                disabled={isLoading}
                aria-describedby={hasError ? 'login-error' : undefined}
                aria-invalid={hasError}
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                tabIndex={-1}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
                className="absolute inset-y-0 right-0 flex items-center px-3 text-slate-400 transition-colors hover:text-slate-600 focus-visible:outline-none"
              >
                {showPassword
                  ? <EyeOff className="h-4 w-4" aria-hidden="true" />
                  : <Eye className="h-4 w-4" aria-hidden="true" />
                }
              </button>
            </div>
          </div>

          {/* Error banner */}
          {hasError && (
            <div
              id="login-error"
              role="alert"
              aria-live="assertive"
              className="flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 px-3.5 py-3"
            >
              <AlertCircle className="mt-px h-4 w-4 flex-shrink-0 text-red-500" aria-hidden="true" />
              <p className="text-sm text-red-700">{error}</p>
            </div>
          )}

          {/* Submit */}
          <button
            type="submit"
            disabled={isLoading || !identifier.trim() || !password}
            className={cn(
              'flex w-full items-center justify-center gap-2 rounded-lg px-4 py-2.5',
              'bg-slate-900 text-sm font-semibold text-white',
              'transition-colors hover:bg-slate-800 active:bg-slate-950',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-900 focus-visible:ring-offset-2',
              'disabled:cursor-not-allowed disabled:opacity-50',
            )}
          >
            {isLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                Signing in…
              </>
            ) : (
              'Sign in'
            )}
          </button>
        </form>

        {/* Development bypass — stripped from production builds */}
        {process.env.NODE_ENV === 'development' && (
          <div className="mt-6 border-t border-slate-100 pt-5">
            <button
              type="button"
              onClick={handleDevLogin}
              disabled={isLoading}
              className="w-full text-center text-[11px] text-slate-400 transition-colors hover:text-slate-600 disabled:pointer-events-none disabled:opacity-50"
            >
              Development bypass — sign in as trader (admin)
            </button>
          </div>
        )}

        {/* Access notice */}
        <p className="mt-10 text-center text-[11px] leading-relaxed text-slate-400">
          Access is restricted to authorised personnel only.
          <br />
          Contact your administrator to request access.
        </p>
      </div>
    </div>
  );
}

// ── Shared mark ────────────────────────────────────────────────────────────────

function CortexMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M10 2L3 6v4l7 4 7-4V6L10 2z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
      />
      <path d="M3 14l7 4 7-4" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <path d="M3 10l7 4 7-4" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────

export default function LoginPage() {
  return (
    <div className="flex min-h-screen">
      <BrandPanel />
      <LoginForm />
    </div>
  );
}
