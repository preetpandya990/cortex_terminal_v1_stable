'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Clock,
  TrendingDown,
  TrendingUp,
  X,
  XCircle,
} from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { strategiesAPI } from '@/lib/api';
import { PriceInput } from '@/components/ui/PriceInput';
import type { StrategyActivation, ActivationState } from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Helpers ──────────────────────────────────────────────────────────────────

const STATE_CONFIG: Record<ActivationState, {
  label: string;
  color: string;
  icon: React.ElementType;
}> = {
  WATCHING: { label: 'Watching',     color: 'bg-slate-100 text-slate-600',    icon: Clock },
  IN_SETUP: { label: 'In Setup',     color: 'bg-amber-50 text-amber-700',     icon: Activity },
  IN_TRADE: { label: 'In Trade',     color: 'bg-emerald-50 text-emerald-700', icon: TrendingUp },
  PARTIAL_EXIT: { label: 'Partial',  color: 'bg-blue-50 text-blue-700',       icon: TrendingDown },
  EXITED:   { label: 'Exited',       color: 'bg-slate-100 text-slate-500',    icon: CheckCircle2 },
  CANCELLED:{ label: 'Cancelled',    color: 'bg-red-50 text-red-500',         icon: XCircle },
};

function StateBadge({ state }: { state: ActivationState }) {
  const cfg = STATE_CONFIG[state] ?? STATE_CONFIG.WATCHING;
  const Icon = cfg.icon;
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide', cfg.color)}>
      <Icon className="h-3 w-3" aria-hidden="true" />
      {cfg.label}
    </span>
  );
}

function ExitReasonPill({ reason }: { reason: string | null }) {
  if (!reason) return null;
  const colors: Record<string, string> = {
    SL: 'bg-red-50 text-red-600',
    TP1: 'bg-emerald-50 text-emerald-700',
    TP2: 'bg-emerald-100 text-emerald-800',
    TP3: 'bg-emerald-200 text-emerald-900',
    FULL_TP: 'bg-emerald-200 text-emerald-900',
    MANUAL: 'bg-slate-100 text-slate-600',
    SIGNAL_EXPIRED: 'bg-amber-50 text-amber-700',
    STRATEGY_PAUSED: 'bg-amber-50 text-amber-700',
    RULE_VIOLATED: 'bg-red-50 text-red-600',
  };
  return (
    <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-semibold', colors[reason] ?? 'bg-slate-100 text-slate-600')}>
      {reason.replace('_', ' ')}
    </span>
  );
}

