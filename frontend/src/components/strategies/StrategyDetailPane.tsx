'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ChevronRight,
  Globe,
  Lock,
  Loader2,
  Pause,
  Play,
  Shield,
  ShieldOff,
  TrendingDown,
  TrendingUp,
  Users,
  X,
  Zap,
} from 'lucide-react';
import { strategiesAPI } from '@/lib/api';
import type {
  ConditionGroup,
  ConditionLeaf,
  ConditionNode,
  Strategy,
  StrategySummary,
} from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Field / operator human labels ────────────────────────────────────────────

const FIELD_LABELS: Record<string, string> = {
  ltp: 'LTP',
  close: 'Close',
  open: 'Open',
  high: 'High',
  low: 'Low',
  volume: 'Volume',
  volume_ratio: 'Volume Ratio',
  ema_9: 'EMA 9',
  ema_20: 'EMA 20',
  ema_21: 'EMA 21',
  ema_50: 'EMA 50',
  ema_200: 'EMA 200',
  sma_20: 'SMA 20',
  sma_50: 'SMA 50',
  rsi_14: 'RSI (14)',
  rsi_9: 'RSI (9)',
  macd: 'MACD',
  macd_signal: 'MACD Signal',
  macd_hist: 'MACD Histogram',
  bb_upper: 'Bollinger Upper',
  bb_lower: 'Bollinger Lower',
  bb_mid: 'Bollinger Mid',
  atr_14: 'ATR (14)',
  'signal.confidence': 'Signal Confidence',
  'signal.direction': 'Signal Direction',
  'signal.strength': 'Signal Strength',
};

const OP_LABELS: Record<string, string> = {
  '>': '>',
  '>=': '≥',
  '<': '<',
  '<=': '≤',
  '==': '=',
  '!=': '≠',
  between: 'between',
  crosses_above: 'crosses above',
  crosses_below: 'crosses below',
};

function fieldLabel(f: string): string {
  return FIELD_LABELS[f] ?? f.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
}

// ─── Condition tree renderer ───────────────────────────────────────────────────

function LeafNode({ node }: { node: ConditionLeaf }) {
  const field = fieldLabel(node.field);
  const op = OP_LABELS[node.op] ?? node.op;
  let value: string;
  if (Array.isArray(node.value)) {
    value = `${node.value[0]} and ${node.value[1]}`;
  } else if (typeof node.value === 'string') {
    value = FIELD_LABELS[node.value] ?? node.value;
  } else {
    value = String(node.value);
  }

  return (
    <div className="flex items-baseline gap-1.5 font-mono text-xs">
      <span className="font-semibold text-slate-800">{field}</span>
      <span className="text-slate-400">{op}</span>
      <span className="text-blue-700">{value}</span>
    </div>
  );
}

function GroupNode({ node, depth = 0 }: { node: ConditionGroup; depth?: number }) {
  const colors: Record<string, string> = {
    AND: 'text-emerald-700 bg-emerald-50 border-emerald-200',
    OR: 'text-amber-700 bg-amber-50 border-amber-200',
    NOT: 'text-rose-700 bg-rose-50 border-rose-200',
  };
  const label: Record<string, string> = { AND: 'ALL of', OR: 'ANY of', NOT: 'NOT' };
  const borderColor: Record<string, string> = {
    AND: 'border-emerald-200',
    OR: 'border-amber-200',
    NOT: 'border-rose-200',
  };

  return (
    <div className={cn('border-l-2 pl-3', borderColor[node.operator] ?? 'border-slate-200')}>
      <span
        className={cn(
          'mb-1.5 inline-block rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider border',
          colors[node.operator] ?? 'text-slate-600 bg-slate-50 border-slate-200',
        )}
      >
        {label[node.operator] ?? node.operator}
      </span>
      <div className="space-y-1.5">
        {node.children.map((child, i) => (
          <ConditionNodeView key={i} node={child} depth={depth + 1} />
        ))}
      </div>
    </div>
  );
}

