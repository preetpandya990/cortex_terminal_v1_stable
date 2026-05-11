'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import {
  ArrowLeft,
  BarChart2,
  CheckCircle2,
  Clock,
  FlaskConical,
  Loader2,
  XCircle,
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
import { strategiesAPI } from '@/lib/api';
import type { StrategyBacktestRun } from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Types ────────────────────────────────────────────────────────────────────

interface BacktestMetrics {
  total_trades: number;
  win_rate_pct: number | null;
  sharpe_ratio: number | null;
  sortino_ratio: number | null;
  calmar_ratio: number | null;
  max_drawdown_pct: number | null;
  avg_daily_return_pct: number | null;
  total_net_pnl: number;
  best_trade_pnl: number | null;
  worst_trade_pnl: number | null;
}

interface EquityPoint {
  date: string;
  equity: number;
  pnl_pct: number;
}

interface SimTrade {
  symbol: string;
  direction: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  stop_loss: number | null;
  take_profit_1: number | null;
  exit_reason: string;
  entry_at: string;
  exit_at: string;
  gross_pnl: number;
  total_charges: number;
  net_pnl: number;
  pnl_pct: number;
  hold_duration_seconds: number;
}

interface ResultMetrics {
  mode: string;
  start_dt: string;
  end_dt: string;
  symbols: string[];
  metrics: BacktestMetrics;
  equity_curve: EquityPoint[];
  trades: SimTrade[];
  per_symbol_stats: Record<string, { wins: number; losses: number; net_pnl: number }>;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function fmtNum(v: number | null, decimals = 2): string {
  if (v == null) return '—';
  return v.toFixed(decimals);
}

function fmtPct(v: number | null): string {
  if (v == null) return '—';
  return `${v >= 0 ? '+' : ''}${v.toFixed(2)}%`;
}

function fmtDuration(secs: number): string {
  if (secs < 60) return `${secs.toFixed(0)}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  return `${Math.floor(secs / 86400)}d`;
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleDateString('en-IN', {
    day: '2-digit', month: 'short', year: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

// ─── Sub-components ───────────────────────────────────────────────────────────

function MetricCard({
  label,
  value,
  positive,
  sub,
}: {
  label: string;
  value: string;
  positive?: boolean | null;
  sub?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-100 bg-white p-4">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-slate-400">{label}</p>
      <p
        className={cn(
          'mt-1 text-xl font-bold',
          positive === true ? 'text-emerald-600' :
          positive === false ? 'text-rose-500' :
          'text-slate-900',
        )}
      >
        {value}
      </p>
      {sub && <p className="mt-0.5 text-[10px] text-slate-400">{sub}</p>}
    </div>
  );
}

const EXIT_COLORS: Record<string, string> = {
  TP1:            'bg-emerald-50 text-emerald-700 ring-emerald-200',
  SL:             'bg-rose-50 text-rose-600 ring-rose-200',
  SIGNAL_EXPIRED: 'bg-amber-50 text-amber-700 ring-amber-200',
  PERIOD_END:     'bg-slate-100 text-slate-500 ring-slate-200',
};

function SimTradeRow({ trade }: { trade: SimTrade }) {
  const isPos = trade.net_pnl >= 0;
  return (
    <tr className="border-b border-slate-50 last:border-0 hover:bg-slate-50/50">
      <td className="py-2.5 pl-4 pr-3 text-[11px] text-slate-500">{fmtDate(trade.entry_at)}</td>
      <td className="px-3 py-2.5 text-[11px] font-medium text-slate-700">{trade.symbol}</td>
      <td className="px-3 py-2.5">
        <span className={cn(
          'rounded-full px-2 py-0.5 text-[10px] font-semibold',
          trade.direction === 'LONG'
            ? 'bg-emerald-50 text-emerald-700'
            : 'bg-rose-50 text-rose-600',
        )}>
          {trade.direction}
        </span>
      </td>
      <td className="px-3 py-2.5 text-[11px] text-slate-600">{trade.entry_price.toFixed(2)}</td>
      <td className="px-3 py-2.5 text-[11px] text-slate-600">{trade.exit_price.toFixed(2)}</td>
      <td className={cn('px-3 py-2.5 text-[11px] font-medium', isPos ? 'text-emerald-600' : 'text-rose-500')}>
        {fmtPct(trade.pnl_pct)}
      </td>
      <td className={cn('px-3 py-2.5 text-[11px] font-medium', isPos ? 'text-emerald-600' : 'text-rose-500')}>
        {isPos ? '+' : ''}{trade.net_pnl.toFixed(2)}
      </td>
      <td className="px-3 py-2.5 text-[11px] text-slate-400">{fmtDuration(trade.hold_duration_seconds)}</td>
      <td className="py-2.5 pl-3 pr-4">
        <span className={cn(
          'rounded-full px-2 py-0.5 text-[10px] font-semibold ring-1',
          EXIT_COLORS[trade.exit_reason] ?? 'bg-slate-100 text-slate-500',
        )}>
          {trade.exit_reason}
        </span>
      </td>
    </tr>
  );
}

const STATUS_STYLES: Record<string, string> = {
  pending:   'bg-slate-100 text-slate-500',
  running:   'bg-blue-50 text-blue-600',
  completed: 'bg-emerald-50 text-emerald-700',
  failed:    'bg-rose-50 text-rose-600',
  cancelled: 'bg-slate-100 text-slate-400',
};

const MODE_LABELS: Record<string, string> = {
  signal_replay: 'Signal Replay',
  model_replay:  'Model Replay',
  hybrid:        'Hybrid',
};

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function BacktestResultPage() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string; runId: string }>();
  const { id: strategyId, runId } = params;

  const [run, setRun] = useState<StrategyBacktestRun | null>(null);
  const [loading, setLoading] = useState(true);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  const fetchRun = async () => {
    try {
      const data = await strategiesAPI.getBacktest(strategyId, runId);
      setRun(data);
      return data;
    } catch {
      return null;
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (!isAuthenticated) return;
    fetchRun();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated]);

  // Poll for live runs
  useEffect(() => {
    if (!run) return;
    const isLive = run.status === 'pending' || run.status === 'running';
    if (isLive && !pollRef.current) {
      pollRef.current = setInterval(async () => {
        const updated = await fetchRun();
        if (updated && updated.status !== 'pending' && updated.status !== 'running') {
          clearInterval(pollRef.current!);
          pollRef.current = null;
        }
      }, 2500);
    }
    return () => {
      if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.status]);

  if (loading) {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
      </div>
    );
  }

  if (!run) {
    return (
      <div className="py-20 text-center">
        <p className="text-sm text-rose-600">Backtest run not found.</p>
        <Link href={`/strategies/${strategyId}`} className="mt-4 inline-block text-xs text-slate-500 underline">
          ← Back to Strategy
        </Link>
      </div>
    );
  }

  const result = run.result_metrics as ResultMetrics | null;
  const metrics = result?.metrics;
  const equityCurve = result?.equity_curve ?? [];
  const trades = result?.trades ?? [];
  const perSymbol = result?.per_symbol_stats ?? {};
  const isLive = run.status === 'pending' || run.status === 'running';
  const finalEquity = equityCurve.length > 0 ? equityCurve[equityCurve.length - 1].equity : 100;
  const chartPositive = finalEquity >= 100;

  return (
    <div className="space-y-8 py-6">

      {/* ── Breadcrumb ── */}
      <div>
        <Link
          href={`/strategies/${strategyId}`}
          className="mb-3 inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Strategy
        </Link>

        <div className="flex flex-wrap items-center gap-3">
          <FlaskConical className="h-5 w-5 text-slate-400" />
          <h1 className="text-lg font-semibold text-slate-900">
            {MODE_LABELS[run.mode] ?? run.mode} Backtest
          </h1>
          <span className={cn(
            'rounded-full px-2.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider',
            STATUS_STYLES[run.status] ?? 'bg-slate-100 text-slate-500',
          )}>
            {run.status}
          </span>
        </div>

        <p className="mt-1.5 text-xs text-slate-400">
          {new Date(run.start_dt).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })}
          {' – '}
          {new Date(run.end_dt).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })}
          {' · '}
          {run.symbols.join(', ')}
        </p>
      </div>

      {/* ── Live progress ── */}
      {isLive && (
        <div className="rounded-xl border border-blue-100 bg-blue-50 p-5">
          <div className="mb-3 flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
            <span className="text-sm font-medium text-blue-700">
              {run.status === 'pending' ? 'Queued — waiting for worker…' : `Running… ${run.progress_pct}% complete`}
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-blue-100">
            <div
              className="h-full rounded-full bg-blue-500 transition-all duration-500"
              style={{ width: `${run.progress_pct}%` }}
            />
          </div>
        </div>
      )}

      {/* ── Failed state ── */}
      {run.status === 'failed' && (
        <div className="flex items-start gap-3 rounded-xl border border-rose-100 bg-rose-50 p-4">
          <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-500" />
          <div>
            <p className="text-sm font-medium text-rose-700">Backtest failed</p>
            {run.error_detail && (
              <p className="mt-1 font-mono text-xs text-rose-600">{run.error_detail}</p>
            )}
          </div>
        </div>
      )}

      {/* ── Results ── */}
      {run.status === 'completed' && metrics && (
        <>
          {/* Metrics grid */}
          <section aria-labelledby="metrics-heading">
            <h2 id="metrics-heading" className="mb-4 text-sm font-semibold text-slate-700">
              Performance Summary
            </h2>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              <MetricCard
                label="Total Trades"
                value={String(metrics.total_trades)}
              />
              <MetricCard
                label="Win Rate"
                value={metrics.win_rate_pct != null ? `${metrics.win_rate_pct.toFixed(1)}%` : '—'}
                positive={metrics.win_rate_pct != null ? metrics.win_rate_pct >= 50 : null}
              />
              <MetricCard
                label="Net P&L"
                value={fmtNum(metrics.total_net_pnl)}
                positive={metrics.total_net_pnl >= 0}
                sub="per unit"
              />
              <MetricCard
                label="Max Drawdown"
                value={metrics.max_drawdown_pct != null ? `${metrics.max_drawdown_pct.toFixed(2)}%` : '—'}
                positive={metrics.max_drawdown_pct != null ? metrics.max_drawdown_pct < 10 : null}
              />
              <MetricCard
                label="Sharpe Ratio"
                value={fmtNum(metrics.sharpe_ratio)}
                positive={metrics.sharpe_ratio != null ? metrics.sharpe_ratio >= 1 : null}
              />
              <MetricCard
                label="Sortino Ratio"
                value={fmtNum(metrics.sortino_ratio)}
                positive={metrics.sortino_ratio != null ? metrics.sortino_ratio >= 1 : null}
              />
              <MetricCard
                label="Calmar Ratio"
                value={fmtNum(metrics.calmar_ratio)}
                positive={metrics.calmar_ratio != null ? metrics.calmar_ratio >= 1 : null}
              />
              <MetricCard
                label="Avg Daily Return"
                value={metrics.avg_daily_return_pct != null ? `${fmtNum(metrics.avg_daily_return_pct)}%` : '—'}
                positive={metrics.avg_daily_return_pct != null ? metrics.avg_daily_return_pct >= 0 : null}
              />
              <MetricCard
                label="Best Trade"
                value={fmtNum(metrics.best_trade_pnl)}
                positive
              />
              <MetricCard
                label="Worst Trade"
                value={fmtNum(metrics.worst_trade_pnl)}
                positive={false}
              />
            </div>
          </section>

          {/* Equity curve */}
          {equityCurve.length > 0 && (
            <section aria-labelledby="equity-heading">
              <h2 id="equity-heading" className="mb-4 text-sm font-semibold text-slate-700">
                Equity Curve
              </h2>
              <div className="rounded-xl border border-slate-100 bg-white p-4 pb-2">
                <ResponsiveContainer width="100%" height={220}>
                  <AreaChart data={equityCurve} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                    <defs>
                      <linearGradient id="btEquityGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop
                          offset="5%"
                          stopColor={chartPositive ? '#10b981' : '#f43f5e'}
                          stopOpacity={0.25}
                        />
                        <stop
                          offset="95%"
                          stopColor={chartPositive ? '#10b981' : '#f43f5e'}
                          stopOpacity={0.02}
                        />
                      </linearGradient>
                    </defs>
                    <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" vertical={false} />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 10, fill: '#94a3b8' }}
                      tickLine={false}
                      axisLine={false}
                      interval="preserveStartEnd"
                    />
                    <YAxis
                      tick={{ fontSize: 10, fill: '#94a3b8' }}
                      tickLine={false}
                      axisLine={false}
                      tickFormatter={v => v.toFixed(0)}
                      width={36}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#fff',
                        border: '1px solid #e2e8f0',
                        borderRadius: 8,
                        fontSize: 11,
                        padding: '6px 10px',
                      }}
                      formatter={(value: unknown) => [typeof value === 'number' ? value.toFixed(2) : '—', 'Equity']}
                      labelFormatter={label => `Date: ${label}`}
                    />
                    <Area
                      type="monotone"
                      dataKey="equity"
                      stroke={chartPositive ? '#10b981' : '#f43f5e'}
                      strokeWidth={1.5}
                      fill="url(#btEquityGrad)"
                      dot={false}
                    />
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            </section>
          )}

          {/* Per-symbol stats */}
          {Object.keys(perSymbol).length > 0 && (
            <section aria-labelledby="per-symbol-heading">
              <h2 id="per-symbol-heading" className="mb-4 text-sm font-semibold text-slate-700">
                Per-Symbol Breakdown
              </h2>
              <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
                <table className="min-w-full">
                  <thead>
                    <tr className="border-b border-slate-100">
                      {['Symbol', 'Wins', 'Losses', 'Win Rate', 'Net P&L'].map(h => (
                        <th
                          key={h}
                          className={cn(
                            'py-2.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400',
                            h === 'Symbol' ? 'pl-4 pr-3 text-left' : 'px-3 text-left',
                          )}
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {Object.entries(perSymbol).map(([sym, s]) => {
                      const total = s.wins + s.losses;
                      const wr = total > 0 ? (s.wins / total * 100).toFixed(1) : '—';
                      return (
                        <tr key={sym} className="border-b border-slate-50 last:border-0 hover:bg-slate-50/50">
                          <td className="py-2.5 pl-4 pr-3 text-[11px] font-medium text-slate-700">{sym}</td>
                          <td className="px-3 py-2.5 text-[11px] text-emerald-600">{s.wins}</td>
                          <td className="px-3 py-2.5 text-[11px] text-rose-500">{s.losses}</td>
                          <td className="px-3 py-2.5 text-[11px] text-slate-600">{wr !== '—' ? `${wr}%` : '—'}</td>
                          <td className={cn('px-3 py-2.5 text-[11px] font-medium', s.net_pnl >= 0 ? 'text-emerald-600' : 'text-rose-500')}>
                            {s.net_pnl >= 0 ? '+' : ''}{s.net_pnl.toFixed(2)}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </section>
          )}

          {/* Trade log */}
          <section aria-labelledby="trades-heading">
            <div className="mb-4 flex items-center justify-between">
              <h2 id="trades-heading" className="flex items-center gap-2 text-sm font-semibold text-slate-700">
                <BarChart2 className="h-4 w-4 text-slate-400" aria-hidden="true" />
                Simulated Trade Log
                <span className="text-[11px] font-normal text-slate-400">({trades.length} trades)</span>
              </h2>
            </div>

            {trades.length === 0 ? (
              <div className="flex flex-col items-center justify-center rounded-xl border border-dashed border-slate-200 bg-slate-50 py-14 text-center">
                <BarChart2 className="mb-3 h-8 w-8 text-slate-300" />
                <p className="text-sm font-medium text-slate-600">No trades generated</p>
                <p className="mt-1 text-xs text-slate-400">
                  The strategy filters blocked all signals in this period.
                </p>
              </div>
            ) : (
              <div className="overflow-x-auto rounded-xl border border-slate-200 bg-white">
                <table className="min-w-full">
                  <thead>
                    <tr className="border-b border-slate-100">
                      {['Entry Date', 'Symbol', 'Dir', 'Entry', 'Exit', 'P&L %', 'P&L', 'Hold', 'Exit'].map(h => (
                        <th
                          key={h}
                          className={cn(
                            'py-2.5 text-[10px] font-semibold uppercase tracking-wider text-slate-400',
                            h === 'Entry Date' ? 'pl-4 pr-3 text-left' :
                            h === 'Exit' ? 'pl-3 pr-4 text-left' :
                            'px-3 text-left',
                          )}
                        >
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((t, i) => <SimTradeRow key={i} trade={t} />)}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Run metadata */}
          <div className="flex flex-wrap gap-4 rounded-xl border border-slate-100 bg-slate-50 px-4 py-3 text-[11px] text-slate-500">
            <span className="flex items-center gap-1">
              <CheckCircle2 className="h-3 w-3 text-emerald-500" />
              Completed {run.completed_at ? fmtDate(run.completed_at) : '—'}
            </span>
            {run.started_at && run.completed_at && (
              <span className="flex items-center gap-1">
                <Clock className="h-3 w-3" />
                {fmtDuration((new Date(run.completed_at).getTime() - new Date(run.started_at).getTime()) / 1000)} wall time
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
