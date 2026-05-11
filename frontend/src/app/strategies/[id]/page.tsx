'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter, useSearchParams } from 'next/navigation';
import {
  Activity,
  ArrowLeft,
  BarChart2,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  Loader2,
  Pause,
  Pencil,
  Play,
  PlayCircle,
  Search,
  Tag,
  X,
  XCircle,
  Zap,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useAuth } from '@/contexts/AuthContext';
import { strategiesAPI, upstoxAPI } from '@/lib/api';
import type { UpstoxInstrument, UpstoxInstrumentSearchResponse } from '@/types/upstox';
import type {
  BacktestMode,
  Strategy,
  StrategyBacktestRun,
  StrategyPerformance,
  StrategyTrade,
  UserProfile,
  UserStrategySubscription,
} from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Type helpers ─────────────────────────────────────────────────────────────

interface EquityPoint {
  label: string;
  cumulative: number;
  trade: number;
}

// ─── Formatting helpers ───────────────────────────────────────────────────────

function fmtCurrency(v: number): string {
  if (Math.abs(v) >= 1e7) return `₹${(v / 1e7).toFixed(2)}Cr`;
  if (Math.abs(v) >= 1e5) return `₹${(v / 1e5).toFixed(2)}L`;
  return `₹${v.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function fmtDuration(secs: number | null): string {
  if (!secs) return '—';
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return `${Math.floor(secs / 86400)}d`;
}

function fmtRatio(v: number | null): string {
  if (v == null) return '—';
  return v.toFixed(2);
}

function fmtPct(v: number | null): string {
  if (v == null) return '—';
  return `${v.toFixed(2)}%`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: '2-digit' });
}

// ─── Status & scope badges ────────────────────────────────────────────────────

function StatusBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    active: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    draft: 'bg-slate-100 text-slate-500 ring-slate-200',
    paused: 'bg-amber-50 text-amber-700 ring-amber-200',
    archived: 'bg-red-50 text-red-500 ring-red-200',
  };
  return (
    <span className={cn(
      'rounded-full px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider ring-1',
      styles[status] ?? styles.draft,
    )}>
      {status}
    </span>
  );
}

// ─── Metric card ─────────────────────────────────────────────────────────────

interface MetricCardProps {
  label: string;
  value: string;
  sub?: string;
  positive?: boolean | null;
  loading?: boolean;
}

function MetricCard({ label, value, sub, positive, loading }: MetricCardProps) {
  if (loading) {
    return (
      <div className="animate-pulse rounded-xl border border-slate-200 bg-white p-4">
        <div className="mb-2 h-3 w-20 rounded bg-slate-100" />
        <div className="h-6 w-28 rounded bg-slate-100" />
      </div>
    );
  }
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4">
      <p className="mb-1 text-[11px] font-medium uppercase tracking-wider text-slate-400">{label}</p>
      <p className={cn(
        'text-xl font-bold tabular-nums',
        positive === true ? 'text-emerald-600' :
        positive === false ? 'text-rose-600' :
        'text-slate-900',
      )}>
        {value}
      </p>
      {sub && <p className="mt-0.5 text-[11px] text-slate-400">{sub}</p>}
    </div>
  );
}

// ─── Equity curve chart ───────────────────────────────────────────────────────

function EquityCurveChart({ trades }: { trades: StrategyTrade[] }) {
  const data: EquityPoint[] = useMemo(() => {
    let cumulative = 0;
    return trades.map((t, i) => {
      cumulative += t.net_pnl;
      return {
        label: fmtDate(t.entry_at),
        cumulative: parseFloat(cumulative.toFixed(2)),
        trade: i + 1,
      };
    });
  }, [trades]);

  if (data.length === 0) {
    return (
      <div className="flex h-44 items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50">
        <p className="text-sm text-slate-400">No trade data yet</p>
      </div>
    );
  }

  const isPositive = data.length > 0 && data[data.length - 1].cumulative >= 0;
  const strokeColor = isPositive ? '#10b981' : '#f43f5e';
  const fillColor = isPositive ? '#d1fae5' : '#ffe4e6';

  const CustomTooltip = ({ active, payload }: { active?: boolean; payload?: { payload: EquityPoint }[] }) => {
    if (!active || !payload?.length) return null;
    const pt = payload[0].payload;
    return (
      <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-lg text-xs">
        <p className="font-medium text-slate-700">Trade #{pt.trade} · {pt.label}</p>
        <p className={cn('mt-0.5 font-semibold tabular-nums', pt.cumulative >= 0 ? 'text-emerald-600' : 'text-rose-600')}>
          {fmtCurrency(pt.cumulative)}
        </p>
      </div>
    );
  };

  return (
    <ResponsiveContainer width="100%" height={220}>
      <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
        <defs>
          <linearGradient id="equityGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%" stopColor={fillColor} stopOpacity={0.8} />
            <stop offset="95%" stopColor={fillColor} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
        <XAxis
          dataKey="trade"
          tick={{ fontSize: 10, fill: '#94a3b8' }}
          tickLine={false}
          axisLine={false}
          label={{ value: 'Trade #', position: 'insideBottom', offset: -2, fontSize: 10, fill: '#94a3b8' }}
          height={24}
        />
        <YAxis
          tick={{ fontSize: 10, fill: '#94a3b8' }}
          tickLine={false}
          axisLine={false}
          tickFormatter={(v: number) => v >= 1000 ? `${(v / 1000).toFixed(0)}k` : String(v)}
          width={48}
        />
        <Tooltip content={<CustomTooltip />} />
        <Area
          type="monotone"
          dataKey="cumulative"
          stroke={strokeColor}
          strokeWidth={2}
          fill="url(#equityGrad)"
          dot={false}
          activeDot={{ r: 4, strokeWidth: 0 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

// ─── Trade row ────────────────────────────────────────────────────────────────

const EXIT_REASON_STYLES: Record<string, string> = {
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

function TradeRow({ trade }: { trade: StrategyTrade }) {
  const profit = trade.net_pnl >= 0;
  return (
    <tr className="border-b border-slate-100 text-xs transition-colors hover:bg-slate-50/60">
      <td className="py-2.5 pl-4 pr-3 text-slate-500">{fmtDate(trade.entry_at)}</td>
      <td className="px-3 py-2.5 font-medium text-slate-900">{trade.symbol}</td>
      <td className="px-3 py-2.5">
        <span className={cn(
          'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase',
          trade.direction === 'LONG' ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-600',
        )}>
          {trade.direction}
        </span>
      </td>
      <td className="px-3 py-2.5 tabular-nums text-slate-700">₹{trade.entry_price.toLocaleString()}</td>
      <td className="px-3 py-2.5 tabular-nums text-slate-700">₹{trade.exit_price.toLocaleString()}</td>
      <td className="px-3 py-2.5">
        <span className={cn(
          'font-semibold tabular-nums',
          profit ? 'text-emerald-600' : 'text-rose-600',
        )}>
          {profit ? '+' : ''}{trade.pnl_pct.toFixed(2)}%
        </span>
      </td>
      <td className={cn(
        'px-3 py-2.5 font-medium tabular-nums',
        profit ? 'text-emerald-600' : 'text-rose-600',
      )}>
        {profit ? '+' : ''}{fmtCurrency(trade.net_pnl)}
      </td>
      <td className="px-3 py-2.5 text-slate-500">{fmtDuration(trade.hold_duration_seconds)}</td>
      <td className="py-2.5 pl-3 pr-4">
        <span className={cn(
          'rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide',
          EXIT_REASON_STYLES[trade.exit_reason] ?? 'bg-slate-100 text-slate-600',
        )}>
          {trade.exit_reason.replace('_', ' ')}
        </span>
      </td>
    </tr>
  );
}

// ─── Subscribe modal ──────────────────────────────────────────────────────────

function SubscribeModal({
  onClose,
  onConfirm,
}: {
  onClose: () => void;
  onConfirm: (priority: number) => Promise<void>;
}) {
  const [priority, setPriority] = useState('1');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const submit = async () => {
    const p = parseInt(priority, 10);
    if (isNaN(p) || p < 1) {
      setError('Priority must be a positive integer (1 = highest).');
      return;
    }
    setLoading(true);
    setError('');
    try {
      await onConfirm(p);
      onClose();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '';
      setError(msg || 'Failed to subscribe. Check that the priority is unique.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-xl">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Subscribe to Strategy</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Assign a unique priority (1 = highest). Higher-priority strategies are evaluated first on each signal.
            </p>
          </div>
          <button onClick={onClose} className="rounded p-1 text-slate-400 hover:text-slate-700">
            <X className="h-4 w-4" />
          </button>
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-slate-700">Priority</label>
          <input
            type="number"
            min="1"
            step="1"
            value={priority}
            onChange={e => setPriority(e.target.value)}
            className="w-full rounded-md border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
          />
        </div>
        {error && <p className="mt-2 text-xs text-rose-600">{error}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            onClick={submit}
            disabled={loading}
            className="rounded-md bg-slate-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {loading ? 'Subscribing…' : 'Subscribe'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Backtest components ──────────────────────────────────────────────────────

const BACKTEST_STATUS_STYLES: Record<string, string> = {
  pending:   'bg-slate-100 text-slate-500 ring-slate-200',
  running:   'bg-blue-50 text-blue-600 ring-blue-200',
  completed: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  failed:    'bg-rose-50 text-rose-600 ring-rose-200',
  cancelled: 'bg-slate-100 text-slate-400 ring-slate-200',
};

const BACKTEST_MODE_LABELS: Record<string, string> = {
  signal_replay: 'Signal Replay',
  model_replay:  'Model Replay',
  hybrid:        'Hybrid',
};

function BacktestRunRow({ run }: { run: StrategyBacktestRun }) {
  const isLive = run.status === 'pending' || run.status === 'running';
  const metrics = run.result_metrics?.metrics as Record<string, number | null> | undefined;

  return (
    <div className="flex items-center justify-between gap-4 rounded-lg border border-slate-100 bg-white px-4 py-3">
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={cn(
              'rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider ring-1',
              BACKTEST_STATUS_STYLES[run.status] ?? 'bg-slate-100 text-slate-500',
            )}
          >
            {run.status}
          </span>
          <span className="text-[11px] text-slate-500">
            {BACKTEST_MODE_LABELS[run.mode] ?? run.mode}
          </span>
          <span className="text-[11px] text-slate-400">
            {new Date(run.start_dt).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' })}
            {' – '}
            {new Date(run.end_dt).toLocaleDateString('en-IN', { month: 'short', year: '2-digit' })}
          </span>
          <span className="text-[11px] text-slate-400">
            {run.symbols.slice(0, 3).join(', ')}{run.symbols.length > 3 ? ` +${run.symbols.length - 3}` : ''}
          </span>
        </div>

        {isLive && (
          <div className="mt-2 flex items-center gap-2">
            <Loader2 className="h-3 w-3 animate-spin text-blue-500" />
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-100">
              <div
                className="h-full rounded-full bg-blue-500 transition-all duration-500"
                style={{ width: `${run.progress_pct}%` }}
              />
            </div>
            <span className="text-[10px] text-slate-400">{run.progress_pct}%</span>
          </div>
        )}

        {run.status === 'completed' && metrics && (
          <div className="mt-1.5 flex flex-wrap gap-4 text-[11px] text-slate-500">
            <span><span className="font-medium text-slate-700">{metrics.total_trades}</span> trades</span>
            {metrics.win_rate_pct != null && (
              <span><span className="font-medium text-slate-700">{metrics.win_rate_pct.toFixed(1)}%</span> win rate</span>
            )}
            {metrics.sharpe_ratio != null && (
              <span>Sharpe <span className="font-medium text-slate-700">{metrics.sharpe_ratio.toFixed(2)}</span></span>
            )}
            {metrics.total_net_pnl != null && (
              <span className={metrics.total_net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-500'}>
                {metrics.total_net_pnl >= 0 ? '+' : ''}{metrics.total_net_pnl.toFixed(2)} P&L
              </span>
            )}
          </div>
        )}

        {run.status === 'failed' && run.error_detail && (
          <p className="mt-1 truncate text-[11px] text-rose-500">{run.error_detail}</p>
        )}
      </div>

      {run.status === 'completed' && (
        <Link
          href={`/strategies/${run.strategy_id}/backtests/${run.id}`}
          className="flex shrink-0 items-center gap-1 rounded-md border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
        >
          View Results
        </Link>
      )}
    </div>
  );
}

interface BacktestSectionProps {
  strategyId: string;
  symbols: string[];
}

function BacktestSection({ strategyId, symbols }: BacktestSectionProps) {
  const [runs, setRuns] = useState<StrategyBacktestRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [showModal, setShowModal] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const loadRuns = useCallback(async () => {
    try {
      const data = await strategiesAPI.listBacktests(strategyId);
      setRuns(data.items);
    } catch {
      // non-critical — keep stale list
    } finally {
      setLoading(false);
    }
  }, [strategyId]);

  // Auto-poll while any run is in-flight
  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    const hasLive = runs.some(r => r.status === 'pending' || r.status === 'running');
    if (hasLive && !pollRef.current) {
      pollRef.current = setInterval(loadRuns, 3000);
    } else if (!hasLive && pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
    return () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [runs, loadRuns]);

  const handleNewRun = async (
    syms: string[],
    startDt: string,
    endDt: string,
    mode: BacktestMode,
  ) => {
    await strategiesAPI.triggerBacktest(strategyId, {
      symbols: syms,
      start_dt: startDt,
      end_dt: endDt,
      mode,
    });
    setShowModal(false);
    setTimeout(loadRuns, 500);
  };

  return (
    <section aria-labelledby="backtest-heading">
      <div className="mb-4 flex items-center justify-between">
        <h2 id="backtest-heading" className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <FlaskConical className="h-4 w-4 text-slate-400" aria-hidden="true" />
          Backtests
        </h2>
        <button
          onClick={() => setShowModal(true)}
          className="flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
        >
          <PlayCircle className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
          New Run
        </button>
      </div>

      {loading ? (
        <div className="space-y-2">
          {[1, 2].map(i => (
            <div key={i} className="h-14 animate-pulse rounded-lg border border-slate-100 bg-slate-50" />
          ))}
        </div>
      ) : runs.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 px-6 py-12 text-center">
          <FlaskConical className="mb-3 h-8 w-8 text-slate-300" aria-hidden="true" />
          <p className="text-sm font-medium text-slate-600">No backtest runs yet</p>
          <p className="mt-1 text-xs text-slate-400">
            Run a historical simulation to evaluate strategy performance.
          </p>
          <button
            onClick={() => setShowModal(true)}
            className="mt-4 flex items-center gap-1.5 rounded-md bg-slate-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-slate-700"
          >
            <PlayCircle className="h-3.5 w-3.5" aria-hidden="true" />
            Start First Backtest
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          {runs.map(r => <BacktestRunRow key={r.id} run={r} />)}
        </div>
      )}

      {showModal && (
        <NewBacktestModal
          defaultSymbols={symbols}
          onClose={() => setShowModal(false)}
          onSubmit={handleNewRun}
        />
      )}
    </section>
  );
}

// ─── Symbol multi-picker (search + chips) ────────────────────────────────────

function SymbolSearchInput({
  symbols,
  onChange,
}: {
  symbols: string[];
  onChange: (symbols: string[]) => void;
}) {
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<UpstoxInstrument[]>([]);
  const [isOpen, setIsOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const runSearch = useCallback(async (q: string) => {
    if (q.trim().length < 1) { setResults([]); setIsOpen(false); return; }
    setSearching(true);
    try {
      const res = await upstoxAPI.searchInstruments(q.trim(), { segment: 'NSE_EQ', limit: 10 });
      const items = ((res as UpstoxInstrumentSearchResponse).results ?? []) as UpstoxInstrument[];
      setResults(items);
      setIsOpen(items.length > 0);
      setHighlightedIndex(items.length > 0 ? 0 : -1);
    } catch {
      setResults([]);
    } finally {
      setSearching(false);
    }
  }, []);

  const handleChange = (value: string) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (value.trim().length >= 1) {
      debounceRef.current = setTimeout(() => runSearch(value), 200);
    } else {
      setIsOpen(false);
      setResults([]);
    }
  };

  const addSymbol = (symbol: string) => {
    const upper = symbol.toUpperCase().trim();
    if (upper && !symbols.includes(upper)) onChange([...symbols, upper]);
    setQuery('');
    setIsOpen(false);
    setResults([]);
    inputRef.current?.focus();
  };

  const removeSymbol = (symbol: string) => onChange(symbols.filter(s => s !== symbol));

  useEffect(() => {
    const handlePointerDown = (e: PointerEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    window.addEventListener('pointerdown', handlePointerDown);
    return () => window.removeEventListener('pointerdown', handlePointerDown);
  }, []);

  return (
    <div ref={containerRef} className="relative">
      <div
        className="flex min-h-[2.5rem] flex-wrap gap-1.5 cursor-text rounded-lg border border-slate-200 bg-white px-2 py-1.5 focus-within:border-slate-400 focus-within:ring-1 focus-within:ring-slate-300"
        onClick={() => inputRef.current?.focus()}
      >
        {symbols.map(s => (
          <span key={s} className="flex items-center gap-1 rounded-md bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-700">
            {s}
            <button
              type="button"
              onClick={e => { e.stopPropagation(); removeSymbol(s); }}
              className="text-slate-400 hover:text-slate-700"
            >
              <X className="h-3 w-3" />
            </button>
          </span>
        ))}
        <div className="relative flex min-w-[8rem] flex-1 items-center">
          <Search className="pointer-events-none absolute left-1 h-3.5 w-3.5 shrink-0 text-slate-400" />
          <input
            ref={inputRef}
            value={query}
            onChange={e => handleChange(e.target.value)}
            onFocus={() => { if (results.length > 0) setIsOpen(true); }}
            onKeyDown={e => {
              if (e.key === 'Enter') {
                e.preventDefault();
                if (highlightedIndex >= 0 && results[highlightedIndex]) {
                  addSymbol(results[highlightedIndex].trading_symbol);
                } else if (query.trim()) {
                  addSymbol(query);
                }
              } else if (e.key === 'ArrowDown') {
                e.preventDefault();
                setHighlightedIndex(prev => Math.min(prev + 1, results.length - 1));
              } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                setHighlightedIndex(prev => Math.max(prev - 1, 0));
              } else if (e.key === 'Escape') {
                setIsOpen(false);
              } else if (e.key === 'Backspace' && !query && symbols.length > 0) {
                removeSymbol(symbols[symbols.length - 1]);
              }
            }}
            placeholder={symbols.length === 0 ? 'Search symbol or company…' : 'Add more…'}
            className="min-w-0 flex-1 bg-transparent pl-5 text-sm text-slate-800 placeholder-slate-400 outline-none"
          />
          {searching && <Loader2 className="h-3.5 w-3.5 animate-spin text-slate-400" />}
        </div>
      </div>

      {isOpen && results.length > 0 && (
        <div className="absolute z-50 mt-1 w-full overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
          <div className="max-h-52 overflow-y-auto">
            {results.map((inst, i) => {
              const already = symbols.includes(inst.trading_symbol);
              return (
                <button
                  key={inst.instrument_key}
                  type="button"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => { if (!already) addSymbol(inst.trading_symbol); }}
                  onMouseEnter={() => setHighlightedIndex(i)}
                  disabled={already}
                  className={cn(
                    'flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition-colors',
                    already ? 'cursor-not-allowed opacity-40' :
                    highlightedIndex === i ? 'bg-slate-100 text-slate-900' : 'text-slate-700 hover:bg-slate-50',
                  )}
                >
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold">{inst.trading_symbol}</p>
                    <p className="truncate text-[11px] text-slate-500">{inst.name || '—'}</p>
                  </div>
                  <span className="shrink-0 text-[10px] text-slate-400">{inst.exchange || inst.segment || '—'}</span>
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

// ─── New backtest modal ───────────────────────────────────────────────────────

interface NewBacktestModalProps {
  defaultSymbols: string[];
  onClose: () => void;
  onSubmit: (symbols: string[], startDt: string, endDt: string, mode: BacktestMode) => Promise<void>;
}

function NewBacktestModal({ defaultSymbols, onClose, onSubmit }: NewBacktestModalProps) {
  const today = new Date().toISOString().slice(0, 10);
  const oneYearAgo = new Date(Date.now() - 365 * 86400 * 1000).toISOString().slice(0, 10);

  const [symbols, setSymbols] = useState<string[]>(defaultSymbols);
  const [startDt, setStartDt] = useState(oneYearAgo);
  const [endDt, setEndDt] = useState(today);
  const [mode, setMode] = useState<BacktestMode>('signal_replay');
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleSubmit = async () => {
    setErr(null);
    if (symbols.length === 0) { setErr('Add at least one symbol.'); return; }
    if (!startDt || !endDt) { setErr('Select a date range.'); return; }
    if (startDt >= endDt) { setErr('Start date must be before end date.'); return; }
    setSubmitting(true);
    try {
      await onSubmit(symbols, startDt, endDt, mode);
    } catch {
      setErr('Failed to queue backtest. Please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-sm">
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white p-6 shadow-2xl">
        <div className="mb-5 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-slate-900">New Backtest Run</h3>
          <button onClick={onClose} className="rounded p-1 hover:bg-slate-100">
            <X className="h-4 w-4 text-slate-400" />
          </button>
        </div>

        <div className="space-y-4">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-600">Symbols</label>
            <SymbolSearchInput symbols={symbols} onChange={setSymbols} />
            <p className="mt-1 text-[10px] text-slate-400">
              Search by name or symbol · Enter or click to add · Backspace removes last
            </p>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-600">Start Date</label>
              <input
                type="date"
                value={startDt}
                onChange={e => setStartDt(e.target.value)}
                max={endDt}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 focus:border-slate-400 focus:outline-none"
              />
            </div>
            <div>
              <label className="mb-1.5 block text-xs font-medium text-slate-600">End Date</label>
              <input
                type="date"
                value={endDt}
                onChange={e => setEndDt(e.target.value)}
                min={startDt}
                max={today}
                className="w-full rounded-lg border border-slate-200 px-3 py-2 text-sm text-slate-800 focus:border-slate-400 focus:outline-none"
              />
            </div>
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium text-slate-600">Mode</label>
            <div className="grid grid-cols-3 gap-2">
              {(['signal_replay', 'model_replay', 'hybrid'] as BacktestMode[]).map(m => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={cn(
                    'rounded-lg border px-3 py-2 text-[11px] font-medium transition-colors',
                    mode === m
                      ? 'border-slate-900 bg-slate-900 text-white'
                      : 'border-slate-200 bg-white text-slate-600 hover:bg-slate-50',
                  )}
                >
                  {BACKTEST_MODE_LABELS[m]}
                </button>
              ))}
            </div>
            <p className="mt-1.5 text-[10px] text-slate-400">
              {mode === 'signal_replay' && 'Replay historical AI signals through the strategy filter pipeline.'}
              {mode === 'model_replay' && 'Re-run the ML ensemble on raw OHLCV bars for bar-level SL/TP resolution.'}
              {mode === 'hybrid' && 'Auto-splice: model replay before signal history boundary, signal replay after.'}
            </p>
          </div>

          {err && <p className="text-xs text-rose-500">{err}</p>}
        </div>

        <div className="mt-6 flex justify-end gap-2">
          <button
            onClick={onClose}
            className="rounded-md border border-slate-200 px-4 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={submitting}
            className="flex items-center gap-1.5 rounded-md bg-slate-900 px-4 py-1.5 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {submitting ? <Loader2 className="h-3 w-3 animate-spin" /> : <PlayCircle className="h-3 w-3" />}
            {submitting ? 'Queuing…' : 'Run Backtest'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

export default function StrategyDetailPage() {
  const { isAuthenticated, isAuthReady, user } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const searchParams = useSearchParams();
  const strategyId = params.id;
  const fromAdmin = searchParams.get('from') === 'admin';

  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [performance, setPerformance] = useState<StrategyPerformance | null>(null);
  const [trades, setTrades] = useState<StrategyTrade[]>([]);
  const [tradeTotal, setTradeTotal] = useState(0);
  const [tradePage, setTradePage] = useState(1);
  const [mySubscription, setMySubscription] = useState<UserStrategySubscription | null>(null);

  const [stratLoading, setStratLoading] = useState(true);
  const [perfLoading, setPerfLoading] = useState(true);
  const [tradesLoading, setTradesLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [showSubscribeModal, setShowSubscribeModal] = useState(false);
  const [actionLoading, setActionLoading] = useState(false);

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  // Load strategy + subscriptions in parallel
  useEffect(() => {
    if (!isAuthenticated || !strategyId) return;
    setStratLoading(true);

    Promise.all([
      strategiesAPI.get(strategyId),
      strategiesAPI.getSubscriptions(),
    ]).then(([s, subs]) => {
      setStrategy(s);
      const mine = subs.items.find(sub => sub.strategy_id === strategyId) ?? null;
      setMySubscription(mine);
    }).catch(() => {
      setError('Failed to load strategy.');
    }).finally(() => {
      setStratLoading(false);
    });
  }, [isAuthenticated, strategyId]);

  // Load performance independently
  useEffect(() => {
    if (!isAuthenticated || !strategyId) return;
    setPerfLoading(true);
    strategiesAPI.getPerformance(strategyId)
      .then(setPerformance)
      .catch(() => { /* non-critical — show empty metrics */ })
      .finally(() => setPerfLoading(false));
  }, [isAuthenticated, strategyId]);

  // Load trades — re-runs on page change
  const loadTrades = useCallback(async () => {
    if (!isAuthenticated || !strategyId) return;
    setTradesLoading(true);
    try {
      const res = await strategiesAPI.getTrades(strategyId, { page: tradePage, page_size: PAGE_SIZE });
      setTrades(res.items);
      setTradeTotal(res.total);
    } catch {
      // trades section shows empty state
    } finally {
      setTradesLoading(false);
    }
  }, [isAuthenticated, strategyId, tradePage]);

  useEffect(() => { loadTrades(); }, [loadTrades]);

  const handleSubscribe = async (priority: number) => {
    await strategiesAPI.subscribe(strategyId, { priority });
    const subs = await strategiesAPI.getSubscriptions();
    const mine = subs.items.find(sub => sub.strategy_id === strategyId) ?? null;
    setMySubscription(mine);
  };

  const handleUnsubscribe = async () => {
    if (!confirm('Unsubscribe from this strategy? Active activations will not be affected.')) return;
    setActionLoading(true);
    try {
      await strategiesAPI.unsubscribe(strategyId);
      setMySubscription(null);
    } catch {
      alert('Failed to unsubscribe. Please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  const handleArchive = async () => {
    if (!confirm('Archive this strategy? It will stop receiving signals.')) return;
    setActionLoading(true);
    try {
      await strategiesAPI.delete(strategyId);
      router.replace('/strategies');
    } catch {
      alert('Failed to archive strategy. Please try again.');
    } finally {
      setActionLoading(false);
    }
  };

  const isOwner = strategy && user && strategy.created_by === (user as UserProfile).id;
  const totalPages = Math.ceil(tradeTotal / PAGE_SIZE);

  // ── Error state ──────────────────────────────────────────────────────────────
  if (!stratLoading && error) {
    return (
      <div className="py-20 text-center">
        <p className="text-sm text-rose-600">{error}</p>
        <Link href={fromAdmin ? '/admin/strategies' : '/strategies'} className="mt-4 inline-block text-xs text-slate-500 underline">
          ← {fromAdmin ? 'Back to Governance' : 'Back to Strategies'}
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-8 py-6">

      {/* ── Breadcrumb ── */}
      <div>
        <Link
          href={fromAdmin ? '/admin/strategies' : '/strategies'}
          className="mb-3 inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {fromAdmin ? 'Governance' : 'Strategies'}
        </Link>

        {/* Header */}
        {stratLoading ? (
          <div className="animate-pulse space-y-2">
            <div className="h-5 w-60 rounded bg-slate-200" />
            <div className="h-3 w-96 rounded bg-slate-100" />
          </div>
        ) : strategy ? (
          <div className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="text-xl font-semibold text-slate-900">{strategy.name}</h1>
                <StatusBadge status={strategy.status} />
                {strategy.scope === 'shared' && (
                  <span className="rounded-full bg-blue-50 px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-blue-600 ring-1 ring-blue-200">
                    Library
                  </span>
                )}
                <span className="text-[10px] text-slate-400">v{strategy.version}</span>
              </div>

              {strategy.description && (
                <p className="mt-1.5 max-w-2xl text-sm text-slate-500">{strategy.description}</p>
              )}

              {strategy.tags && strategy.tags.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1">
                  {strategy.tags.map(tag => (
                    <span key={tag} className="flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-500">
                      <Tag className="h-2.5 w-2.5" aria-hidden="true" />
                      {tag}
                    </span>
                  ))}
                </div>
              )}

              <p className="mt-2 text-[11px] text-slate-400">
                Created {fmtDate(strategy.created_at)}
              </p>
            </div>

            {/* Action bar */}
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              <Link
                href={`/strategies/activations?strategy_id=${strategyId}`}
                className="flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
              >
                <Activity className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                Activations
              </Link>

              {isOwner && (
                <>
                  <Link
                    href={`/strategies/${strategyId}/edit`}
                    className="flex items-center gap-1.5 rounded-md border border-slate-200 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    <Pencil className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
                    Edit
                  </Link>
                  {strategy.status !== 'archived' && (
                    <button
                      onClick={handleArchive}
                      disabled={actionLoading}
                      className="flex items-center gap-1.5 rounded-md border border-rose-200 bg-rose-50 px-3 py-1.5 text-xs font-medium text-rose-600 hover:bg-rose-100 disabled:opacity-50"
                    >
                      <XCircle className="h-3.5 w-3.5" aria-hidden="true" />
                      Archive
                    </button>
                  )}
                </>
              )}

              {mySubscription ? (
                <div className="flex items-center gap-1.5">
                  {mySubscription.is_active ? (
                    <span className="flex items-center gap-1 rounded-md bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
                      <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                      P{mySubscription.priority} · Active
                    </span>
                  ) : (
                    <span className="flex items-center gap-1 rounded-md bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200">
                      <Pause className="h-3.5 w-3.5" aria-hidden="true" />
                      P{mySubscription.priority} · Paused
                    </span>
                  )}
                  <button
                    onClick={async () => {
                      setActionLoading(true);
                      try {
                        const updated = await strategiesAPI.setSubscriptionActive(strategyId, !mySubscription.is_active);
                        setMySubscription(prev => prev ? { ...prev, is_active: updated.is_active } : prev);
                      } catch {
                        alert('Failed to update subscription. Please try again.');
                      } finally {
                        setActionLoading(false);
                      }
                    }}
                    disabled={actionLoading}
                    className={cn(
                      'flex items-center gap-1 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50',
                      mySubscription.is_active
                        ? 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100'
                        : 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100',
                    )}
                  >
                    {actionLoading ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : mySubscription.is_active ? (
                      <Pause className="h-3.5 w-3.5" aria-hidden="true" />
                    ) : (
                      <Play className="h-3.5 w-3.5" aria-hidden="true" />
                    )}
                    {mySubscription.is_active ? 'Pause' : 'Resume'}
                  </button>
                  <button
                    onClick={handleUnsubscribe}
                    disabled={actionLoading}
                    className="rounded-md border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50 disabled:opacity-50"
                  >
                    Unsubscribe
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => setShowSubscribeModal(true)}
                  className="flex items-center gap-1.5 rounded-md bg-slate-900 px-3.5 py-1.5 text-xs font-semibold text-white hover:bg-slate-700"
                >
                  <Zap className="h-3.5 w-3.5" aria-hidden="true" />
                  Subscribe
                </button>
              )}
            </div>
          </div>
        ) : null}
      </div>

      {/* ── Performance metric cards ── */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Performance</h2>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
          <MetricCard
            label="Win Rate"
            value={fmtPct(performance?.win_rate_pct ?? null)}
            sub={`${performance?.total_trades ?? 0} trades`}
            positive={performance?.win_rate_pct != null ? performance.win_rate_pct >= 50 : null}
            loading={perfLoading}
          />
          <MetricCard
            label="Total P&L"
            value={performance ? fmtCurrency(performance.total_net_pnl) : '—'}
            positive={performance ? performance.total_net_pnl >= 0 : null}
            loading={perfLoading}
          />
          <MetricCard
            label="Sharpe"
            value={fmtRatio(performance?.sharpe_ratio ?? null)}
            sub="annualised"
            positive={performance?.sharpe_ratio != null ? performance.sharpe_ratio >= 1 : null}
            loading={perfLoading}
          />
          <MetricCard
            label="Sortino"
            value={fmtRatio(performance?.sortino_ratio ?? null)}
            sub="annualised"
            positive={performance?.sortino_ratio != null ? performance.sortino_ratio >= 1 : null}
            loading={perfLoading}
          />
          <MetricCard
            label="Max Drawdown"
            value={performance?.max_drawdown_pct != null ? `-${performance.max_drawdown_pct.toFixed(2)}%` : '—'}
            positive={performance?.max_drawdown_pct != null ? performance.max_drawdown_pct < 10 : null}
            loading={perfLoading}
          />
          <MetricCard
            label="Avg Hold"
            value={fmtDuration(performance?.avg_hold_duration_seconds ?? null)}
            loading={perfLoading}
          />
        </div>

        {/* Calmar + best/worst secondary strip */}
        {!perfLoading && performance && performance.total_trades > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-1 pl-1 text-xs text-slate-500">
            {performance.calmar_ratio != null && (
              <span>Calmar <span className="font-medium text-slate-700">{performance.calmar_ratio.toFixed(2)}</span></span>
            )}
            {performance.avg_daily_return_pct != null && (
              <span>Avg return/trade <span className="font-medium text-slate-700">
                {performance.avg_daily_return_pct >= 0 ? '+' : ''}{performance.avg_daily_return_pct.toFixed(3)}%
              </span></span>
            )}
            {performance.best_trade_pnl != null && (
              <span>Best trade <span className="font-medium text-emerald-600">{fmtCurrency(performance.best_trade_pnl)}</span></span>
            )}
            {performance.worst_trade_pnl != null && (
              <span>Worst trade <span className="font-medium text-rose-600">{fmtCurrency(performance.worst_trade_pnl)}</span></span>
            )}
          </div>
        )}
      </section>

      {/* ── Equity curve ── */}
      <section>
        <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-slate-400">Equity Curve</h2>
        <div className="rounded-xl border border-slate-200 bg-white p-4">
          <EquityCurveChart trades={trades} />
          {trades.length === 0 && !tradesLoading && (
            <p className="mt-2 text-center text-xs text-slate-400">
              Equity curve will appear once this strategy has closed trades.
            </p>
          )}
        </div>
      </section>

      {/* ── Trade history ── */}
      <section>
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
            Trade History
            {tradeTotal > 0 && (
              <span className="ml-2 normal-case font-normal text-slate-400">({tradeTotal} total)</span>
            )}
          </h2>
          {totalPages > 1 && (
            <div className="flex items-center gap-1">
              <button
                onClick={() => setTradePage(p => Math.max(1, p - 1))}
                disabled={tradePage === 1}
                className="rounded border border-slate-200 p-1 text-slate-500 hover:bg-slate-50 disabled:opacity-30"
              >
                <ChevronLeft className="h-3.5 w-3.5" />
              </button>
              <span className="text-xs text-slate-500">
                {tradePage} / {totalPages}
              </span>
              <button
                onClick={() => setTradePage(p => Math.min(totalPages, p + 1))}
                disabled={tradePage === totalPages}
                className="rounded border border-slate-200 p-1 text-slate-500 hover:bg-slate-50 disabled:opacity-30"
              >
                <ChevronRight className="h-3.5 w-3.5" />
              </button>
            </div>
          )}
        </div>

        {tradesLoading ? (
          <div className="space-y-1.5">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="h-10 animate-pulse rounded-lg border border-slate-200 bg-slate-50" />
            ))}
          </div>
        ) : trades.length === 0 ? (
          <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 px-6 py-14 text-center">
            <BarChart2 className="mb-3 h-8 w-8 text-slate-300" aria-hidden="true" />
            <p className="text-sm font-medium text-slate-600">No trades yet</p>
            <p className="mt-1 text-xs text-slate-400">
              Closed trades from this strategy will appear here.
            </p>
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
            <table className="min-w-full">
              <thead>
                <tr className="border-b border-slate-100">
                  {['Date', 'Symbol', 'Dir', 'Entry', 'Exit', 'P&L %', 'P&L ₹', 'Hold', 'Exit Reason'].map(h => (
                    <th
                      key={h}
                      className={cn(
                        'py-2.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400',
                        h === 'Date' ? 'pl-4 pr-3 text-left' :
                        h === 'Exit Reason' ? 'pl-3 pr-4 text-left' :
                        'px-3 text-left',
                      )}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.map(t => <TradeRow key={t.id} trade={t} />)}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* ── Backtests ── */}
      {strategy && (
        <BacktestSection
          strategyId={strategyId}
          symbols={strategy.definition.signal_gates.allowed_symbols}
        />
      )}

      {/* ── Subscribe modal ── */}
      {showSubscribeModal && (
        <SubscribeModal
          onClose={() => setShowSubscribeModal(false)}
          onConfirm={handleSubscribe}
        />
      )}
    </div>
  );
}