function ConditionNodeView({ node, depth = 0 }: { node: ConditionNode; depth?: number }) {
  if (node.type === 'leaf') return <LeafNode node={node} />;
  return <GroupNode node={node} depth={depth} />;
}

// ─── Section wrapper ───────────────────────────────────────────────────────────

function Section({ title, children, className }: { title: string; children: React.ReactNode; className?: string }) {
  return (
    <div className={cn('space-y-3', className)}>
      <h3 className="flex items-center gap-2 text-[11px] font-bold uppercase tracking-widest text-slate-400">
        {title}
      </h3>
      {children}
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 text-xs">
      <span className="shrink-0 text-slate-500">{label}</span>
      <span className="text-right font-medium text-slate-800">{value}</span>
    </div>
  );
}

// ─── Stop loss display ─────────────────────────────────────────────────────────

function StopLossBlock({ sl }: { sl: Strategy['definition']['stop_loss'] }) {
  if (!sl) return <p className="text-xs text-slate-400 italic">Not configured (manual exit only)</p>;

  const typeLabel: Record<string, string> = {
    fixed_pct: `${sl.value}% below entry`,
    atr_multiple: `${sl.value}× ATR (period ${sl.atr_period})`,
    price_level: `Fixed price ₹${sl.value}`,
  };

  return (
    <div className="rounded-lg border border-rose-100 bg-rose-50 px-3.5 py-3">
      <div className="flex items-center gap-2 text-xs font-semibold text-rose-700">
        <TrendingDown className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
        <span>{typeLabel[sl.type] ?? sl.type}</span>
        {sl.trail && (
          <span className="ml-auto rounded bg-rose-100 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-rose-600">
            Trailing
          </span>
        )}
      </div>
    </div>
  );
}

// ─── Take profit display ───────────────────────────────────────────────────────

function TakeProfitBlock({ tp }: { tp: Strategy['definition']['take_profit'] }) {
  if (!tp || tp.levels.length === 0)
    return <p className="text-xs text-slate-400 italic">Not configured (manual exit only)</p>;

  return (
    <div className="space-y-1.5">
      {tp.levels.map((lvl, i) => (
        <div
          key={i}
          className="flex items-center justify-between rounded-lg border border-emerald-100 bg-emerald-50 px-3.5 py-2.5 text-xs"
        >
          <div className="flex items-center gap-2 font-semibold text-emerald-700">
            <TrendingUp className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span>TP{i + 1} · +{lvl.pct}%</span>
          </div>
          <span className="text-[11px] text-emerald-600">
            Exit {Math.round(lvl.exit_fraction * 100)}% of position
          </span>
        </div>
      ))}
    </div>
  );
}

// ─── Position sizing display ───────────────────────────────────────────────────

function SizingBlock({ ps }: { ps: Strategy['definition']['position_sizing'] }) {
  const modeLabel: Record<string, string> = {
    fixed_qty: `${ps.fixed_qty ?? '—'} shares (fixed)`,
    risk_pct: `Risk ${ps.risk_pct ?? '—'}% of portfolio per trade`,
    kelly: `Kelly × ${ps.kelly_fraction ?? '—'}`,
  };

  return (
    <div className="space-y-1.5 text-xs">
      <InfoRow label="Mode" value={modeLabel[ps.mode] ?? ps.mode} />
      <InfoRow label="Max position size" value={`${ps.max_position_pct}% of portfolio`} />
    </div>
  );
}

// ─── Signal gates display ──────────────────────────────────────────────────────

