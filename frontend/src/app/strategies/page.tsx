'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useRouter, useSearchParams } from 'next/navigation';
import {
  ArrowLeft,
  BarChart2,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Clock,
  GripVertical,
  Loader2,
  Pause,
  Play,
  Plus,
  Save,
  TrendingUp,
  Users,
  Workflow,
  X,
  Zap,
} from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { strategiesAPI } from '@/lib/api';
import { StrategyDetailPane } from '@/components/strategies/StrategyDetailPane';
import type { StrategySummary, UserStrategySubscription } from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-50 text-emerald-700',
    draft: 'bg-slate-100 text-slate-500',
    paused: 'bg-amber-50 text-amber-700',
    archived: 'bg-red-50 text-red-500',
  };
  return (
    <span className={cn('rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider', styles[status] ?? styles.draft)}>
      {status}
    </span>
  );
}

function ScopeBadge({ scope }: { scope: string }) {
  return scope === 'shared' ? (
    <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-blue-600">
      Library
    </span>
  ) : null;
}

function MetricPill({ label, value }: { label: string; value: string | number | null }) {
  if (value == null) return null;
  return (
    <span className="flex items-center gap-1 text-xs text-slate-500">
      <span className="font-medium text-slate-700">{typeof value === 'number' ? value.toFixed(1) : value}</span>
      <span>{label}</span>
    </span>
  );
}

// ─── Quick-subscribe inline modal ─────────────────────────────────────────────

interface QuickSubscribeModalProps {
  strategyName: string;
  onConfirm: (priority: number) => Promise<void>;
  onClose: () => void;
}

