"use client";

/**
 * SignalDetailModal — Full signal audit view.
 *
 * Custom overlay (no shadcn Dialog) — consistent with PositionDetailModal.
 * Sections: header · confidence + price levels · AI reasoning ·
 *           market events · ML predictions · technical indicators ·
 *           signal history (audit trail).
 */

import { type ReactNode, useCallback } from "react";
import {
  X,
  TrendingUp,
  TrendingDown,
  Minus,
  AlertCircle,
  ExternalLink,
  Clock,
  Brain,
  Activity,
  BarChart3,
  Newspaper,
  Plus,
  Check,
  Loader2,
} from "lucide-react";
import { isPast, differenceInHours } from "date-fns";
import { useSignalAudit } from "@/hooks/useSignals";
import { useWatchlist } from "@/hooks/useWatchlist";
import { useAuth } from "@/contexts/AuthContext";
import { Button } from "@/components/ui/button";
import { EVENT_TYPE_META } from "@/types/events";
import {
  SignalType,
  type ContributingEvent,
  type ContributingMLPrediction,
  type ContributingTechnical,
  type SignalAuditEntry,
  type TradingSignal,
} from "@/types/signals";

// ── Formatters ────────────────────────────────────────────────────────────────

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmtIST(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(d);
}

function toTitleCase(s: string): string {
  return s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// ── Direction meta ────────────────────────────────────────────────────────────

interface DirectionMeta {
  icon: (size: "sm" | "md") => ReactNode;
  label: string;
  iconBg: string;
  badgeCls: string;
  pillCls: string;
}

const DIRECTION_META: Record<SignalType, DirectionMeta> = {
  [SignalType.BUY]: {
    icon: (size) => (
      <TrendingUp className={size === "md" ? "h-[18px] w-[18px] text-emerald-700" : "h-3.5 w-3.5 text-emerald-700"} />
    ),
    label: "BUY",
    iconBg: "bg-emerald-100",
    badgeCls: "bg-emerald-100 text-emerald-700",
    pillCls: "bg-emerald-50 border-emerald-200 text-emerald-700",
  },
  [SignalType.SELL]: {
    icon: (size) => (
      <TrendingDown className={size === "md" ? "h-[18px] w-[18px] text-rose-700" : "h-3.5 w-3.5 text-rose-700"} />
    ),
    label: "SELL",
    iconBg: "bg-rose-100",
    badgeCls: "bg-rose-100 text-rose-700",
    pillCls: "bg-rose-50 border-rose-200 text-rose-700",
  },
  [SignalType.HOLD]: {
    icon: (size) => (
      <Minus className={size === "md" ? "h-[18px] w-[18px] text-amber-700" : "h-3.5 w-3.5 text-amber-700"} />
    ),
    label: "HOLD",
    iconBg: "bg-amber-100",
    badgeCls: "bg-amber-100 text-amber-700",
    pillCls: "bg-amber-50 border-amber-200 text-amber-700",
  },
};

// ── Regime meta ───────────────────────────────────────────────────────────────

const REGIME_META: Record<string, { label: string; cls: string }> = {
  bull_trending:   { label: "Bull Trending",    cls: "bg-emerald-100 text-emerald-700" },
  bear_trending:   { label: "Bear Trending",    cls: "bg-rose-100 text-rose-700" },
  sideways_range:  { label: "Sideways / Range", cls: "bg-blue-100 text-blue-700" },
  high_volatility: { label: "High Volatility",  cls: "bg-amber-100 text-amber-700" },
  low_liquidity:   { label: "Low Liquidity",    cls: "bg-slate-100 text-slate-600" },
  news_driven:     { label: "News Driven",      cls: "bg-violet-100 text-violet-700" },
};

// ── Horizon meta ──────────────────────────────────────────────────────────────

const HORIZON_META: Record<string, { label: string; cls: string }> = {
  intraday:   { label: "Intraday",   cls: "bg-blue-100 text-blue-700" },
  swing:      { label: "Swing",      cls: "bg-purple-100 text-purple-700" },
  positional: { label: "Positional", cls: "bg-cyan-100 text-cyan-700" },
};

// ── Technical indicator display names ─────────────────────────────────────────

const INDICATOR_LABELS: Record<string, string> = {
  rsi_14: "RSI (14)",
  ema_20: "EMA 20",
  ema_50: "EMA 50",
};

const SIGNAL_BADGE: Record<string, { label: string; cls: string; accentCls: string }> = {
  bullish: { label: "Bullish", cls: "bg-emerald-50 border-emerald-200 text-emerald-700", accentCls: "border-l-emerald-400" },
  bearish: { label: "Bearish", cls: "bg-rose-50 border-rose-200 text-rose-700",         accentCls: "border-l-rose-400" },
  neutral: { label: "Neutral", cls: "bg-slate-100 border-slate-200 text-slate-600",     accentCls: "border-l-slate-300" },
};

// ── Section header (with trailing divider line) ───────────────────────────────

function SectionHeader({
  icon,
  label,
  count,
}: {
  icon: ReactNode;
  label: string;
  count?: number;
}) {
  return (
    <div className="flex items-center gap-2 mb-3">
      <span className="text-slate-400">{icon}</span>
      <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        {label}
      </span>
      {count !== undefined && (
        <span className="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-bold text-slate-500">
          {count}
        </span>
      )}
      <div className="flex-1 border-t border-slate-100" />
    </div>
  );
}

// ── Confidence bar with level pill ────────────────────────────────────────────

function ConfidenceBar({ value }: { value: number }) {
  const pct     = Math.min(100, Math.max(0, value * 100));
  const barCls  = value >= 0.8 ? "bg-emerald-500" : value >= 0.6 ? "bg-amber-500" : "bg-rose-500";
  const textCls = value >= 0.8 ? "text-emerald-700" : value >= 0.6 ? "text-amber-700" : "text-rose-700";
  const level   = value >= 0.8 ? "HIGH" : value >= 0.6 ? "MED" : "LOW";
  const pillCls = value >= 0.8
    ? "bg-emerald-50 border-emerald-200 text-emerald-700"
    : value >= 0.6
    ? "bg-amber-50 border-amber-200 text-amber-700"
    : "bg-rose-50 border-rose-200 text-rose-700";

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
          Calibrated Confidence
        </span>
        <div className="flex items-center gap-2">
          <span className={`rounded-md border px-2 py-0.5 text-[10px] font-bold ${pillCls}`}>
            {level}
          </span>
          <span className={`text-base font-bold tabular-nums ${textCls}`}>
            {pct.toFixed(1)}%
          </span>
        </div>
      </div>
      <div className="h-3 rounded-full bg-slate-100 overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barCls}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Expiry badge ──────────────────────────────────────────────────────────────

function ExpiryBadge({ expiresAt }: { expiresAt: string }) {
  const d         = new Date(expiresAt);
  const isExpired = isPast(d);
  const isSoon    = !isExpired && differenceInHours(d, new Date()) <= 2;

  if (isExpired) return (
    <span className="inline-flex items-center gap-1 rounded-full bg-rose-100 px-2 py-0.5 text-[11px] font-semibold text-rose-700">
      <Clock className="h-3 w-3" /> Expired
    </span>
  );
  if (isSoon) return (
    <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-[11px] font-semibold text-amber-700">
      <Clock className="h-3 w-3" /> Expiring soon
    </span>
  );
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2 py-0.5 text-[11px] font-semibold text-emerald-700">
      <Clock className="h-3 w-3" /> Active
    </span>
  );
}

// ── Impact bar (0–1 scale for ContributingEvent) ──────────────────────────────

function ImpactBar({ score }: { score: number }) {
  const pct    = Math.min(100, Math.max(0, score * 100));
  const barCls = score >= 0.7 ? "bg-rose-500" : score >= 0.4 ? "bg-amber-500" : "bg-slate-400";
  const label  = score >= 0.7 ? "High" : score >= 0.4 ? "Med" : "Low";
  const labelCls = score >= 0.7 ? "text-rose-600" : score >= 0.4 ? "text-amber-600" : "text-slate-500";

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden">
        <div className={`h-full rounded-full ${barCls}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[10px] font-semibold tabular-nums w-6 text-right ${labelCls}`}>
        {pct.toFixed(0)}
      </span>
      <span className={`text-[10px] font-semibold w-6 ${labelCls}`}>{label}</span>
    </div>
  );
}