function SignalGatesBlock({ gates }: { gates: Strategy['definition']['signal_gates'] }) {
  const directionColors: Record<string, string> = {
    BUY: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    SELL: 'bg-rose-50 text-rose-700 border-rose-200',
  };

  return (
    <div className="space-y-2 text-xs">
      {/* Directions */}
      <div className="flex items-center gap-2">
        <span className="w-28 shrink-0 text-slate-500">Directions</span>
        <div className="flex flex-wrap gap-1">
          {gates.allowed_directions.map((d) => (
            <span
              key={d}
              className={cn(
                'rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider',
                directionColors[d] ?? 'bg-slate-50 text-slate-600 border-slate-200',
              )}
            >
              {d}
            </span>
          ))}
        </div>
      </div>

      {/* Timeframes */}
      {gates.allowed_timeframes.length > 0 && (
        <div className="flex items-center gap-2">
          <span className="w-28 shrink-0 text-slate-500">Timeframes</span>
          <div className="flex flex-wrap gap-1">
            {gates.allowed_timeframes.map((tf) => (
              <span key={tf} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                {tf}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Regimes */}
      {gates.allowed_regimes.length > 0 && (
        <div className="flex items-center gap-2">
          <span className="w-28 shrink-0 text-slate-500">Market regime</span>
          <div className="flex flex-wrap gap-1">
            {gates.allowed_regimes.map((r) => (
              <span key={r} className="rounded bg-blue-50 px-1.5 py-0.5 text-[10px] text-blue-600">
                {r.replace(/_/g, ' ')}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Allowed symbols */}
      {gates.allowed_symbols.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="w-28 shrink-0 text-slate-500">Allow only</span>
          <div className="flex flex-wrap gap-1">
            {gates.allowed_symbols.slice(0, 8).map((s) => (
              <span key={s} className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-700">
                {s}
              </span>
            ))}
            {gates.allowed_symbols.length > 8 && (
              <span className="text-[10px] text-slate-400">+{gates.allowed_symbols.length - 8} more</span>
            )}
          </div>
        </div>
      )}

      {/* Blocked symbols */}
      {gates.blocked_symbols.length > 0 && (
        <div className="flex items-start gap-2">
          <span className="w-28 shrink-0 text-slate-500">Blocked</span>
          <div className="flex flex-wrap gap-1">
            {gates.blocked_symbols.slice(0, 6).map((s) => (
              <span key={s} className="rounded bg-rose-50 px-1.5 py-0.5 text-[10px] font-medium text-rose-600">
                {s}
              </span>
            ))}
            {gates.blocked_symbols.length > 6 && (
              <span className="text-[10px] text-slate-400">+{gates.blocked_symbols.length - 6} more</span>
            )}
          </div>
        </div>
      )}

      {/* Market cap */}
      {gates.min_market_cap_cr != null && (
        <InfoRow label="Min market cap" value={`₹${gates.min_market_cap_cr} Cr`} />
      )}

      {/* Catch-all */}
      {gates.allowed_symbols.length === 0 &&
        gates.allowed_timeframes.length === 0 &&
        gates.allowed_regimes.length === 0 &&
        gates.blocked_symbols.length === 0 &&
        gates.min_market_cap_cr == null && (
          <p className="text-slate-400 italic">All symbols, timeframes, and regimes pass through.</p>
        )}
    </div>
  );
}

// ─── Guardrails display ────────────────────────────────────────────────────────

function GuardrailsBlock({ gr }: { gr: Strategy['definition']['guardrails'] }) {
  return (
    <div className="space-y-1.5 text-xs">
      <InfoRow label="Max concurrent trades" value={`${gr.max_concurrent_activations}`} />
      <InfoRow label="Min confidence score" value={`${gr.min_confidence_score}+`} />
      <InfoRow
        label="Volume confirmation"
        value={
          gr.require_volume_confirm ? (
            <span className="flex items-center gap-1 text-emerald-600">
              <CheckCircle2 className="h-3 w-3" />
              Required
            </span>
          ) : (
            <span className="text-slate-400">Not required</span>
          )
        }
      />
      {gr.allowed_sessions.length > 0 && (
        <InfoRow
          label="Sessions"
          value={gr.allowed_sessions.map((s) => s.replace(/_/g, ' ')).join(', ')}
        />
      )}
      {gr.max_daily_loss_pct != null && (
        <InfoRow
          label="Auto-pause at"
          value={
            <span className="text-rose-600">−{gr.max_daily_loss_pct}% daily loss</span>
          }
        />
      )}
    </div>
  );
}

// ─── Main pane ─────────────────────────────────────────────────────────────────

export interface StrategyDetailPaneProps {
  strategyId: string | null;
  onClose: () => void;
  subscribedIds: Set<string>;
  priorityMap: Map<string, number>;
  activeMap?: Map<string, boolean>;
  isAdmin: boolean;
  onSubscribeRequest: (strategy: StrategySummary) => void;
  onToggleActive?: (strategyId: string, currentlyActive: boolean) => Promise<void>;
  /** Optional query string appended to the "View full page" link, e.g. "?from=admin" */
  fullPageSuffix?: string;
}

export function StrategyDetailPane({
  strategyId,
  onClose,
  subscribedIds,
  priorityMap,
  activeMap,
  isAdmin,
  onSubscribeRequest,
  onToggleActive,
  fullPageSuffix = '',
}: StrategyDetailPaneProps) {
  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toggling, setToggling] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!strategyId) { setStrategy(null); return; }
    setLoading(true);
    setError(null);
    strategiesAPI.get(strategyId)
      .then(setStrategy)
      .catch(() => setError('Failed to load strategy details.'))
      .finally(() => setLoading(false));
  }, [strategyId]);

  // Keyboard close
  useEffect(() => {
    if (!strategyId) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [strategyId, onClose]);

  // Lock body scroll
  useEffect(() => {
    if (!strategyId) return;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = ''; };
  }, [strategyId]);

  const isOpen = !!strategyId;

  if (!isOpen) return null;

  const isSubscribed = strategy ? subscribedIds.has(strategy.id) : false;
  const priority = strategy ? priorityMap.get(strategy.id) : undefined;
  const subIsActive = strategy ? (activeMap?.get(strategy.id) ?? true) : true;

  const handleToggle = async () => {
    if (!strategy || !onToggleActive || toggling) return;
    setToggling(true);
    try { await onToggleActive(strategy.id, subIsActive); }
    finally { setToggling(false); }
  };
  const def = strategy?.definition;

  const statusColors: Record<string, string> = {
    active: 'bg-emerald-50 text-emerald-700',
    draft: 'bg-slate-100 text-slate-500',
    paused: 'bg-amber-50 text-amber-700',
    archived: 'bg-red-50 text-red-500',
  };

  return (
    <>
      {/* Backdrop */}
      <div
        className="fixed inset-0 z-40 bg-black/30 backdrop-blur-[2px]"
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Panel */}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={strategy?.name ?? 'Strategy details'}
        className={cn(
          'fixed right-0 top-0 z-50 flex h-full w-full flex-col bg-white shadow-2xl',
          'sm:w-[480px] lg:w-[520px]',
          'animate-in slide-in-from-right duration-200',
        )}
      >
        {/* ── Header ── */}
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-slate-200 px-5 py-4">
          <div className="min-w-0 flex-1">
            {loading ? (
              <div className="h-5 w-40 animate-pulse rounded bg-slate-100" />
            ) : strategy ? (
              <>
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-base font-semibold text-slate-900 leading-tight">{strategy.name}</h2>
                  <span
                    className={cn(
                      'rounded px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider',
                      statusColors[strategy.status] ?? statusColors.draft,
                    )}
                  >
                    {strategy.status}
                  </span>
                  {strategy.scope === 'shared' ? (
                    <span className="flex items-center gap-0.5 rounded bg-blue-50 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-blue-600">
                      <Globe className="h-2.5 w-2.5" />
                      Library
                    </span>
                  ) : (
                    <span className="flex items-center gap-0.5 rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
                      <Lock className="h-2.5 w-2.5" />
                      Private
                    </span>
                  )}
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-500">
                    v{strategy.version}
                  </span>
                </div>
              </>
            ) : null}
          </div>

          <div className="flex shrink-0 items-center gap-1">
            {strategy && (
              <Link
                href={`/strategies/${strategy.id}${fullPageSuffix}`}
                className="flex items-center gap-1 rounded-md border border-slate-200 px-2.5 py-1.5 text-[11px] font-medium text-slate-600 transition-colors hover:bg-slate-50"
              >
                Full page
                <ArrowRight className="h-3 w-3" aria-hidden="true" />
              </Link>
            )}
            <button
              onClick={onClose}
              className="rounded-md p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-700"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* ── Body ── */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center py-16">
              <Loader2 className="h-6 w-6 animate-spin text-slate-300" />
            </div>
          )}

          {error && (
            <div className="flex items-center gap-2 p-5 text-sm text-rose-600">
              <AlertTriangle className="h-4 w-4 shrink-0" />
              {error}
            </div>
          )}

          {strategy && def && (
            <div className="divide-y divide-slate-100">

              {/* ── Performance strip ── */}
              <div className="grid grid-cols-4 divide-x divide-slate-100 bg-slate-50">
                {[
                  {
                    label: 'Win Rate',
                    value: strategy.win_rate_pct != null ? `${strategy.win_rate_pct.toFixed(1)}%` : '—',
                    accent: strategy.win_rate_pct != null && strategy.win_rate_pct >= 50 ? 'text-emerald-600' : 'text-slate-700',
                  },
                  {
                    label: 'Trades',
                    value: strategy.total_trades > 0 ? strategy.total_trades : '—',
                    accent: 'text-slate-700',
                  },
                  {
                    label: 'Avg/Day',
                    value: strategy.avg_daily_return_pct != null
                      ? `${strategy.avg_daily_return_pct >= 0 ? '+' : ''}${strategy.avg_daily_return_pct.toFixed(2)}%`
                      : '—',
                    accent: strategy.avg_daily_return_pct != null
                      ? strategy.avg_daily_return_pct >= 0 ? 'text-emerald-600' : 'text-rose-600'
                      : 'text-slate-700',
                  },
                  {
                    label: 'Subs',
                    value: strategy.total_subscribers > 0 ? strategy.total_subscribers : '—',
                    accent: 'text-slate-700',
                  },
                ].map(({ label, value, accent }) => (
                  <div key={label} className="flex flex-col items-center py-3 text-center">
                    <span className={cn('text-base font-bold', accent)}>{value}</span>
                    <span className="mt-0.5 text-[10px] text-slate-400">{label}</span>
                  </div>
                ))}
              </div>

              {/* ── Subscribe / pause action ── */}
              {strategy.scope === 'shared' && (
                <div className="px-5 py-4">
                  {isSubscribed ? (
                    <div className="flex items-center gap-2">
                      {subIsActive ? (
                        <span className="flex items-center gap-1.5 rounded-md bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700 ring-1 ring-emerald-200">
                          <CheckCircle2 className="h-3.5 w-3.5" />
                          Active{priority != null ? ` · P${priority}` : ''}
                        </span>
                      ) : (
                        <span className="flex items-center gap-1.5 rounded-md bg-amber-50 px-3 py-1.5 text-xs font-medium text-amber-700 ring-1 ring-amber-200">
                          <Pause className="h-3.5 w-3.5" />
                          Paused{priority != null ? ` · P${priority}` : ''}
                        </span>
                      )}
                      {onToggleActive && (
                        <button
                          onClick={handleToggle}
                          disabled={toggling}
                          className={cn(
                            'flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-xs font-medium transition-colors disabled:opacity-50',
                            subIsActive
                              ? 'border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-100'
                              : 'border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-100',
                          )}
                        >
                          {toggling ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : subIsActive ? (
                            <Pause className="h-3.5 w-3.5" />
                          ) : (
                            <Play className="h-3.5 w-3.5" />
                          )}
                          {subIsActive ? 'Pause' : 'Resume'}
                        </button>
                      )}
                    </div>
                  ) : !isAdmin ? (
                    <button
                      onClick={() => onSubscribeRequest({
                        id: strategy.id,
                        name: strategy.name,
                        description: strategy.description,
                        tags: strategy.tags,
                        scope: strategy.scope,
                        status: strategy.status,
                        version: strategy.version,
                        total_subscribers: strategy.total_subscribers,
                        avg_daily_return_pct: strategy.avg_daily_return_pct,
                        win_rate_pct: strategy.win_rate_pct,
                        total_trades: strategy.total_trades,
                        created_at: strategy.created_at,
                      })}
                      className="flex items-center gap-2 rounded-lg bg-slate-900 px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-slate-700"
                    >
                      <Zap className="h-3.5 w-3.5 text-amber-400" aria-hidden="true" />
                      Subscribe to this strategy
                    </button>
                  ) : null}
                </div>
              )}

              {/* ── Overview ── */}
              <div className="space-y-4 px-5 py-5">
                <Section title="Overview">
                  {strategy.description ? (
                    <p className="text-xs leading-relaxed text-slate-600">{strategy.description}</p>
                  ) : (
                    <p className="text-xs italic text-slate-400">No description provided.</p>
                  )}

                  {strategy.tags && strategy.tags.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {strategy.tags.map((tag) => (
                        <span key={tag} className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500">
                          #{tag}
                        </span>
                      ))}
                    </div>
                  )}

                  <div className="space-y-1.5">
                    <InfoRow label="Scope" value={strategy.scope === 'shared' ? 'Global library' : 'Private'} />
                    <InfoRow label="Version" value={`v${strategy.version}`} />
                    <InfoRow
                      label="Created"
                      value={new Date(strategy.created_at).toLocaleDateString('en-IN', {
                        day: 'numeric',
                        month: 'short',
                        year: 'numeric',
                      })}
                    />
                    <InfoRow
                      label="Last updated"
                      value={new Date(strategy.updated_at).toLocaleDateString('en-IN', {
                        day: 'numeric',
                        month: 'short',
                        year: 'numeric',
                      })}
                    />
                  </div>
                </Section>
              </div>

              {/* ── Signal Gates ── */}
              <div className="px-5 py-5">
                <Section title="Signal Filters">
                  <SignalGatesBlock gates={def.signal_gates} />
                </Section>
              </div>

              {/* ── Entry Conditions ── */}
              {(def.signal_condition || def.indicator_condition) && (
                <div className="space-y-4 px-5 py-5">
                  <Section title="Entry Conditions">
                    {def.signal_condition && (
                      <div className="space-y-1.5">
                        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                          Signal conditions
                        </p>
                        <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
                          <ConditionNodeView node={def.signal_condition} />
                        </div>
                      </div>
                    )}
                    {def.indicator_condition && (
                      <div className="space-y-1.5">
                        <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                          Indicator conditions
                        </p>
                        <div className="rounded-lg border border-slate-100 bg-slate-50 p-3">
                          <ConditionNodeView node={def.indicator_condition} />
                        </div>
                      </div>
                    )}
                  </Section>
                </div>
              )}

              {/* ── Exit Rules ── */}
              <div className="space-y-4 px-5 py-5">
                <Section title="Exit Rules">
                  <div className="space-y-1.5">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Stop loss
                    </p>
                    <StopLossBlock sl={def.stop_loss} />
                  </div>
                  <div className="space-y-1.5">
                    <p className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                      Take profit
                    </p>
                    <TakeProfitBlock tp={def.take_profit} />
                  </div>
                </Section>
              </div>

              {/* ── Position Sizing ── */}
              <div className="px-5 py-5">
                <Section title="Position Sizing">
                  <SizingBlock ps={def.position_sizing} />
                </Section>
              </div>

              {/* ── Guardrails ── */}
              <div className="px-5 py-5">
                <Section title="Guardrails">
                  <GuardrailsBlock gr={def.guardrails} />
                </Section>
              </div>

              {/* ── Footer link ── */}
              <div className="px-5 py-4">
                <Link
                  href={`/strategies/${strategy.id}${fullPageSuffix}`}
                  className="flex items-center justify-center gap-2 rounded-lg border border-slate-200 py-2.5 text-xs font-medium text-slate-600 transition-colors hover:bg-slate-50"
                >
                  View full details, backtests &amp; trade history
                  <ChevronRight className="h-3.5 w-3.5 text-slate-400" />
                </Link>
              </div>

            </div>
          )}
        </div>
      </div>
    </>
  );
}