function formatDuration(seconds: number | null): string {
  if (seconds == null || seconds <= 0) return '—';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

function holdDuration(activation: StrategyActivation): string {
  const entryEntry = [...activation.state_history].reverse().find(e => e.state === 'IN_TRADE');
  const entryAt = entryEntry?.at ? new Date(entryEntry.at) : activation.entry_at ? new Date(activation.entry_at) : null;
  if (!entryAt) return '—';
  const exitAt = activation.exit_at ? new Date(activation.exit_at) : new Date();
  return formatDuration(Math.floor((exitAt.getTime() - entryAt.getTime()) / 1000));
}

// ─── Exit modal ───────────────────────────────────────────────────────────────

function ExitModal({
  activation,
  onClose,
  onConfirm,
}: {
  activation: StrategyActivation;
  onClose: () => void;
  onConfirm: (price: number | null) => Promise<void>;
}) {
  const [price, setPrice] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async () => {
    setLoading(true);
    setError('');
    try {
      const parsed = price.trim() ? parseFloat(price) : null;
      if (parsed !== null && (isNaN(parsed) || parsed <= 0)) {
        setError('Enter a valid exit price or leave blank to use market price.');
        return;
      }
      await onConfirm(parsed);
      onClose();
    } catch {
      setError('Failed to exit activation. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Exit Position</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              {activation.symbol} · {activation.state}
            </p>
          </div>
          <button onClick={onClose} className="rounded p-1 text-slate-400 hover:text-slate-700">
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="space-y-3">
          <div>
            <label htmlFor="exit-price" className="mb-1 block text-xs font-medium text-slate-700">
              Exit price{" "}
              <span className="font-normal text-slate-400">(optional — leave blank for market price)</span>
            </label>
            <PriceInput
              id="exit-price"
              value={price}
              onChange={setPrice}
              error={error || undefined}
              focusVariant="neutral"
              className="rounded-md py-2 focus:ring-slate-400"
              placeholder="e.g. 2940.00"
            />
          </div>
        </div>

        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading}
            className="rounded-md bg-rose-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-rose-700 disabled:opacity-50"
          >
            {loading ? 'Exiting…' : 'Exit trade'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Activation row ───────────────────────────────────────────────────────────

function ActivationRow({
  activation,
  onExit,
  onCancel,
}: {
  activation: StrategyActivation;
  onExit: (a: StrategyActivation) => void;
  onCancel: (a: StrategyActivation) => void;
}) {
  const inTradeEntry = [...activation.state_history].reverse().find(e => e.state === 'IN_TRADE');
  const entryPrice = inTradeEntry?.entry_price as number | undefined;
  const slPrice = inTradeEntry?.sl_price as number | undefined;
  const tp1Price = inTradeEntry?.tp1_price as number | undefined;
  const isLive = activation.state === 'IN_TRADE' || activation.state === 'PARTIAL_EXIT';
  const canExit = isLive;
  const canCancel = activation.state === 'WATCHING' || activation.state === 'IN_SETUP';

  return (
    <div className="flex items-center gap-3 rounded-lg border border-slate-200 bg-white px-4 py-3 text-xs transition-shadow hover:shadow-sm">
      {/* Symbol + state */}
      <div className="w-28 shrink-0">
        <p className="font-semibold text-slate-900">{activation.symbol}</p>
        <p className="mt-0.5 text-slate-400">{new Date(activation.created_at).toLocaleDateString()}</p>
      </div>

      <div className="w-24 shrink-0">
        <StateBadge state={activation.state} />
      </div>

      {/* Entry / SL / TP */}
      <div className="flex flex-1 flex-wrap items-center gap-x-4 gap-y-1 text-slate-600">
        {entryPrice != null && (
          <span>Entry <span className="font-medium text-slate-900">₹{entryPrice.toLocaleString()}</span></span>
        )}
        {slPrice != null && (
          <span className="text-red-500">SL <span className="font-medium">₹{slPrice.toLocaleString()}</span></span>
        )}
        {tp1Price != null && (
          <span className="text-emerald-600">TP1 <span className="font-medium">₹{tp1Price.toLocaleString()}</span></span>
        )}
        {activation.auto_execute && (
          <span className="rounded bg-violet-50 px-1.5 py-0.5 text-[10px] font-semibold text-violet-700">Auto</span>
        )}
      </div>

      {/* Duration */}
      <div className="w-12 shrink-0 text-right text-slate-500">
        {holdDuration(activation)}
      </div>

      {/* Exit reason */}
      <div className="w-20 shrink-0 text-right">
        <ExitReasonPill reason={activation.exit_reason} />
      </div>

      {/* Actions */}
      <div className="flex shrink-0 items-center gap-1.5">
        {canExit && (
          <button
            onClick={() => onExit(activation)}
            className="rounded bg-rose-50 px-2 py-1 text-[10px] font-semibold text-rose-600 hover:bg-rose-100"
          >
            Exit
          </button>
        )}
        {canCancel && (
          <button
            onClick={() => onCancel(activation)}
            className="rounded bg-slate-100 px-2 py-1 text-[10px] font-semibold text-slate-600 hover:bg-slate-200"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Filters ──────────────────────────────────────────────────────────────────

type FilterState = 'all' | 'active' | 'terminal';

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function ActivationsPage() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();
  const [activations, setActivations] = useState<StrategyActivation[]>([]);
  const [filter, setFilter] = useState<FilterState>('active');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exitTarget, setExitTarget] = useState<StrategyActivation | null>(null);

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  const load = async () => {
    if (!isAuthenticated) return;
    setLoading(true);
    try {
      const res = await strategiesAPI.getActivations({ page_size: 100 });
      setActivations(res.items);
    } catch {
      setError('Failed to load activations.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, [isAuthenticated]);

  const handleExit = async (price: number | null) => {
    if (!exitTarget) return;
    await strategiesAPI.exitActivation(exitTarget.id, price);
    await load();
  };

  const handleCancel = async (a: StrategyActivation) => {
    if (!confirm(`Cancel the ${a.symbol} WATCHING activation?`)) return;
    try {
      await strategiesAPI.cancelActivation(a.id);
      await load();
    } catch {
      alert('Failed to cancel activation.');
    }
  };

  const filtered = activations.filter(a => {
    if (filter === 'active') return !['EXITED', 'CANCELLED'].includes(a.state);
    if (filter === 'terminal') return ['EXITED', 'CANCELLED'].includes(a.state);
    return true;
  });

  const activeCount = activations.filter(a => !['EXITED', 'CANCELLED'].includes(a.state)).length;
  const inTradeCount = activations.filter(a => ['IN_TRADE', 'PARTIAL_EXIT'].includes(a.state)).length;

  return (
    <div className="space-y-6 py-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <div className="mb-1 flex items-center gap-2">
            <Link
              href="/strategies"
              className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
            >
              <ArrowLeft className="h-3.5 w-3.5" />
              Strategies
            </Link>
          </div>
          <h1 className="text-xl font-semibold text-slate-900">Activations</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Live and historical strategy activation records.
          </p>
        </div>

        <div className="flex flex-col items-end gap-1 text-right">
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">{inTradeCount} in trade</span>
            <span className="h-3 w-px bg-slate-200" />
            <span className="text-xs text-slate-500">{activeCount} active</span>
          </div>
        </div>
      </div>

      {/* Filter bar */}
      <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1 w-fit">
        {(['active', 'all', 'terminal'] as FilterState[]).map(f => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={cn(
              'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
              filter === f ? 'bg-white text-slate-900 shadow-sm' : 'text-slate-500 hover:text-slate-700',
            )}
          >
            {f.charAt(0).toUpperCase() + f.slice(1)}
          </button>
        ))}
      </div>

      {/* List */}
      {error ? (
        <p className="text-sm text-rose-600">{error}</p>
      ) : loading ? (
        <div className="space-y-2">
          {[1, 2, 3].map(i => (
            <div key={i} className="h-14 animate-pulse rounded-lg border border-slate-200 bg-slate-50" />
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 px-6 py-14 text-center">
          <Activity className="mb-3 h-8 w-8 text-slate-300" />
          <p className="text-sm font-medium text-slate-600">
            {filter === 'active' ? 'No active activations' : 'No activations found'}
          </p>
          <p className="mt-1 text-xs text-slate-400">
            Activations appear here when strategies match incoming signals.
          </p>
        </div>
      ) : (
        <div className="space-y-2">
          {/* Column headers */}
          <div className="flex items-center gap-3 px-4 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
            <span className="w-28 shrink-0">Symbol</span>
            <span className="w-24 shrink-0">State</span>
            <span className="flex-1">Levels</span>
            <span className="w-12 shrink-0 text-right">Hold</span>
            <span className="w-20 shrink-0 text-right">Exit</span>
            <span className="w-16 shrink-0" />
          </div>
          {filtered.map(a => (
            <ActivationRow
              key={a.id}
              activation={a}
              onExit={setExitTarget}
              onCancel={handleCancel}
            />
          ))}
        </div>
      )}

      {exitTarget && (
        <ExitModal
          activation={exitTarget}
          onClose={() => setExitTarget(null)}
          onConfirm={handleExit}
        />
      )}
    </div>
  );
}