// ── Event card (Market Events section) ───────────────────────────────────────

function EventCard({ event }: { event: ContributingEvent }) {
  const title    = event.article_title || toTitleCase(event.event_type);
  const typeKey  = event.event_type as keyof typeof EVENT_TYPE_META;
  const typeMeta = EVENT_TYPE_META[typeKey] ?? {
    label: toTitleCase(event.event_type),
    badgeCls: "bg-slate-100 border-slate-200 text-slate-600",
  };

  return (
    <div className="rounded-xl border border-slate-200 bg-white px-4 py-3.5 space-y-2">

      {/* Article title (primary) + source link */}
      <div className="flex items-start justify-between gap-2">
        <p className="text-sm font-semibold leading-snug text-slate-900 line-clamp-2 flex-1">
          {title}
        </p>
        {event.source_url && (
          <a
            href={event.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="shrink-0 mt-0.5 rounded-md p-1 text-slate-400 hover:bg-slate-100 hover:text-blue-600 transition-colors"
            aria-label="Open source article"
          >
            <ExternalLink className="h-3.5 w-3.5" />
          </a>
        )}
      </div>

      {/* Source name */}
      {event.source_name && (
        <div className="flex items-center gap-1 text-[11px] text-slate-500">
          <Newspaper className="h-3 w-3 shrink-0" />
          {event.source_name}
        </div>
      )}

      {/* Summary excerpt */}
      {event.summary && event.summary !== title && (
        <p className="text-xs leading-relaxed text-slate-600 line-clamp-2">
          {event.summary}
        </p>
      )}

      {/* Event type badge + impact bar */}
      <div className="flex items-center gap-3 pt-0.5">
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[10px] font-semibold ${typeMeta.badgeCls}`}>
          {typeMeta.label}
        </span>
        <div className="flex-1">
          <ImpactBar score={event.impact_score} />
        </div>
      </div>

    </div>
  );
}

// ── ML prediction card ────────────────────────────────────────────────────────

function MLPredCard({ pred }: { pred: ContributingMLPrediction }) {
  const pct      = Math.min(100, Math.max(0, pred.confidence * 100));
  const predKey  = pred.prediction.toLowerCase();
  const badge    = SIGNAL_BADGE[predKey] ?? SIGNAL_BADGE["neutral"];
  const barCls   =
    predKey === "bullish" ? "bg-emerald-500" :
    predKey === "bearish" ? "bg-rose-500" : "bg-slate-400";

  return (
    <div className={`rounded-xl border border-l-[3px] border-slate-200 bg-white px-4 py-3.5 ${badge.accentCls}`}>
      <div className="flex items-center justify-between mb-3">
        <span className="text-sm font-semibold text-slate-900">{pred.model_name}</span>
        <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-semibold ${badge.cls}`}>
          {badge.label}
        </span>
      </div>
      <div className="space-y-1.5">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-500">Model Confidence</span>
          <span className="font-semibold tabular-nums text-slate-700">{pct.toFixed(1)}%</span>
        </div>
        <div className="h-2 rounded-full bg-slate-100 overflow-hidden">
          <div className={`h-full rounded-full ${barCls}`} style={{ width: `${pct}%` }} />
        </div>
      </div>
    </div>
  );
}

