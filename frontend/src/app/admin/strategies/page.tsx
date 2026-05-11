'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ArrowUpCircle,
  ArrowDownCircle,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  History,
  Loader2,
  Plus,
  Search,
  Shield,
  X,
} from 'lucide-react';
import Link from 'next/link';
import {
  adminStrategiesAPI,
  strategiesAPI,
  type AdminStrategyItem,
  type StrategyAuditLogEntry,
} from '@/lib/api';
import type { UserStrategySubscription } from '@/types/strategies';
import { cn } from '@/lib/utils';
import { StrategyDetailPane } from '@/components/strategies/StrategyDetailPane';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: 'numeric',
  });
}

function fmtDatetime(iso: string): string {
  return new Date(iso).toLocaleString('en-IN', {
    day: '2-digit', month: 'short', year: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

// ─── Badges ───────────────────────────────────────────────────────────────────

const STATUS_STYLES: Record<string, string> = {
  active:   'bg-emerald-50 text-emerald-700 ring-emerald-200',
  draft:    'bg-slate-100 text-slate-500 ring-slate-200',
  paused:   'bg-amber-50 text-amber-700 ring-amber-200',
  archived: 'bg-rose-50 text-rose-500 ring-rose-200',
};

function StatusBadge({ status }: { status: string }) {
  return (
    <span className={cn(
      'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ring-1',
      STATUS_STYLES[status] ?? 'bg-slate-100 text-slate-500',
    )}>
      {status}
    </span>
  );
}

function ScopeBadge({ scope }: { scope: string }) {
  return scope === 'shared' ? (
    <span className="rounded-full bg-blue-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-blue-600 ring-1 ring-blue-200">
      Library
    </span>
  ) : (
    <span className="rounded-full bg-slate-50 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400 ring-1 ring-slate-200">
      Private
    </span>
  );
}

// ─── Reason dialog ────────────────────────────────────────────────────────────

interface ReasonDialogProps {
  action: 'promote' | 'demote';
  strategyName: string;
  onConfirm: (reason: string) => Promise<void>;
  onClose: () => void;
}

function ReasonDialog({ action, strategyName, onConfirm, onClose }: ReasonDialogProps) {
  const [reason, setReason] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const isPromote = action === 'promote';

  const handleConfirm = async () => {
    if (reason.trim().length < 5) {
      setError('Reason must be at least 5 characters.');
      return;
    }
    setError(null);
    setLoading(true);
    try {
      await onConfirm(reason.trim());
    } catch {
      setError('Action failed. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl">
        <div className="mb-4 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {isPromote ? (
              <ArrowUpCircle className="h-5 w-5 text-blue-500" />
            ) : (
              <ArrowDownCircle className="h-5 w-5 text-amber-500" />
            )}
            <h3 className="text-sm font-semibold text-slate-900">
              {isPromote ? 'Promote to Global Library' : 'Demote from Global Library'}
            </h3>
          </div>
          <button onClick={onClose} className="rounded p-1 hover:bg-slate-100">
            <X className="h-4 w-4 text-slate-400" />
          </button>
        </div>

        <p className="mb-4 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
          <span className="font-medium text-slate-800">{strategyName}</span>
          {isPromote
            ? ' will become visible to all users in the Global Library.'
            : ' will be hidden from the Global Library and reverted to private.'
          }
        </p>

        <div className="mb-4">
          <label className="mb-1.5 block text-xs font-medium text-slate-600">
            Reason <span className="text-rose-400">*</span>
            <span className="ml-1 font-normal text-slate-400">(recorded in audit log)</span>
          </label>
          <textarea
            ref={inputRef}
            rows={3}
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder={isPromote
              ? 'e.g. Consistently strong backtest results across 12 months, approved for library.'
              : 'e.g. Strategy author requested removal for revision.'
            }
            className="w-full resize-none rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 placeholder-slate-300 focus:border-slate-400 focus:outline-none"
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
            onClick={handleConfirm}
            disabled={loading}
            className={cn(
              'flex items-center gap-1.5 rounded-md px-4 py-1.5 text-xs font-semibold text-white disabled:opacity-50',
              isPromote ? 'bg-blue-600 hover:bg-blue-700' : 'bg-amber-500 hover:bg-amber-600',
            )}
          >
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            {isPromote ? 'Promote' : 'Demote'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Audit log drawer ─────────────────────────────────────────────────────────

interface AuditDrawerProps {
  strategyId: string;
  strategyName: string;
  onClose: () => void;
}

function AuditDrawer({ strategyId, strategyName, onClose }: AuditDrawerProps) {
  const [entries, setEntries] = useState<StrategyAuditLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [total, setTotal] = useState(0);

  useEffect(() => {
    adminStrategiesAPI.getAuditLog(strategyId).then(res => {
      setEntries(res.items);
      setTotal(res.total);
    }).finally(() => setLoading(false));
  }, [strategyId]);

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-end">
      {/* Backdrop */}
      <div
        className="absolute inset-0 bg-black/20 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* Panel */}
      <div className="relative z-10 flex h-full w-full max-w-sm flex-col border-l border-slate-200 bg-white shadow-2xl">
        <div className="flex items-center justify-between border-b border-slate-100 px-5 py-4">
          <div>
            <p className="flex items-center gap-1.5 text-sm font-semibold text-slate-900">
              <History className="h-4 w-4 text-slate-400" />
              Audit Log
            </p>
            <p className="mt-0.5 truncate text-[11px] text-slate-400">{strategyName}</p>
          </div>
          <button onClick={onClose} className="rounded p-1 hover:bg-slate-100">
            <X className="h-4 w-4 text-slate-400" />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
            </div>
          ) : entries.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-center">
              <History className="mb-3 h-7 w-7 text-slate-300" />
              <p className="text-sm font-medium text-slate-500">No audit entries</p>
              <p className="mt-1 text-xs text-slate-400">
                Promote/demote actions will appear here.
              </p>
            </div>
          ) : (
            <div className="space-y-3">
              <p className="text-[11px] text-slate-400">{total} total entries</p>
              {entries.map(entry => (
                <div
                  key={entry.id}
                  className="rounded-xl border border-slate-100 bg-slate-50 p-3.5"
                >
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className={cn(
                      'flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ring-1',
                      entry.action === 'promote'
                        ? 'bg-blue-50 text-blue-600 ring-blue-200'
                        : 'bg-amber-50 text-amber-700 ring-amber-200',
                    )}>
                      {entry.action === 'promote' ? (
                        <ArrowUpCircle className="h-3 w-3" />
                      ) : (
                        <ArrowDownCircle className="h-3 w-3" />
                      )}
                      {entry.action}
                    </span>
                    <span className="flex items-center gap-1 text-[10px] text-slate-400">
                      <Clock className="h-3 w-3" />
                      {fmtDatetime(entry.created_at)}
                    </span>
                  </div>

                  <p className="text-xs text-slate-700">
                    <span className="font-medium">{entry.scope_before}</span>
                    {' → '}
                    <span className="font-medium">{entry.scope_after}</span>
                  </p>

                  {entry.admin_username && (
                    <p className="mt-1 text-[11px] text-slate-500">
                      By <span className="font-medium">{entry.admin_username}</span>
                    </p>
                  )}

                  <p className="mt-2 text-[11px] italic text-slate-500">"{entry.reason}"</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Strategy table row ───────────────────────────────────────────────────────

interface StrategyRowProps {
  strategy: AdminStrategyItem;
  onPromote: (s: AdminStrategyItem) => void;
  onDemote: (s: AdminStrategyItem) => void;
  onViewAudit: (s: AdminStrategyItem) => void;
  onSelect: (id: string) => void;
}

function StrategyRow({ strategy, onPromote, onDemote, onViewAudit, onSelect }: StrategyRowProps) {
  const isShared = strategy.scope === 'shared';
  const isArchived = strategy.status === 'archived';

  return (
    <tr className="border-b border-slate-50 last:border-0 hover:bg-slate-50/60 transition-colors">
      <td className="py-3 pl-5 pr-3">
        <button
          onClick={() => onSelect(strategy.id)}
          className="text-left group"
        >
          <p className="text-sm font-medium text-slate-800 group-hover:text-blue-600 transition-colors">{strategy.name}</p>
          {strategy.description && (
            <p className="mt-0.5 max-w-xs truncate text-[11px] text-slate-400">{strategy.description}</p>
          )}
          {strategy.tags && strategy.tags.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {strategy.tags.slice(0, 3).map(t => (
                <span key={t} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                  #{t}
                </span>
              ))}
            </div>
          )}
        </button>
      </td>
      <td className="px-3 py-3 text-[11px] text-slate-600">
        <span className="font-mono text-slate-700">{strategy.creator_username ?? `uid:${strategy.created_by}`}</span>
        <p className="text-slate-400">{fmtDate(strategy.created_at)}</p>
      </td>
      <td className="px-3 py-3">
        <div className="flex flex-wrap gap-1.5">
          <StatusBadge status={strategy.status} />
          <ScopeBadge scope={strategy.scope} />
        </div>
      </td>
      <td className="px-3 py-3 text-right">
        <div className="space-y-0.5 text-[11px]">
          <p className="font-medium text-slate-700">{strategy.total_trades} trades</p>
          {strategy.win_rate_pct != null && (
            <p className="text-slate-500">{strategy.win_rate_pct.toFixed(1)}% win</p>
          )}
          {strategy.avg_daily_return_pct != null && (
            <p className={cn(
              'font-medium',
              strategy.avg_daily_return_pct >= 0 ? 'text-emerald-600' : 'text-rose-500',
            )}>
              {strategy.avg_daily_return_pct >= 0 ? '+' : ''}{strategy.avg_daily_return_pct.toFixed(2)}%/d
            </p>
          )}
          <p className="text-slate-400">{strategy.total_subscribers} subs</p>
        </div>
      </td>
      <td className="py-3 pl-3 pr-5">
        <div className="flex items-center justify-end gap-1.5">
          <button
            onClick={() => onViewAudit(strategy)}
            title="View audit log"
            className="rounded-md p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-700"
          >
            <History className="h-3.5 w-3.5" />
          </button>

          {!isShared && !isArchived && (
            <button
              onClick={() => onPromote(strategy)}
              title="Promote to library"
              className="flex items-center gap-1 rounded-md border border-blue-200 bg-blue-50 px-2.5 py-1 text-[11px] font-semibold text-blue-600 hover:bg-blue-100"
            >
              <ArrowUpCircle className="h-3.5 w-3.5" />
              Promote
            </button>
          )}

          {isShared && (
            <button
              onClick={() => onDemote(strategy)}
              title="Demote from library"
              className="flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-[11px] font-semibold text-amber-700 hover:bg-amber-100"
            >
              <ArrowDownCircle className="h-3.5 w-3.5" />
              Demote
            </button>
          )}
        </div>
      </td>
    </tr>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

const PAGE_SIZE = 25;

type ScopeFilter = '' | 'private' | 'shared';
type StatusFilter = '' | 'active' | 'draft' | 'paused' | 'archived';

export default function AdminStrategiesPage() {
  const [strategies, setStrategies] = useState<AdminStrategyItem[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);

  // Filters
  const [scopeFilter, setScopeFilter] = useState<ScopeFilter>('');
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('');
  const [searchInput, setSearchInput] = useState('');
  const [appliedSearch, setAppliedSearch] = useState('');

  // Dialogs
  const [promoteTarget, setPromoteTarget] = useState<AdminStrategyItem | null>(null);
  const [demoteTarget, setDemoteTarget] = useState<AdminStrategyItem | null>(null);
  const [auditTarget, setAuditTarget] = useState<AdminStrategyItem | null>(null);
  const [selectedStrategyId, setSelectedStrategyId] = useState<string | null>(null);

  // Admin's own subscriptions (for pause/resume in detail pane)
  const [subscriptions, setSubscriptions] = useState<UserStrategySubscription[]>([]);

  useEffect(() => {
    strategiesAPI.getSubscriptions()
      .then(res => setSubscriptions(res.items))
      .catch(() => {});
  }, []);

  // Debounce ref for search
  const searchDebounce = useRef<ReturnType<typeof setTimeout> | null>(null);

  const loadStrategies = useCallback(async () => {
    setLoading(true);
    try {
      const res = await adminStrategiesAPI.list({
        scope: scopeFilter || undefined,
        status: statusFilter || undefined,
        search: appliedSearch || undefined,
        page,
        page_size: PAGE_SIZE,
      });
      setStrategies(res.items);
      setTotal(res.total);
    } catch {
      // keep stale data on error
    } finally {
      setLoading(false);
    }
  }, [scopeFilter, statusFilter, appliedSearch, page]);

  useEffect(() => { loadStrategies(); }, [loadStrategies]);

  const handleSearchChange = (value: string) => {
    setSearchInput(value);
    if (searchDebounce.current) clearTimeout(searchDebounce.current);
    searchDebounce.current = setTimeout(() => {
      setAppliedSearch(value);
      setPage(1);
    }, 350);
  };

  const handleFilterChange = (scope: ScopeFilter, status: StatusFilter) => {
    setScopeFilter(scope);
    setStatusFilter(status);
    setPage(1);
  };

  const handlePromote = async (reason: string) => {
    if (!promoteTarget) return;
    await adminStrategiesAPI.promote(promoteTarget.id, { reason });
    setPromoteTarget(null);
    loadStrategies();
  };

  const handleDemote = async (reason: string) => {
    if (!demoteTarget) return;
    await adminStrategiesAPI.demote(demoteTarget.id, { reason });
    setDemoteTarget(null);
    loadStrategies();
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);
  const libraryCount = strategies.filter(s => s.scope === 'shared').length;
  const privateCount = strategies.filter(s => s.scope === 'private').length;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-bold tracking-tight text-slate-900">
            <Shield className="h-5 w-5 text-slate-400" />
            Strategy Governance
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Review all user strategies, promote to the global library, and audit scope history.
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Link
            href="/strategies?from=admin"
            className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-4 py-2 text-xs font-semibold text-slate-700 shadow-sm transition-colors hover:bg-slate-50"
          >
            <BookOpen className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
            Strategy Library
          </Link>
          <Link
            href="/admin/strategies/new"
            className="flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-xs font-semibold text-white shadow-sm transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-2"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            New Library Strategy
          </Link>
        </div>
      </div>

      {/* Stats strip */}
      <div className="grid grid-cols-3 gap-3">
        {[
          { label: 'Total Strategies', value: total },
          { label: 'In Library', value: strategies.filter(s => s.scope === 'shared').length, accent: 'text-blue-600' },
          { label: 'Private', value: strategies.filter(s => s.scope === 'private').length },
        ].map(({ label, value, accent }) => (
          <div key={label} className="rounded-xl border border-slate-100 bg-white px-4 py-3">
            <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</p>
            <p className={cn('mt-1 text-2xl font-bold', accent ?? 'text-slate-900')}>{value}</p>
          </div>
        ))}
      </div>

      {/* Filters toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        {/* Search */}
        <div className="relative min-w-0 flex-1">
          <Search className="absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-400" />
          <input
            value={searchInput}
            onChange={e => handleSearchChange(e.target.value)}
            placeholder="Search by name or description…"
            className="w-full rounded-lg border border-slate-200 bg-white py-2 pl-9 pr-3 text-sm text-slate-800 placeholder-slate-400 focus:border-slate-400 focus:outline-none"
          />
        </div>

        {/* Scope filter */}
        <select
          value={scopeFilter}
          onChange={e => handleFilterChange(e.target.value as ScopeFilter, statusFilter)}
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 focus:border-slate-400 focus:outline-none"
        >
          <option value="">All Scopes</option>
          <option value="private">Private</option>
          <option value="shared">Library</option>
        </select>

        {/* Status filter */}
        <select
          value={statusFilter}
          onChange={e => handleFilterChange(scopeFilter, e.target.value as StatusFilter)}
          className="rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 focus:border-slate-400 focus:outline-none"
        >
          <option value="">All Statuses</option>
          <option value="active">Active</option>
          <option value="draft">Draft</option>
          <option value="paused">Paused</option>
          <option value="archived">Archived</option>
        </select>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded-2xl border border-slate-200 bg-white shadow-sm">
        <table className="min-w-full">
          <thead>
            <tr className="border-b border-slate-100">
              {['Strategy', 'Creator', 'Scope / Status', 'Performance', 'Actions'].map((h, i) => (
                <th
                  key={h}
                  className={cn(
                    'py-3 text-[10px] font-semibold uppercase tracking-wider text-slate-400',
                    i === 0 ? 'pl-5 pr-3 text-left' :
                    i === 4 ? 'pl-3 pr-5 text-right' :
                    i === 3 ? 'px-3 text-right' :
                    'px-3 text-left',
                  )}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              Array.from({ length: 8 }).map((_, i) => (
                <tr key={i} className="border-b border-slate-50">
                  {[1, 2, 3, 4, 5].map(j => (
                    <td key={j} className="px-3 py-3">
                      <div className="h-4 animate-pulse rounded bg-slate-100" />
                    </td>
                  ))}
                </tr>
              ))
            ) : strategies.length === 0 ? (
              <tr>
                <td colSpan={5} className="py-16 text-center">
                  <BookOpen className="mx-auto mb-3 h-8 w-8 text-slate-300" />
                  <p className="text-sm font-medium text-slate-500">No strategies match your filters</p>
                </td>
              </tr>
            ) : (
              strategies.map(s => (
                <StrategyRow
                  key={s.id}
                  strategy={s}
                  onPromote={setPromoteTarget}
                  onDemote={setDemoteTarget}
                  onViewAudit={setAuditTarget}
                  onSelect={setSelectedStrategyId}
                />
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between text-xs text-slate-500">
          <span>
            {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, total)} of {total}
          </span>
          <div className="flex items-center gap-1">
            <button
              onClick={() => setPage(p => Math.max(1, p - 1))}
              disabled={page === 1}
              className="rounded border border-slate-200 p-1 hover:bg-slate-50 disabled:opacity-30"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </button>
            <span className="px-2 font-medium text-slate-700">{page} / {totalPages}</span>
            <button
              onClick={() => setPage(p => Math.min(totalPages, p + 1))}
              disabled={page === totalPages}
              className="rounded border border-slate-200 p-1 hover:bg-slate-50 disabled:opacity-30"
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        </div>
      )}

      {/* Promote dialog */}
      {promoteTarget && (
        <ReasonDialog
          action="promote"
          strategyName={promoteTarget.name}
          onConfirm={handlePromote}
          onClose={() => setPromoteTarget(null)}
        />
      )}

      {/* Demote dialog */}
      {demoteTarget && (
        <ReasonDialog
          action="demote"
          strategyName={demoteTarget.name}
          onConfirm={handleDemote}
          onClose={() => setDemoteTarget(null)}
        />
      )}

      {/* Audit drawer */}
      {auditTarget && (
        <AuditDrawer
          strategyId={auditTarget.id}
          strategyName={auditTarget.name}
          onClose={() => setAuditTarget(null)}
        />
      )}

      {/* Strategy detail pane */}
      <StrategyDetailPane
        strategyId={selectedStrategyId}
        onClose={() => setSelectedStrategyId(null)}
        subscribedIds={new Set(subscriptions.map(s => s.strategy_id))}
        priorityMap={new Map(subscriptions.map(s => [s.strategy_id, s.priority]))}
        activeMap={new Map(subscriptions.map(s => [s.strategy_id, s.is_active]))}
        isAdmin={true}
        fullPageSuffix="?from=admin"
        onSubscribeRequest={() => {}}
        onToggleActive={async (strategyId, currentlyActive) => {
          await strategiesAPI.setSubscriptionActive(strategyId, !currentlyActive);
          setSubscriptions(prev =>
            prev.map(s => s.strategy_id === strategyId ? { ...s, is_active: !currentlyActive } : s)
          );
        }}
      />
    </div>
  );
}