function QuickSubscribeModal({ strategyName, onConfirm, onClose }: QuickSubscribeModalProps) {
  const [priority, setPriority] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async () => {
    if (priority < 1 || priority > 100) { setError('Priority must be 1–100.'); return; }
    setError(null);
    setLoading(true);
    try {
      await onConfirm(priority);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '';
      setError(msg.includes('409') || msg.includes('conflict') ? 'Priority already taken. Choose another.' : 'Failed to subscribe. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm"
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="w-full max-w-sm rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold text-slate-900">
            <Zap className="h-4 w-4 text-amber-500" />
            Subscribe
          </h3>
          <button onClick={onClose} className="rounded p-1 hover:bg-slate-100">
            <X className="h-4 w-4 text-slate-400" />
          </button>
        </div>

        <p className="mb-4 text-xs text-slate-500">
          Subscribing to <span className="font-medium text-slate-800">{strategyName}</span>.
          Higher priority strategies are evaluated first on each signal.
        </p>

        <div className="mb-4">
          <label className="mb-1.5 block text-xs font-medium text-slate-600">Priority (1 = highest)</label>
          <input
            type="number"
            min={1}
            max={100}
            value={priority}
            onChange={e => setPriority(Number(e.target.value))}
            className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 focus:border-slate-400 focus:outline-none"
          />
        </div>

        {error && <p className="mb-3 text-xs text-rose-500">{error}</p>}

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-slate-200 px-4 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-md bg-slate-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : null}
            {loading ? 'Subscribing…' : 'Subscribe'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Strategy card ────────────────────────────────────────────────────────────

function StrategyCard({
  strategy,
  subscribed,
  subActive,
  priority,
  onSelect,
  onSubscribeRequest,
  onToggleActive,
}: {
  strategy: StrategySummary;
  subscribed: boolean;
  subActive?: boolean;
  priority?: number;
  onSelect: (id: string) => void;
  onSubscribeRequest?: (s: StrategySummary) => void;
  onToggleActive?: (id: string, currentlyActive: boolean) => void;
}) {
  const [toggling, setToggling] = useState(false);

  const handleToggle = async (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!onToggleActive || toggling) return;
    setToggling(true);
    try {
      await onToggleActive(strategy.id, !!subActive);
    } finally {
      setToggling(false);
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onSelect(strategy.id)}
      onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(strategy.id); } }}
      className={cn(
        'group relative flex w-full cursor-pointer flex-col rounded-xl border bg-white p-5 shadow-sm transition-all text-left hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400',
        subscribed && !subActive
          ? 'border-amber-200 hover:border-amber-300'
          : 'border-slate-200 hover:border-slate-300',
      )}
    >
      <div className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="truncate text-sm font-semibold text-slate-900">{strategy.name}</h3>
            <ScopeBadge scope={strategy.scope} />
          </div>
          {strategy.description && (
            <p className="mt-0.5 line-clamp-2 text-xs text-slate-500">{strategy.description}</p>
          )}
        </div>
        <StatusBadge status={strategy.status} />
      </div>

      {/* Metrics row */}
      <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-3">
        <MetricPill label="win rate" value={strategy.win_rate_pct != null ? `${strategy.win_rate_pct.toFixed(1)}%` : null} />
        <MetricPill label="trades" value={strategy.total_trades > 0 ? strategy.total_trades : null} />
        <MetricPill label="subscribers" value={strategy.total_subscribers > 0 ? strategy.total_subscribers : null} />
        {strategy.avg_daily_return_pct != null && (
          <span className={cn(
            'flex items-center gap-0.5 text-xs font-semibold',
            strategy.avg_daily_return_pct >= 0 ? 'text-emerald-600' : 'text-rose-600',
          )}>
            <TrendingUp className="h-3 w-3" aria-hidden="true" />
            {strategy.avg_daily_return_pct >= 0 ? '+' : ''}{strategy.avg_daily_return_pct.toFixed(2)}%/day
          </span>
        )}
      </div>

      {/* Tags */}
      {strategy.tags && strategy.tags.length > 0 && (
        <div className="mt-2.5 flex flex-wrap gap-1">
          {strategy.tags.slice(0, 4).map((tag) => (
            <span key={tag} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
              #{tag}
            </span>
          ))}
        </div>
      )}

      {/* Bottom bar: subscription state + quick actions */}
      <div className="mt-3 flex items-center justify-between gap-2">
        {subscribed ? (
          <div className="flex items-center gap-1.5">
            {subActive ? (
              <div className="flex items-center gap-1 text-xs text-emerald-600">
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Active{priority != null ? ` · P${priority}` : ''}</span>
              </div>
            ) : (
              <div className="flex items-center gap-1 text-xs text-amber-600">
                <Pause className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Paused{priority != null ? ` · P${priority}` : ''}</span>
              </div>
            )}
          </div>
        ) : (
          <span />
        )}

        <div className="flex items-center gap-1.5">
          {subscribed && onToggleActive && (
            <button
              onClick={handleToggle}
              disabled={toggling}
              title={subActive ? 'Pause — strategy will stop processing signals' : 'Resume — strategy will process signals again'}
              className={cn(
                'relative z-10 flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11px] font-medium shadow-sm transition-colors disabled:opacity-50',
                subActive
                  ? 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100'
                  : 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100',
              )}
            >
              {toggling ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : subActive ? (
                <Pause className="h-3 w-3" aria-hidden="true" />
              ) : (
                <Play className="h-3 w-3" aria-hidden="true" />
              )}
              {subActive ? 'Pause' : 'Resume'}
            </button>
          )}

          {strategy.scope === 'shared' && !subscribed && onSubscribeRequest && (
            <button
              onClick={e => { e.stopPropagation(); onSubscribeRequest(strategy); }}
              className="relative z-10 flex items-center gap-1 rounded-md border border-slate-200 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-600 shadow-sm hover:bg-slate-50"
            >
              <Zap className="h-3 w-3 text-amber-500" aria-hidden="true" />
              Subscribe
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Skeleton card ────────────────────────────────────────────────────────────

function SkeletonCard() {
  return (
    <div className="animate-pulse rounded-xl border border-slate-200 bg-white p-5">
      <div className="mb-3 space-y-2">
        <div className="h-4 w-2/3 rounded bg-slate-100" />
        <div className="h-3 w-full rounded bg-slate-100" />
      </div>
      <div className="flex gap-3 border-t border-slate-100 pt-3">
        {[1, 2, 3].map((i) => <div key={i} className="h-3 w-14 rounded bg-slate-100" />)}
      </div>
    </div>
  );
}

// ─── Tab bar ──────────────────────────────────────────────────────────────────

type Tab = 'library' | 'mine';

function TabBar({ active, onChange }: { active: Tab; onChange: (t: Tab) => void }) {
  return (
    <div className="flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 p-1">
      {([
        { id: 'library' as Tab, label: 'Global Library', icon: BookOpen },
        { id: 'mine' as Tab, label: 'My Strategies', icon: Workflow },
      ]).map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          onClick={() => onChange(id)}
          className={cn(
            'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400',
            active === id
              ? 'bg-white text-slate-900 shadow-sm'
              : 'text-slate-500 hover:text-slate-700',
          )}
        >
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />
          {label}
        </button>
      ))}
    </div>
  );
}

// ─── Empty state ──────────────────────────────────────────────────────────────

function EmptyState({ tab, isAdmin }: { tab: Tab; isAdmin: boolean }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 px-6 py-16 text-center">
      {tab === 'library' ? (
        <>
          <BookOpen className="mb-3 h-8 w-8 text-slate-300" aria-hidden="true" />
          <p className="text-sm font-medium text-slate-600">No public strategies yet</p>
          <p className="mt-1 text-xs text-slate-400">Strategies promoted by admins will appear here.</p>
        </>
      ) : (
        <>
          <Workflow className="mb-3 h-8 w-8 text-slate-300" aria-hidden="true" />
          <p className="text-sm font-medium text-slate-600">No strategies yet</p>
          {!isAdmin && (
            <>
              <p className="mt-1 text-xs text-slate-400">Create your first strategy to get started.</p>
              <Link
                href="/strategies/new"
                className="mt-4 flex items-center gap-1.5 rounded-md bg-slate-900 px-3.5 py-2 text-xs font-semibold text-white transition-colors hover:bg-slate-700"
              >
                <Plus className="h-3.5 w-3.5" aria-hidden="true" />
                New strategy
              </Link>
            </>
          )}
        </>
      )}
    </div>
  );
}

// ─── Subscription reorder panel ───────────────────────────────────────────────

function SubscriptionReorderPanel({
  subscriptions,
  onSaved,
}: {
  subscriptions: UserStrategySubscription[];
  onSaved: (reordered: UserStrategySubscription[]) => void;
}) {
  const [order, setOrder] = useState<UserStrategySubscription[]>(subscriptions);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  // Sync when subscriptions prop changes
  useEffect(() => { setOrder(subscriptions); }, [subscriptions]);

  const isDirty = order.some((s, i) => s.id !== subscriptions[i]?.id);

  const move = (index: number, direction: -1 | 1) => {
    const next = [...order];
    const target = index + direction;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    setOrder(next);
    setSaved(false);
  };

  const save = async () => {
    setSaving(true);
    try {
      const res = await strategiesAPI.reorderSubscriptions({
        subscription_ids: order.map(s => s.id),
      });
      setSaved(true);
      onSaved(res.items);
    } catch {
      alert('Failed to save order. Please try again.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white">
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-3">
        <div>
          <p className="text-xs font-semibold text-slate-900">Subscription Priority</p>
          <p className="mt-0.5 text-[11px] text-slate-400">Strategies are evaluated top-to-bottom on each signal.</p>
        </div>
        {isDirty && (
          <button
            onClick={save}
            disabled={saving}
            className="flex items-center gap-1.5 rounded-md bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            <Save className="h-3 w-3" aria-hidden="true" />
            {saving ? 'Saving…' : 'Save Order'}
          </button>
        )}
        {saved && !isDirty && (
          <span className="flex items-center gap-1 text-xs text-emerald-600">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            Saved
          </span>
        )}
      </div>
      <ul className="divide-y divide-slate-100">
        {order.map((sub, i) => (
          <li key={sub.id} className="flex items-center gap-3 px-4 py-2.5">
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[10px] font-bold text-slate-600">
              {i + 1}
            </span>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-slate-900">
                {sub.strategy?.name ?? `Strategy #${sub.strategy_id.slice(0, 8)}`}
              </p>
              {sub.notes && (
                <p className="truncate text-[10px] text-slate-400">{sub.notes}</p>
              )}
            </div>
            <div className="flex shrink-0 flex-col gap-0.5">
              <button
                onClick={() => move(i, -1)}
                disabled={i === 0}
                className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-20"
                aria-label="Move up"
              >
                <ChevronUp className="h-3.5 w-3.5" />
              </button>
              <button
                onClick={() => move(i, 1)}
                disabled={i === order.length - 1}
                className="rounded p-0.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700 disabled:opacity-20"
                aria-label="Move down"
              >
                <ChevronDown className="h-3.5 w-3.5" />
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function StrategiesPage() {
  const { isAuthenticated, isAuthReady, isAdmin } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const fromAdmin = searchParams.get('from') === 'admin';
  const [tab, setTab] = useState<Tab>('library');
  const [library, setLibrary] = useState<StrategySummary[]>([]);
  const [mine, setMine] = useState<StrategySummary[]>([]);
  const [subscriptions, setSubscriptions] = useState<UserStrategySubscription[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [quickSubscribeTarget, setQuickSubscribeTarget] = useState<StrategySummary | null>(null);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(null);

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  useEffect(() => {
    if (!isAuthenticated) return;
    setLoading(true);
    Promise.all([
      strategiesAPI.list({ page_size: 50 }),
      strategiesAPI.listMine({ page_size: 50 }),
      strategiesAPI.getSubscriptions(),
    ])
      .then(([lib, myStrats, subs]) => {
        setLibrary(lib.items);
        setMine(myStrats.items);
        setSubscriptions(subs.items);
      })
      .catch(() => setError('Failed to load strategies.'))
      .finally(() => setLoading(false));
  }, [isAuthenticated]);

  const subscribedIds = new Set(subscriptions.map((s) => s.strategy_id));
  const priorityMap = new Map(subscriptions.map((s) => [s.strategy_id, s.priority]));
  const activeMap = new Map(subscriptions.map((s) => [s.strategy_id, s.is_active]));

  const items = tab === 'library' ? library : mine;

  const handleQuickSubscribe = async (priority: number) => {
    if (!quickSubscribeTarget) return;
    await strategiesAPI.subscribe(quickSubscribeTarget.id, { priority });
    const subs = await strategiesAPI.getSubscriptions();
    setSubscriptions(subs.items);
    setQuickSubscribeTarget(null);
  };

  const handleToggleActive = async (strategyId: string, currentlyActive: boolean) => {
    await strategiesAPI.setSubscriptionActive(strategyId, !currentlyActive);
    setSubscriptions(prev =>
      prev.map(s => s.strategy_id === strategyId ? { ...s, is_active: !currentlyActive } : s)
    );
  };

  return (
    <div className="space-y-6 py-6">
      {/* Back to Governance — shown only when navigated from admin */}
      {fromAdmin && (
        <Link
          href="/admin/strategies"
          className="inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Governance
        </Link>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-slate-900">Trading Strategies</h1>
          <p className="mt-0.5 text-sm text-slate-500">
            Browse the global strategy library or manage your own rules.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {subscriptions.length > 0 && (
            <Link
              href="/strategies/activations"
              className="flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 transition-colors hover:bg-slate-50"
            >
              <BarChart2 className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
              Activations
              {subscriptions.length > 0 && (
                <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold text-slate-600">
                  {subscriptions.length}
                </span>
              )}
            </Link>
          )}
          {!isAdmin && (
            <Link
              href="/strategies/new"
              className="flex items-center gap-1.5 rounded-md bg-slate-900 px-3 py-1.5 text-xs font-semibold text-white transition-colors hover:bg-slate-700"
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              New strategy
            </Link>
          )}
        </div>
      </div>

      {/* Subscription summary strip */}
      {subscriptions.length > 0 && (
        <div className="flex items-center justify-between rounded-lg border border-emerald-100 bg-emerald-50 px-4 py-2.5">
          <div className="flex items-center gap-2">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden="true" />
            <span className="text-xs font-medium text-emerald-800">
              {subscriptions.length} active subscription{subscriptions.length !== 1 ? 's' : ''}
            </span>
            <span className="text-xs text-emerald-600">
              — evaluated in priority order on each signal
            </span>
          </div>
          <Link
            href="/settings"
            className="flex items-center gap-1 text-xs font-medium text-emerald-700 hover:text-emerald-900"
          >
            Manage <ChevronRight className="h-3 w-3" />
          </Link>
        </div>
      )}

      {/* Tabs */}
      <TabBar active={tab} onChange={setTab} />

      {/* Subscription reorder panel — shown only on My Strategies tab when subscriptions exist */}
      {tab === 'mine' && subscriptions.length > 0 && (
        <SubscriptionReorderPanel
          subscriptions={subscriptions}
          onSaved={(reordered) => setSubscriptions(reordered)}
        />
      )}

      {/* Content */}
      {error ? (
        <p className="text-sm text-rose-600">{error}</p>
      ) : loading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => <SkeletonCard key={i} />)}
        </div>
      ) : items.length === 0 ? (
        <EmptyState tab={tab} isAdmin={isAdmin} />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {items.map((s) => (
            <StrategyCard
              key={s.id}
              strategy={s}
              subscribed={subscribedIds.has(s.id)}
              subActive={activeMap.get(s.id)}
              priority={priorityMap.get(s.id)}
              onSelect={setSelectedStrategyId}
              onSubscribeRequest={tab === 'library' ? setQuickSubscribeTarget : undefined}
              onToggleActive={subscribedIds.has(s.id) ? handleToggleActive : undefined}
            />
          ))}
        </div>
      )}

      {/* Stats strip (non-empty only) */}
      {!loading && !error && items.length > 0 && (
        <p className="text-center text-xs text-slate-400">
          Showing {items.length} {tab === 'library' ? 'public strategies' : 'of your strategies'}
          {tab === 'mine' && mine.length > 0 && (
            <>
              {' · '}
              <Link href="/settings" className="text-slate-500 hover:text-slate-700 underline underline-offset-2">
                Configure enforcement mode
              </Link>
            </>
          )}
        </p>
      )}

      {/* Quick-subscribe modal */}
      {quickSubscribeTarget && (
        <QuickSubscribeModal
          strategyName={quickSubscribeTarget.name}
          onConfirm={handleQuickSubscribe}
          onClose={() => setQuickSubscribeTarget(null)}
        />
      )}

      {/* Strategy detail pane */}
      <StrategyDetailPane
        strategyId={selectedStrategyId}
        onClose={() => setSelectedStrategyId(null)}
        subscribedIds={subscribedIds}
        priorityMap={priorityMap}
        isAdmin={isAdmin}
        onSubscribeRequest={(s) => {
          setSelectedStrategyId(null);
          setQuickSubscribeTarget(s);
        }}
      />
    </div>
  );
}