// ── Technical indicator card ──────────────────────────────────────────────────

function TechIndicatorCard({ tech }: { tech: ContributingTechnical }) {
  const key   = tech.indicator.toLowerCase();
  const isRsi = key.includes("rsi");
  const isEma = key.includes("ema");
  const label = INDICATOR_LABELS[key] ?? toTitleCase(tech.indicator);

  let badge = SIGNAL_BADGE[tech.signal.toLowerCase()] ?? SIGNAL_BADGE["neutral"];
  if (isRsi) {
    if (tech.value >= 70)      badge = { label: "Overbought", cls: "bg-rose-50 border-rose-200 text-rose-700",         accentCls: "border-l-rose-400" };
    else if (tech.value <= 30) badge = { label: "Oversold",   cls: "bg-emerald-50 border-emerald-200 text-emerald-700", accentCls: "border-l-emerald-400" };
  }

  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3.5 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1.5">
        {label}
      </div>
      <div className="text-base font-bold text-slate-900 tabular-nums mb-2.5">
        {isEma ? INR.format(tech.value) : tech.value.toFixed(2)}
      </div>
      <span className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] font-semibold ${badge.cls}`}>
        {badge.label}
      </span>
    </div>
  );
}

// ── Audit trail entry ─────────────────────────────────────────────────────────

function outcomeCls(outcome: string): string {
  const u = outcome.toUpperCase();
  if (u.includes("TP") || u.includes("HIT") || u.includes("PROFIT"))
    return "bg-emerald-50 border-emerald-200 text-emerald-700";
  if (u.includes("SL") || u.includes("STOP") || u.includes("LOSS"))
    return "bg-rose-50 border-rose-200 text-rose-700";
  if (u.includes("EXPIRE"))
    return "bg-slate-100 border-slate-200 text-slate-500";
  return "bg-slate-100 border-slate-200 text-slate-600";
}

function AuditEntry({
  entry,
  isLast,
}: {
  entry: SignalAuditEntry;
  isLast: boolean;
}) {
  const meta = DIRECTION_META[entry.signal_type as SignalType] ?? DIRECTION_META[SignalType.HOLD];
  const pct  = (entry.confidence * 100).toFixed(1);

  return (
    <div className="flex gap-3">
      {/* Stem */}
      <div className="flex flex-col items-center">
        <div className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${meta.iconBg}`}>
          {meta.icon("sm")}
        </div>
        {!isLast && (
          <div className="mt-1 w-[2px] min-h-[16px] flex-1 bg-slate-200" />
        )}
      </div>

      {/* Content */}
      <div className={`flex-1 ${!isLast ? "pb-4" : "pb-1"}`}>
        <div className="flex flex-wrap items-center gap-1.5">
          <span className={`rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${meta.pillCls}`}>
            {entry.signal_type.toUpperCase()}
          </span>
          <span className="text-xs font-semibold tabular-nums text-slate-600">
            {pct}%
          </span>
          {entry.outcome && (
            <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold ${outcomeCls(entry.outcome)}`}>
              {toTitleCase(entry.outcome)}
            </span>
          )}
        </div>
        <p className="mt-1 text-[11px] text-slate-400">{fmtIST(entry.generated_at)}</p>
      </div>
    </div>
  );
}

// ── Public component ──────────────────────────────────────────────────────────

export interface SignalDetailModalProps {
  signal: TradingSignal;
  onClose: () => void;
}

export function SignalDetailModal({ signal, onClose }: SignalDetailModalProps) {
  const { data: auditData } = useSignalAudit(signal.signal_id);
  const { isAuthenticated } = useAuth();
  const {
    items: watchlistItems,
    addToWatchlist,
    removeFromWatchlist,
    isAdding,
    isRemoving,
  } = useWatchlist();

  const watchlistEntry = signal.instrument_key
    ? watchlistItems.find((i) => i.instrument_key === signal.instrument_key)
    : undefined;
  const inWatchlist = !!watchlistEntry;
  const watchlistItemId = watchlistEntry?.id ?? null;

  const handleToggleWatchlist = useCallback(async () => {
    if (!isAuthenticated || !signal.instrument_key) return;
    try {
      if (inWatchlist && watchlistItemId !== null) {
        await removeFromWatchlist(watchlistItemId);
      } else {
        await addToWatchlist({
          instrument_key: signal.instrument_key,
          trading_symbol: signal.symbol,
          name: signal.company_name ?? undefined,
          exchange: "NSE",
        });
      }
    } catch (error) {
      console.error("[SignalDetailModal] Failed to toggle watchlist:", error);
    }
  }, [
    isAuthenticated,
    inWatchlist,
    watchlistItemId,
    removeFromWatchlist,
    addToWatchlist,
    signal,
  ]);

  const dirMeta =
    DIRECTION_META[signal.signal_type] ?? DIRECTION_META[SignalType.HOLD];
  const regimeMeta =
    REGIME_META[signal.regime_type] ?? {
      label: toTitleCase(signal.regime_type),
      cls: "bg-slate-100 text-slate-600",
    };
  const horizonMeta =
    HORIZON_META[signal.time_horizon] ?? {
      label: toTitleCase(signal.time_horizon),
      cls: "bg-slate-100 text-slate-600",
    };

  const { events, ml_predictions, technical } = signal.contributing_factors;
  const hasEvents = events.length > 0;
  const hasML     = ml_predictions.length > 0;
  const hasTech   = technical.length > 0;
  const auditLog  = auditData?.audit_log ?? [];
  const hasAudit  = auditLog.length > 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/50 backdrop-blur-sm p-0 sm:p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="relative w-full sm:max-w-3xl max-h-[92dvh] flex flex-col rounded-t-2xl sm:rounded-2xl border border-slate-200 bg-white shadow-2xl overflow-hidden">

        {/* ── Header ──────────────────────────────────────────────────── */}
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 bg-white px-5 py-4 shrink-0">
          <div className="flex items-start gap-3 min-w-0">
            <div className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${dirMeta.iconBg}`}>
              {dirMeta.icon("md")}
            </div>

            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <h2 className="text-lg font-bold text-slate-900">{signal.symbol}</h2>
                <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-bold ${dirMeta.badgeCls}`}>
                  {dirMeta.label}
                </span>
                <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${horizonMeta.cls}`}>
                  {horizonMeta.label}
                </span>
                <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${regimeMeta.cls}`}>
                  {regimeMeta.label}
                </span>
                {!signal.is_nse_eligible && (
                  <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2.5 py-0.5 text-[11px] font-semibold text-amber-700">
                    <AlertCircle className="h-3 w-3" />
                    Informational
                  </span>
                )}
              </div>

              {signal.company_name && (
                <p className="mt-0.5 text-sm text-slate-500 truncate">{signal.company_name}</p>
              )}

              <p className="mt-1 text-[11px] text-slate-400">
                Generated {fmtIST(signal.generated_at)}
                {" · "}
                Expires {fmtIST(signal.expires_at)}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {isAuthenticated && signal.is_nse_eligible && signal.instrument_key && (
              <Button
                variant={inWatchlist ? "outline" : "default"}
                size="sm"
                onClick={handleToggleWatchlist}
                disabled={isAdding || isRemoving}
                className="gap-2"
              >
                {isAdding || isRemoving ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : inWatchlist ? (
                  <Check className="h-4 w-4" />
                ) : (
                  <Plus className="h-4 w-4" />
                )}
                {inWatchlist ? "In Watchlist" : "Add to Watchlist"}
              </Button>
            )}
            <button
              onClick={onClose}
              className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
              aria-label="Close"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* ── Scrollable body ─────────────────────────────────────────── */}
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5">

          {/* Non-NSE disclaimer */}
          {!signal.is_nse_eligible && (
            <div className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
              <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
              <div>
                <p className="text-sm font-semibold text-amber-800">Informational Signal Only</p>
                <p className="mt-0.5 text-xs text-amber-700">
                  {signal.symbol} is not listed as an NSE equity on this platform. This signal was
                  generated from market news and cannot be used for trade execution.
                </p>
              </div>
            </div>
          )}

          {/* ── Confidence + Price Levels ─────────────────────────── */}
          <div className="rounded-xl border border-slate-200 bg-slate-50 px-4 py-4 space-y-4">
            <ConfidenceBar value={signal.calibrated_confidence} />

            {(signal.target_price != null || signal.stop_loss != null) && (
              <div className="grid grid-cols-3 gap-3">
                <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                    Target Price
                  </div>
                  <div className={`mt-1 text-sm font-bold tabular-nums ${signal.target_price != null ? "text-emerald-600" : "text-slate-300"}`}>
                    {signal.target_price != null ? INR.format(signal.target_price) : "—"}
                  </div>
                </div>

                <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                    Stop Loss
                  </div>
                  <div className={`mt-1 text-sm font-bold tabular-nums ${signal.stop_loss != null ? "text-rose-600" : "text-slate-300"}`}>
                    {signal.stop_loss != null ? INR.format(signal.stop_loss) : "—"}
                  </div>
                </div>

                <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
                  <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400 mb-1.5">
                    Status
                  </div>
                  <ExpiryBadge expiresAt={signal.expires_at} />
                </div>
              </div>
            )}
          </div>

          {/* ── AI Reasoning ──────────────────────────────────────── */}
          {signal.reasoning && (
            <div className="rounded-xl border border-l-[3px] border-violet-200 border-l-violet-400 bg-violet-50 px-4 py-3.5">
              <div className="text-[10px] font-semibold uppercase tracking-wide text-violet-500 mb-2">
                AI Reasoning
              </div>
              <p className="text-sm italic leading-relaxed text-violet-900">
                {signal.reasoning}
              </p>
            </div>
          )}

          {/* ── Market Events ─────────────────────────────────────── */}
          {hasEvents && (
            <div>
              <SectionHeader
                icon={<Activity className="h-3.5 w-3.5" />}
                label="Market Events"
                count={events.length}
              />
              <div className="space-y-2.5">
                {events.map((event) => (
                  <EventCard key={event.event_id} event={event} />
                ))}
              </div>
            </div>
          )}

          {/* ── ML Model Predictions ──────────────────────────────── */}
          {hasML && (
            <div>
              <SectionHeader
                icon={<Brain className="h-3.5 w-3.5" />}
                label="ML Model Predictions"
                count={ml_predictions.length}
              />
              <div className="space-y-2.5">
                {ml_predictions.map((pred) => (
                  <MLPredCard key={pred.model_id} pred={pred} />
                ))}
              </div>
            </div>
          )}

          {/* ── Technical Indicators ──────────────────────────────── */}
          {hasTech && (
            <div>
              <SectionHeader
                icon={<BarChart3 className="h-3.5 w-3.5" />}
                label="Technical Indicators"
                count={technical.length}
              />
              <div className="grid grid-cols-2 sm:grid-cols-3 gap-2.5">
                {technical.map((tech, i) => (
                  <TechIndicatorCard key={`${tech.indicator}-${i}`} tech={tech} />
                ))}
              </div>
            </div>
          )}

          {/* ── Signal History / Audit Trail ──────────────────────── */}
          {hasAudit && (
            <div>
              <SectionHeader
                icon={<Clock className="h-3.5 w-3.5" />}
                label="Signal History"
                count={auditLog.length}
              />
              <div className="rounded-xl border border-slate-200 bg-white px-4 pt-4 pb-2">
                {auditLog.map((entry, i) => (
                  <AuditEntry
                    key={entry.audit_id}
                    entry={entry}
                    isLast={i === auditLog.length - 1}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Empty state */}
          {!hasEvents && !hasML && !hasTech && !hasAudit && (
            <div className="rounded-xl border border-dashed border-slate-200 py-10 text-center text-sm text-slate-400">
              No contributing factor data available for this signal.
            </div>
          )}

        </div>
      </div>
    </div>
  );
}
