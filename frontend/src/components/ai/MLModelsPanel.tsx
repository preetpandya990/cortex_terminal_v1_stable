"use client";

import * as React from "react";
import { createPortal } from "react-dom";
import { format, formatDistanceToNow } from "date-fns";
import {
  LineChart,
  Line,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
} from "recharts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/components/ui/toast";
import { useQueryClient } from "@tanstack/react-query";
import {
  governanceKeys,
  useGovernanceSummary,
  useModels,
  useDriftReports,
  useUpdateModelState,
  useTriggerDriftCheck,
  useEnsembleStatus,
} from "@/hooks/useModels";
import { useCAIWebSocket } from "@/hooks/useCAIWebSocket";
import {
  ModelState,
  VALID_TRANSITIONS,
  QUALITY_GATES,
  type DriftReport,
  type EnsembleMember,
  type MLModel,
  type ModelMetrics,
} from "@/types/models";
import {
  Activity,
  AlertTriangle,
  Brain,
  CheckCircle2,
  ChevronRight,
  Layers,
  MoonStar,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  XCircle,
  Zap,
} from "lucide-react";

interface MLModelsPanelProps {
  className?: string;
  isAdmin?: boolean;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const STATE_TABS = [
  { value: "all",                    label: "All"     },
  { value: ModelState.LIVE,          label: "Live"    },
  { value: ModelState.PAPER,         label: "Paper"   },
  { value: ModelState.SHADOW,        label: "Shadow"  },
  { value: ModelState.RETIRED,       label: "Retired" },
] as const;

// ── Visual helpers ────────────────────────────────────────────────────────────

function stateBadgeClass(state: ModelState): string {
  switch (state) {
    case ModelState.LIVE:    return "bg-emerald-500/10 text-emerald-700 border-emerald-500/20";
    case ModelState.PAPER:   return "bg-blue-500/10   text-blue-700   border-blue-500/20";
    case ModelState.SHADOW:  return "bg-amber-500/10  text-amber-700  border-amber-500/20";
    case ModelState.RETIRED: return "bg-slate-500/10  text-slate-600  border-slate-400/20";
  }
}

function driftScoreClass(score: number): string {
  if (score >= 0.7) return "text-red-600";
  if (score >= 0.4) return "text-amber-600";
  return "text-emerald-600";
}

function metricPct(value: number | null): string {
  return value != null ? `${(value * 100).toFixed(1)}%` : "—";
}

function stateLabel(state: ModelState): string {
  return state.charAt(0).toUpperCase() + state.slice(1);
}

// ── Summary stat card ─────────────────────────────────────────────────────────

function SummaryCard({
  icon: Icon,
  label,
  value,
  accent,
}: {
  icon: React.ElementType;
  label: string;
  value: number;
  accent: "green" | "blue" | "amber" | "slate" | "red";
}) {
  const colors = {
    green: { bg: "bg-emerald-500/8", icon: "text-emerald-600", value: "text-emerald-700" },
    blue:  { bg: "bg-blue-500/8",    icon: "text-blue-600",    value: "text-blue-700"    },
    amber: { bg: "bg-amber-500/8",   icon: "text-amber-600",   value: "text-amber-700"   },
    slate: { bg: "bg-slate-500/8",   icon: "text-slate-500",   value: "text-slate-600"   },
    red:   { bg: "bg-red-500/8",     icon: "text-red-600",     value: "text-red-700"     },
  }[accent];

  return (
    <div className={`flex items-center gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm`}>
      <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${colors.bg}`}>
        <Icon className={`h-4 w-4 ${colors.icon}`} />
      </div>
      <div>
        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
        <div className={`mt-0.5 text-xl font-bold leading-tight ${colors.value}`}>{value}</div>
      </div>
    </div>
  );
}

// ── Drift sparkline ───────────────────────────────────────────────────────────

function DriftSparkline({ history }: { history: DriftReport[] }) {
  if (history.length === 0) {
    return <span className="text-xs text-slate-400">No data</span>;
  }

  const latest = history[0];
  const points = [...history].reverse().slice(-10).map((r) => ({ v: r.drift_score ?? 0 }));

  const score = latest.drift_score;
  const color =
    score == null     ? "#94a3b8"
    : score >= 0.7    ? "#dc2626"
    : score >= 0.4    ? "#d97706"
    :                   "#059669";

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1.5">
        {latest.drift_detected && (
          <AlertTriangle className="h-3 w-3 shrink-0 text-red-500" />
        )}
        <span className={`text-sm font-bold tabular-nums ${score != null ? driftScoreClass(score) : "text-slate-400"}`}>
          {score != null ? `${(score * 100).toFixed(1)}%` : "—"}
        </span>
      </div>
      {points.length >= 3 && (
        <div className="h-6 w-[80px]">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={points} margin={{ top: 2, bottom: 2, left: 0, right: 0 }}>
              <Line
                type="monotone"
                dataKey="v"
                stroke={color}
                strokeWidth={1.5}
                dot={false}
                isAnimationActive={false}
              />
              <RechartsTooltip
                content={({ active, payload }) =>
                  active && payload?.[0] ? (
                    <div className="rounded border border-slate-200 bg-white px-2 py-1 text-xs shadow-md">
                      {`${((payload[0].value as number) * 100).toFixed(1)}%`}
                    </div>
                  ) : null
                }
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ── Quality gate preview ──────────────────────────────────────────────────────

function QualityGatePreview({
  model,
  targetState,
}: {
  model: MLModel;
  targetState: ModelState;
}) {
  const gates = QUALITY_GATES[targetState];
  if (gates.length === 0) return null;

  return (
    <div className="mt-4 rounded-lg border border-slate-200 bg-slate-50 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">
        <ShieldCheck className="h-3.5 w-3.5" />
        Quality Gates
        <span className="ml-1 font-normal text-slate-400">(informational — admin overrides)</span>
      </div>
      <div className="space-y-1.5">
        {gates.map(({ metric, label, threshold }) => {
          const actual = model.metrics[metric as keyof ModelMetrics];
          const pass   = actual != null && actual >= threshold;
          return (
            <div key={metric} className="flex items-center justify-between text-sm">
              <div className="flex items-center gap-1.5">
                {pass ? (
                  <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                ) : (
                  <XCircle className="h-3.5 w-3.5 text-red-500" />
                )}
                <span className="text-slate-700">{label}</span>
              </div>
              <div className="flex items-center gap-2 text-xs">
                <span className={`font-semibold ${pass ? "text-emerald-600" : "text-red-600"}`}>
                  {metricPct(actual)}
                </span>
                <span className="text-slate-400">/ ≥{(threshold * 100).toFixed(0)}%</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Transition action buttons ─────────────────────────────────────────────────

function TransitionButtons({
  model,
  onTransition,
}: {
  model: MLModel;
  onTransition: (model: MLModel, target: ModelState) => void;
}) {
  const transitions = VALID_TRANSITIONS[model.deployment_state] ?? [];

  if (transitions.length === 0) return <span className="text-xs text-slate-400">—</span>;

  return (
    <div className="flex items-center gap-1">
      {transitions.map((target) => {
        const isDemotion = target === ModelState.SHADOW;
        const isRestore = model.deployment_state === ModelState.RETIRED;

        let Icon = ChevronRight;
        let title = `Move to ${stateLabel(target)}`;
        let variant: "ghost" | "outline" = "ghost";

        if (target === ModelState.LIVE)  { Icon = TrendingUp;   }
        if (target === ModelState.PAPER) { Icon = Activity;     }
        if (isDemotion || isRestore)     { Icon = TrendingDown; }
        if (isRestore)                   { Icon = RotateCcw; title = "Restore to Paper"; }

        return (
          <Button
            key={target}
            variant={variant}
            size="sm"
            className="h-7 w-7 p-0"
            title={title}
            onClick={() => onTransition(model, target)}
          >
            <Icon className="h-3.5 w-3.5" />
          </Button>
        );
      })}
    </div>
  );
}

// ── Metric explanations ───────────────────────────────────────────────────────
// Every entry drives both the ensemble-card chips and the table-row tooltip.

interface MetricInfo {
  fullName:        string;
  what:            string;   // one sentence: what it measures
  interpret:       string;   // how to read the number
  ideal:           string;   // target values for this system
  baseline?:       string;   // random/null baseline
  formula?:        string;   // optional: symbolic description
  thresholds: { warn: number; good: number; direction: "higher" | "lower" };
}

const METRIC_INFO: Record<string, MetricInfo> = {
  "AUC-PR": {
    fullName:  "Area Under the Precision-Recall Curve",
    what:      "Measures overall signal quality across every possible confidence threshold — how well the model separates genuine UP moves from noise.",
    interpret: "Unlike AUC-ROC, this curve is not flattered by the large number of 'no strong move' days. A random classifier scores ≈ 0.50 on a balanced dataset.",
    ideal:     "≥ 0.60 (development)  ·  ≥ 0.65 (paper trading)  ·  ≥ 0.70 (live)",
    baseline:  "≈ 0.50 for a random classifier on a balanced UP/DOWN split",
    formula:   "∫ Precision(τ) d Recall(τ) as decision threshold τ varies 0 → 1",
    thresholds: { warn: 0.60, good: 0.65, direction: "higher" },
  },
  "DSR": {
    fullName:  "Deflated Sharpe Ratio",
    what:      "The out-of-sample Sharpe Ratio from the CPCV backtest, adjusted downward to account for the multiple-testing bias inherent in any hyperparameter search.",
    interpret: "A positive DSR means there is a statistically significant trading edge even after correcting for the fact that we tried many configurations. Negative DSR = the backtested performance is likely noise.",
    ideal:     "> 0.5 (meaningful edge after bias correction)  ·  > 1.0 (strong edge)",
    baseline:  "0.0 — no edge after adjusting for selection bias",
    formula:   "SR_oos × (1 − PBO) / σ(SR_paths)",
    thresholds: { warn: 0.3, good: 0.5, direction: "higher" },
  },
  "ECE": {
    fullName:  "Expected Calibration Error",
    what:      "Measures how accurately the model's confidence scores reflect real win rates. If the model outputs 70% confidence, it should be right approximately 70% of the time.",
    interpret: "0 = perfect calibration. High ECE means the raw probabilities are misleading for risk sizing. This system applies Beta calibration fitted on the CPCV OOF pool to minimise ECE before deployment.",
    ideal:     "< 0.05 (well-calibrated)  ·  < 0.03 (excellent)",
    baseline:  "Uncalibrated models typically score 0.10–0.20",
    formula:   "Σ |mean_confidence_bin − mean_accuracy_bin| × (n_bin / n_total)",
    thresholds: { warn: 0.05, good: 0.03, direction: "lower" },
  },
  "PBO": {
    fullName:  "Probability of Backtest Overfitting",
    what:      "Estimated probability that the strategy chosen during model selection was picked by chance rather than genuine predictive power, computed from the CPCV path distribution.",
    interpret: "0% = zero overfitting risk (unrealistic). 50% = strategy selection was no better than random. Values below 50% indicate the selection process found real signal. This is a structural property of the training methodology, not just model quality.",
    ideal:     "< 50% (better than random selection)  ·  < 25% (low overfitting risk)",
    baseline:  "50% (random strategy selection)",
    formula:   "P(rank of best in-sample path < median rank out-of-sample across all CPCV paths)",
    thresholds: { warn: 0.5, good: 0.25, direction: "lower" },
  },
  "Accuracy": {
    fullName:  "Directional Accuracy",
    what:      "Fraction of 5-day forward UP/DOWN predictions that matched the realised direction. Samples within the ATR dead-zone (price moves too small to be meaningful) are excluded before measurement.",
    interpret: "A coin flip gives 50%. Each point above 50% reflects genuine signal — the direction filter and dead-zone exclusion make this harder to inflate than raw accuracy on every bar.",
    ideal:     "> 55% (development gate)  ·  > 58% (paper/live gate)",
    baseline:  "≈ 50% for a random classifier",
    thresholds: { warn: 0.55, good: 0.58, direction: "higher" },
  },
  "Precision": {
    fullName:  "Precision (Positive Predictive Value)",
    what:      "Of all BUY signals the model generated, the fraction where price actually moved up over the 5-day horizon.",
    interpret: "High precision = low false-alarm rate. A precision-focused model avoids noisy signals but may miss real moves. Gate ≥ 53% for paper trading.",
    ideal:     "≥ 0.53 (paper gate)  ·  ≥ 0.55 (live gate)",
    baseline:  "≈ 0.50 for a random classifier on a balanced dataset",
    thresholds: { warn: 0.53, good: 0.58, direction: "higher" },
  },
  "Recall": {
    fullName:  "Recall (True Positive Rate / Sensitivity)",
    what:      "Of all 5-day UP moves that actually occurred, the fraction the model correctly flagged as a BUY signal.",
    interpret: "High recall = fewer missed opportunities. There is an inherent precision-recall tradeoff — maximising recall usually reduces precision. Gate ≥ 50% ensures the model is not ignoring most real moves.",
    ideal:     "≥ 0.50 (paper gate)  ·  ≥ 0.53 (live gate)",
    baseline:  "≈ 0.50 for a random classifier",
    thresholds: { warn: 0.50, good: 0.55, direction: "higher" },
  },
  "F1 Score": {
    fullName:  "F1 Score (Harmonic Mean of Precision and Recall)",
    what:      "Single-number balance between precision and recall. Punishes models that sacrifice one for the other — a model with 90% precision and 10% recall scores F1 = 0.18.",
    interpret: "The harmonic mean is more sensitive to the lower of the two values than the arithmetic mean. An F1 above 0.55 indicates genuinely balanced, useful predictions.",
    ideal:     "> 0.55 (development)  ·  > 0.58 (live target)",
    baseline:  "≈ 0.50 for a random classifier on a balanced dataset",
    formula:   "2 × (Precision × Recall) / (Precision + Recall)",
    thresholds: { warn: 0.53, good: 0.58, direction: "higher" },
  },
  "Symbol Coverage": {
    fullName:  "Symbol Coverage",
    what:      "Fraction of the full NSE universe (2,557 symbols) that passed the data-quality audit and were included in this training run.",
    interpret: "Low coverage indicates new listings with insufficient history or upstream data pipeline gaps. The training gate requires ≥ 60% to proceed. Coverage ≈ 66% is typical for the current NSE universe.",
    ideal:     "> 0.60 (training gate)  ·  > 0.70 (healthy pipeline)",
    baseline:  "0.60 (minimum acceptable coverage)",
    thresholds: { warn: 0.60, good: 0.70, direction: "higher" },
  },
};

// ── Shared metric-info tooltip (portal-rendered to escape clipping) ────────────

interface MetricInfoPortalProps {
  metricKey: string;
  value:     number | null;
  anchor:    { top: number; left: number; width: number };
}

function MetricInfoPortal({ metricKey, value, anchor }: MetricInfoPortalProps) {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => { setMounted(true); }, []);
  if (!mounted) return null;

  const info = METRIC_INFO[metricKey];
  if (!info) return null;

  const TOOLTIP_WIDTH = 300;
  const left = Math.min(
    Math.max(8, anchor.left + anchor.width / 2 - TOOLTIP_WIDTH / 2),
    (typeof window !== "undefined" ? window.innerWidth : 1200) - TOOLTIP_WIDTH - 8,
  );
  const top = anchor.top + 6;

  // Value quality assessment
  let quality: "good" | "warn" | "bad" | "neutral" = "neutral";
  if (value != null && info.thresholds) {
    const { warn, good, direction } = info.thresholds;
    if (direction === "higher") {
      quality = value >= good ? "good" : value >= warn ? "warn" : "bad";
    } else {
      quality = value <= good ? "good" : value <= warn ? "warn" : "bad";
    }
  }

  const qualityConfig = {
    good:    { label: "Good",    color: "text-emerald-700 bg-emerald-50 border-emerald-200" },
    warn:    { label: "Fair",    color: "text-amber-700 bg-amber-50 border-amber-200"       },
    bad:     { label: "Low",     color: "text-rose-700 bg-rose-50 border-rose-200"          },
    neutral: { label: "—",       color: "text-slate-500 bg-slate-50 border-slate-200"       },
  }[quality];

  return createPortal(
    <div
      role="tooltip"
      style={{ position: "fixed", top, left, width: TOOLTIP_WIDTH, zIndex: 9999 }}
      className="rounded-xl border border-slate-200 bg-white shadow-2xl"
    >
      {/* Header */}
      <div className="border-b border-slate-100 px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-widest text-slate-400">
              {metricKey}
            </p>
            <p className="mt-0.5 text-[13px] font-semibold text-slate-800 leading-snug">
              {info.fullName}
            </p>
          </div>
          {value != null && (
            <span className={`mt-0.5 flex-shrink-0 rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${qualityConfig.color}`}>
              {qualityConfig.label}
            </span>
          )}
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        {/* What it measures */}
        <p className="text-[12px] leading-relaxed text-slate-600">{info.what}</p>

        {/* How to interpret */}
        <p className="text-[12px] leading-relaxed text-slate-500">{info.interpret}</p>

        {/* Formula */}
        {info.formula && (
          <div className="rounded-lg bg-slate-50 px-3 py-1.5">
            <p className="font-mono text-[11px] text-slate-500">{info.formula}</p>
          </div>
        )}

        {/* Baseline + Ideal target */}
        <div className="space-y-1.5">
          {info.baseline && (
            <div className="flex items-start gap-1.5 text-[11px]">
              <span className="mt-px h-1.5 w-1.5 flex-shrink-0 rounded-full bg-slate-300 mt-1" />
              <span>
                <span className="font-semibold text-slate-500">Baseline: </span>
                <span className="text-slate-400">{info.baseline}</span>
              </span>
            </div>
          )}
          <div className="flex items-start gap-1.5 text-[11px]">
            <span className="mt-px h-1.5 w-1.5 flex-shrink-0 rounded-full bg-emerald-400 mt-1" />
            <span>
              <span className="font-semibold text-emerald-700">Target: </span>
              <span className="text-slate-500">{info.ideal}</span>
            </span>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

// ── Hoverable metric chip (used in EnsembleStatusCard) ────────────────────────

interface MetricChipProps {
  metricKey: string;
  value:     number | null;
  display:   string;
}

function MetricChip({ metricKey, value, display }: MetricChipProps) {
  const ref = React.useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = React.useState(false);
  const [anchor,  setAnchor]  = React.useState({ top: 0, left: 0, width: 0 });

  const handleMouseEnter = () => {
    if (ref.current) {
      const r = ref.current.getBoundingClientRect();
      setAnchor({ top: r.bottom, left: r.left, width: r.width });
    }
    setHovered(true);
  };

  return (
    <div
      ref={ref}
      className="rounded-lg bg-white/70 px-2.5 py-1.5 text-center shadow-sm cursor-default transition-colors hover:bg-white"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setHovered(false)}
    >
      <div className="flex items-center justify-center gap-1">
        <div className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
          {metricKey}
        </div>
        {METRIC_INFO[metricKey] && (
          <svg
            className="h-2.5 w-2.5 flex-shrink-0 text-slate-300"
            fill="currentColor"
            viewBox="0 0 20 20"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-8-3a1 1 0 00-.867.5 1 1 0 11-1.731-1A3 3 0 0113 8a3.001 3.001 0 01-2 2.83V11a1 1 0 11-2 0v-1a1 1 0 011-1 1 1 0 100-2zm0 8a1 1 0 100-2 1 1 0 000 2z"
              clipRule="evenodd"
            />
          </svg>
        )}
      </div>
      <div className="mt-0.5 text-sm font-bold tabular-nums text-slate-800">{display}</div>
      {hovered && (
        <MetricInfoPortal metricKey={metricKey} value={value} anchor={anchor} />
      )}
    </div>
  );
}

// ── Metrics tooltip (portal-rendered to escape overflow-x-auto clipping) ──────

const METRIC_ROWS: Array<{
  key:       keyof ModelMetrics;
  metricKey: string;
  fullName:  string;
  gate:      number | null;
}> = [
  { key: "accuracy",  metricKey: "Accuracy",  fullName: "Accuracy",  gate: null },
  { key: "precision", metricKey: "Precision", fullName: "Precision", gate: 0.53 },
  { key: "recall",    metricKey: "Recall",    fullName: "Recall",    gate: 0.50 },
  { key: "f1_score",  metricKey: "F1 Score",  fullName: "F1 Score",  gate: null },
];

interface MetricsTooltipPortalProps {
  metrics: ModelMetrics;
  anchor:  { top: number; left: number; width: number };
}

function MetricsTooltipPortal({ metrics, anchor }: MetricsTooltipPortalProps) {
  const [mounted, setMounted] = React.useState(false);
  React.useEffect(() => { setMounted(true); }, []);
  if (!mounted) return null;

  const TOOLTIP_WIDTH = 300;

  const left = Math.min(
    Math.max(8, anchor.left + anchor.width / 2 - TOOLTIP_WIDTH / 2),
    (typeof window !== "undefined" ? window.innerWidth : 1200) - TOOLTIP_WIDTH - 8,
  );
  const top = anchor.top + 4;

  return createPortal(
    <div
      role="tooltip"
      style={{ position: "fixed", top, left, width: TOOLTIP_WIDTH, zIndex: 9999 }}
      className="rounded-xl border border-slate-200 bg-white shadow-2xl"
    >
      {/* Header */}
      <div className="border-b border-slate-100 px-4 py-2.5">
        <p className="text-[11px] font-semibold uppercase tracking-widest text-slate-400">
          Performance Breakdown
        </p>
      </div>

      {/* Metric rows */}
      <div className="flex flex-col gap-3.5 px-4 py-3">
        {METRIC_ROWS.map(({ key, metricKey, fullName, gate }) => {
          const value = metrics[key];
          const pct   = value != null ? value * 100 : null;
          const pass  = gate != null && value != null ? value >= gate : null;
          const info  = METRIC_INFO[metricKey];

          return (
            <div key={key}>
              <div className="mb-1 flex items-center justify-between">
                <div>
                  <span className="text-xs font-semibold text-slate-700">{fullName}</span>
                  {info && (
                    <p className="mt-0.5 text-[10px] leading-snug text-slate-400 pr-2">
                      {info.what.split(".")[0].trim()}.
                    </p>
                  )}
                </div>
                <div className="flex flex-shrink-0 flex-col items-end gap-0.5">
                  <span className="text-sm font-bold tabular-nums text-slate-900">
                    {pct != null ? `${pct.toFixed(1)}%` : "—"}
                  </span>
                  {pass === true  && <span className="text-[10px] font-semibold text-emerald-600">✓ gate</span>}
                  {pass === false && <span className="text-[10px] font-semibold text-red-500">✗ gate</span>}
                </div>
              </div>

              {/* Progress bar */}
              <div className="relative h-1.5 w-full overflow-hidden rounded-full bg-slate-100">
                {pct != null && (
                  <div
                    className={[
                      "h-full rounded-full transition-all",
                      pct >= 70 ? "bg-emerald-500"
                      : pct >= 55 ? "bg-blue-500"
                      : pct >= 40 ? "bg-amber-500"
                      : "bg-red-400",
                    ].join(" ")}
                    style={{ width: `${Math.min(pct, 100)}%` }}
                  />
                )}
                {gate != null && (
                  <div
                    className="absolute top-0 h-full w-px bg-slate-400"
                    style={{ left: `${gate * 100}%` }}
                  />
                )}
              </div>

              {gate != null && (
                <p className="mt-0.5 text-right text-[10px] text-slate-400">
                  Gate ≥ {(gate * 100).toFixed(0)}%
                </p>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer: baseline reminder */}
      <div className="border-t border-slate-100 px-4 py-2">
        <p className="text-[10px] text-slate-400">
          Random classifier baseline ≈ 50% on a balanced UP/DOWN dataset.
        </p>
      </div>
    </div>,
    document.body,
  );
}

// ── Metrics cell ──────────────────────────────────────────────────────────────

function MetricsCell({ metrics }: { metrics: ModelMetrics }) {
  const ref    = React.useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = React.useState(false);
  const [anchor,  setAnchor]  = React.useState({ top: 0, left: 0, width: 0 });

  const handleMouseEnter = () => {
    if (ref.current) {
      const r = ref.current.getBoundingClientRect();
      setAnchor({ top: r.bottom + 6, left: r.left, width: r.width });
    }
    setHovered(true);
  };

  return (
    <div
      ref={ref}
      className="cursor-default"
      onMouseEnter={handleMouseEnter}
      onMouseLeave={() => setHovered(false)}
    >
      {/* Compact summary always visible in the cell */}
      <div className="flex flex-col gap-1">
        <div className="flex items-baseline gap-1">
          <span className="text-sm font-bold tabular-nums text-slate-800">
            {metricPct(metrics.accuracy)}
          </span>
          <span className="text-[10px] font-medium uppercase tracking-wide text-slate-400">acc</span>
        </div>
        <div className="flex items-center gap-2.5 text-[11px]">
          {(
            [
              { label: "Pre", value: metrics.precision },
              { label: "Rec", value: metrics.recall    },
              { label: "F1",  value: metrics.f1_score  },
            ] as const
          ).map(({ label, value }) => (
            <span key={label} className="flex items-baseline gap-0.5">
              <span className="text-slate-400">{label}</span>
              <span className="font-semibold tabular-nums text-slate-600">{metricPct(value)}</span>
            </span>
          ))}
        </div>
      </div>

      {/* Portal tooltip — fixed-positioned to escape overflow-x-auto clipping */}
      {hovered && <MetricsTooltipPortal metrics={metrics} anchor={anchor} />}
    </div>
  );
}

// ── State change dialog ───────────────────────────────────────────────────────

interface StateDialogProps {
  model:       MLModel | null;
  targetState: ModelState | null;
  isPending:   boolean;
  onConfirm:   (reason: string) => void;
  onClose:     () => void;
}

function StateChangeDialog({ model, targetState, isPending, onConfirm, onClose }: StateDialogProps) {
  const [reason, setReason] = React.useState("");

  React.useEffect(() => {
    if (!model) setReason("");
  }, [model]);

  if (!model || !targetState) return null;

  const isDemotion = targetState === ModelState.SHADOW || targetState === ModelState.RETIRED;
  const actionLabel = isDemotion ? "Demote" : "Promote";

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Confirm State Change</DialogTitle>
          <div className="text-sm text-slate-500">
            Move{" "}
            <strong className="text-slate-900">{model.model_name}</strong>{" "}
            from{" "}
            <Badge variant="outline" className={`mx-0.5 text-xs ${stateBadgeClass(model.deployment_state)}`}>
              {stateLabel(model.deployment_state)}
            </Badge>
            {" "}to{" "}
            <Badge variant="outline" className={`mx-0.5 text-xs ${stateBadgeClass(targetState)}`}>
              {stateLabel(targetState)}
            </Badge>
          </div>
        </DialogHeader>

        <QualityGatePreview model={model} targetState={targetState} />

        <div className="mt-3">
          <label className="mb-1.5 block text-xs font-semibold text-slate-600">
            Reason <span className="font-normal text-slate-400">(optional — stored in audit log)</span>
          </label>
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder={`Why are you ${isDemotion ? "demoting" : "promoting"} this model?`}
            rows={2}
            maxLength={500}
            className="w-full resize-none rounded-md border border-slate-200 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:outline-none focus:ring-2 focus:ring-blue-500/30 focus:border-blue-400"
          />
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={onClose} disabled={isPending}>
            Cancel
          </Button>
          <Button
            onClick={() => onConfirm(reason.trim())}
            disabled={isPending}
            className={isDemotion ? "bg-amber-600 hover:bg-amber-700 text-white" : ""}
          >
            {isPending ? (
              <>
                <RefreshCw className="mr-2 h-3.5 w-3.5 animate-spin" />
                Updating…
              </>
            ) : (
              `${actionLabel} Model`
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ── Empty state ───────────────────────────────────────────────────────────────

function EmptyState({ tab }: { tab: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      <Brain className="mb-3 h-8 w-8 text-slate-300" />
      <p className="text-sm font-medium text-slate-500">No models</p>
      <p className="mt-1 text-xs text-slate-400">
        {tab === "all"
          ? "No models have been registered yet."
          : `No models in ${tab} state.`}
      </p>
    </div>
  );
}

// ── Ensemble status card ──────────────────────────────────────────────────────

function MemberRow({ m }: { m: EnsembleMember }) {
  const isActive  = m.is_active;
  const auc       = m.metrics.auc_pr;
  const dsr       = m.metrics.deflated_sharpe;
  const ece       = m.metrics.ece_after;
  const acc       = m.metrics.accuracy;

  // For dormant members show the AUC-PR gate gap bar
  const gate      = m.activation_gate;
  const aucCheck  = gate?.checks.find((c) => c.metric === "auc_pr");

  return (
    <div
      className={[
        "flex flex-col gap-3 rounded-xl border p-4 transition-colors",
        isActive
          ? "border-emerald-200 bg-emerald-50/60"
          : "border-amber-200 bg-amber-50/40",
      ].join(" ")}
    >
      {/* Header row */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2.5">
          <div
            className={[
              "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
              isActive ? "bg-emerald-500/12" : "bg-amber-500/12",
            ].join(" ")}
          >
            {isActive
              ? <Zap className="h-4 w-4 text-emerald-600" />
              : <MoonStar className="h-4 w-4 text-amber-600" />
            }
          </div>
          <div>
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold capitalize text-slate-900">
                {m.model_name}
              </span>
              <Badge
                variant="outline"
                className={[
                  "text-[10px] font-semibold",
                  isActive
                    ? "border-emerald-300 bg-emerald-50 text-emerald-700"
                    : "border-amber-300 bg-amber-50 text-amber-700",
                ].join(" ")}
              >
                {isActive ? "LIVE" : "DORMANT"}
              </Badge>
            </div>
            <span className="text-[11px] text-slate-400">{m.model_version}</span>
          </div>
        </div>

        {/* Weight pill */}
        <div className="text-right">
          <div className={[
            "text-lg font-bold tabular-nums",
            isActive ? "text-emerald-700" : "text-slate-400",
          ].join(" ")}>
            {(m.effective_weight * 100).toFixed(0)}%
          </div>
          <div className="text-[10px] uppercase tracking-wide text-slate-400">weight</div>
          {/* A5 training-time recommendation — shown when it differs from effective_weight */}
          {m.training_weight != null && Math.abs(m.training_weight - m.effective_weight) > 0.001 && (
            <div
              className="mt-0.5 text-[10px] text-slate-400"
              title="A5 optimizer recommendation at training time"
            >
              A5: {(m.training_weight * 100).toFixed(0)}%
            </div>
          )}
        </div>
      </div>

      {/* Metrics row — each chip is hoverable with an explanation tooltip */}
      <div className="grid grid-cols-4 gap-2">
        <MetricChip
          metricKey="AUC-PR"
          value={auc}
          display={auc != null ? auc.toFixed(4) : "—"}
        />
        <MetricChip
          metricKey="DSR"
          value={dsr}
          display={dsr != null ? dsr.toFixed(4) : "—"}
        />
        <MetricChip
          metricKey="ECE"
          value={ece}
          display={ece != null ? ece.toFixed(4) : "—"}
        />
        <MetricChip
          metricKey="Accuracy"
          value={acc}
          display={acc != null ? `${(acc * 100).toFixed(1)}%` : "—"}
        />
      </div>

      {/* Dormant: activation gate progress */}
      {!isActive && aucCheck && (
        <div className="space-y-1">
          <div className="flex items-center justify-between text-[11px]">
            <span className="font-medium text-slate-500">AUC-PR gate to activate</span>
            <span className={aucCheck.pass ? "font-bold text-emerald-600" : "font-bold text-amber-700"}>
              {aucCheck.current.toFixed(4)}{" "}
              <span className="font-normal text-slate-400">
                / {aucCheck.required.toFixed(2)} ({aucCheck.gap >= 0 ? "+" : ""}{aucCheck.gap.toFixed(4)})
              </span>
            </span>
          </div>
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-amber-100">
            <div
              className={[
                "h-full rounded-full transition-all",
                aucCheck.pass ? "bg-emerald-500" : "bg-amber-500",
              ].join(" ")}
              style={{ width: `${Math.min((aucCheck.current / aucCheck.required) * 100, 100).toFixed(1)}%` }}
            />
          </div>
          <p className="text-[10px] text-slate-400">
            Retrain with more data to close the gap.
            When gate passes, set <code className="rounded bg-slate-100 px-1 font-mono text-slate-600">is_active=true</code> to enable full ensemble.
          </p>
        </div>
      )}

      {/* Ensemble non-accretive notice — explains why training_weight may show 0% */}
      {!isActive && m.is_ensemble_accretive === false && (
        <div className="flex items-start gap-1.5 rounded-lg border border-amber-200 bg-amber-50/70 px-2.5 py-2 text-[11px] text-amber-700">
          <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0" />
          <span>
            <strong>Not accretive at training time</strong> — the A5 optimizer found this model did not
            improve the ensemble blend (standalone {m.model_name.toUpperCase()} outperformed every combination).
            The blending recommendation was weight&nbsp;0%. Effective serving weight is determined by active
            membership, not this recommendation.
          </span>
        </div>
      )}
    </div>
  );
}

export function EnsembleStatusCard({ className }: { className?: string }) {
  const { data, isLoading } = useEnsembleStatus();

  const modeLabel =
    data?.mode === "full_ensemble"  ? "Full Ensemble (XGBoost + GRU)" :
    data?.mode === "xgboost_only"   ? "XGBoost-Only  ·  GRU Dormant"  :
                                      "Degraded";

  const modeBadgeClass =
    data?.mode === "full_ensemble" ? "bg-emerald-500/10 text-emerald-700 border-emerald-400/30" :
    data?.mode === "xgboost_only"  ? "bg-amber-500/10  text-amber-700  border-amber-400/30"    :
                                     "bg-red-500/10    text-red-700    border-red-400/30";

  return (
    <Card className={className}>
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Layers className="h-4 w-4 text-slate-500" />
            <CardTitle className="text-base font-semibold">Ensemble Composition</CardTitle>
          </div>
          {!isLoading && data && (
            <Badge variant="outline" className={`text-xs font-semibold ${modeBadgeClass}`}>
              {modeLabel}
            </Badge>
          )}
        </div>
        <p className="mt-0.5 text-xs text-slate-400">
          Single-Active-Member Ensemble — production-tier members, active and dormant.
        </p>
      </CardHeader>

      <CardContent className="pt-0">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <RefreshCw className="h-5 w-5 animate-spin text-slate-300" />
          </div>
        ) : !data || data.members.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-8 text-center">
            <Brain className="mb-2 h-6 w-6 text-slate-300" />
            <p className="text-sm text-slate-400">No production-tier models registered.</p>
          </div>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {data.members.map((m) => (
              <MemberRow key={m.model_name} m={m} />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export function MLModelsPanel({ className, isAdmin = false }: MLModelsPanelProps) {
  const { success, error: toastError } = useToast();

  const [activeTab,    setActiveTab]    = React.useState<string>("all");
  const [selectedModel, setSelectedModel] = React.useState<MLModel | null>(null);
  const [targetState,  setTargetState]  = React.useState<ModelState | null>(null);
  const [driftTarget,  setDriftTarget]  = React.useState<number | null>(null);

  const filters = { state: activeTab, page: 1, limit: 100 };

  const { data: summary,    isLoading: isSummaryLoading } = useGovernanceSummary();
  const { data,             isLoading, isRefetching, refetch } = useModels(filters);
  const { data: driftData }                                = useDriftReports();
  const updateState   = useUpdateModelState();
  const triggerDrift  = useTriggerDriftCheck();

  // Invalidate governance cache the moment a drift alert arrives over WebSocket —
  // removes the 60-second polling lag when the background drift loop fires.
  const queryClient = useQueryClient();
  useCAIWebSocket({
    onModelAlert: React.useCallback(() => {
      queryClient.invalidateQueries({ queryKey: governanceKeys.all });
    }, [queryClient]),
  });

  // Group drift reports by model for O(1) lookup
  const driftByModel = React.useMemo<Map<number, DriftReport[]>>(() => {
    const map = new Map<number, DriftReport[]>();
    for (const r of driftData?.reports ?? []) {
      const arr = map.get(r.model_id) ?? [];
      arr.push(r);
      map.set(r.model_id, arr);
    }
    return map;
  }, [driftData]);

  // Active drift alerts (for the alert banner)
  const activeDriftAlerts = React.useMemo(
    () => (driftData?.reports ?? []).filter((r) => r.drift_detected),
    [driftData],
  );

  // Tab counts
  const tabCount = React.useCallback(
    (tab: string): number | null => {
      if (!summary) return null;
      if (tab === "all") return summary.total_models;
      return summary.states[tab as keyof typeof summary.states] ?? 0;
    },
    [summary],
  );

  const handleTransitionClick = (model: MLModel, target: ModelState) => {
    setSelectedModel(model);
    setTargetState(target);
  };

  const handleStateConfirm = async (reason: string) => {
    if (!selectedModel || !targetState) return;
    try {
      await updateState.mutateAsync({
        modelId: selectedModel.model_id,
        data: {
          new_state: targetState,
          reason: reason || `State transition to ${targetState}`,
        },
      });
      success(
        "Model state updated",
        `${selectedModel.model_name} → ${stateLabel(targetState)}`,
      );
    } catch (err: any) {
      const detail = err?.response?.data?.detail ?? "Unknown error";
      toastError("State change failed", detail);
    } finally {
      setSelectedModel(null);
      setTargetState(null);
    }
  };

  const handleDriftCheck = async (modelId: number, modelName: string) => {
    setDriftTarget(modelId);
    try {
      const report = await triggerDrift.mutateAsync({ modelId });
      if (report.drift_detected) {
        toastError(
          `Drift detected — ${modelName}`,
          `Score: ${((report.drift_score ?? 0) * 100).toFixed(1)}% · Action: ${report.action_taken ?? "none"}`,
        );
      } else {
        success(`No drift — ${modelName}`, "Model appears stable.");
      }
    } catch (err: any) {
      const detail = err?.response?.data?.detail ?? "Drift check failed";
      toastError("Drift check failed", detail);
    } finally {
      setDriftTarget(null);
    }
  };

  const models = data?.models ?? [];
  const isDriftChecking = (modelId: number) =>
    driftTarget === modelId && triggerDrift.isPending;

  return (
    <>
      <Card className={className}>
        <CardHeader className="pb-4">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base font-semibold">ML Models</CardTitle>
            <Button
              variant="outline"
              size="sm"
              onClick={() => refetch()}
              disabled={isRefetching}
              className="h-8 gap-1.5 text-xs"
            >
              <RefreshCw className={`h-3.5 w-3.5 ${isRefetching ? "animate-spin" : ""}`} />
              Refresh
            </Button>
          </div>

          {/* Summary stat cards */}
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
            <SummaryCard icon={Zap}          label="Live"          value={summary?.states.live    ?? 0} accent="green" />
            <SummaryCard icon={Activity}     label="Paper"         value={summary?.states.paper   ?? 0} accent="blue"  />
            <SummaryCard icon={Brain}        label="Shadow"        value={summary?.states.shadow  ?? 0} accent="amber" />
            <SummaryCard icon={TrendingDown} label="Retired"       value={summary?.states.retired ?? 0} accent="slate" />
            <SummaryCard icon={AlertTriangle} label="Drift (24h)"  value={summary?.drift_alerts_24h ?? 0} accent="red" />
          </div>

          {/* State filter tabs */}
          <div className="mt-4">
            <Tabs value={activeTab} onValueChange={setActiveTab}>
              <TabsList className="h-8 gap-0.5 bg-slate-100 p-0.5">
                {STATE_TABS.map(({ value, label }) => {
                  const count = tabCount(value);
                  return (
                    <TabsTrigger
                      key={value}
                      value={value}
                      className="h-7 px-3 text-xs data-[state=active]:bg-white data-[state=active]:shadow-sm"
                    >
                      {label}
                      {count != null && count > 0 && (
                        <span className="ml-1.5 rounded-full bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold leading-none text-slate-600">
                          {count}
                        </span>
                      )}
                    </TabsTrigger>
                  );
                })}
              </TabsList>
            </Tabs>
          </div>
        </CardHeader>

        <CardContent className="pt-0">
          {/* Drift alert banner */}
          {activeDriftAlerts.length > 0 && (
            <div className="mb-4 flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
              <div className="flex-1 text-sm">
                <span className="font-semibold text-red-700">
                  {activeDriftAlerts.length} drift alert{activeDriftAlerts.length > 1 ? "s" : ""} in the last 7 days
                </span>
                <div className="mt-1 flex flex-wrap gap-1.5">
                  {activeDriftAlerts.slice(0, 5).map((r) => (
                    <span key={r.id} className="rounded-full bg-red-100 px-2 py-0.5 text-xs font-medium text-red-700">
                      {r.model_name} · {((r.drift_score ?? 0) * 100).toFixed(0)}%
                    </span>
                  ))}
                  {activeDriftAlerts.length > 5 && (
                    <span className="text-xs text-red-500">+{activeDriftAlerts.length - 5} more</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* Table */}
          {isLoading ? (
            <div className="flex items-center justify-center py-16">
              <RefreshCw className="h-5 w-5 animate-spin text-slate-400" />
            </div>
          ) : models.length === 0 ? (
            <EmptyState tab={activeTab} />
          ) : (
            <div className="overflow-x-auto rounded-xl border border-slate-200 shadow-sm">
              <table className="w-full min-w-[860px] border-collapse text-sm">
                <colgroup>
                  <col style={{ width: "200px" }} />
                  <col style={{ width: "110px" }} />
                  <col style={{ width: "80px" }} />
                  <col style={{ width: "180px" }} />
                  <col style={{ width: "130px" }} />
                  <col style={{ width: "120px" }} />
                  {isAdmin && <col style={{ width: "44px" }} />}
                  {isAdmin && <col style={{ width: "90px" }} />}
                </colgroup>

                <thead>
                  <tr className="border-b border-slate-200 bg-slate-50">
                    {[
                      "Model",
                      "Type",
                      "State",
                      "Performance",
                      "Drift Trend",
                      "Trained",
                      ...(isAdmin ? ["", "Transition"] : []),
                    ].map((h) => (
                      <th
                        key={h}
                        className="px-4 py-3 text-left text-[11px] font-semibold uppercase tracking-wider text-slate-400"
                      >
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>

                <tbody className="divide-y divide-slate-100">
                  {models.map((model, i) => {
                    const driftHistory = driftByModel.get(model.model_id) ?? [];
                    const isRetired    = model.deployment_state === ModelState.RETIRED;
                    const isEven       = i % 2 === 0;

                    return (
                      <tr
                        key={model.model_id}
                        className={[
                          "group transition-colors",
                          isEven ? "bg-white" : "bg-slate-50/40",
                          isRetired ? "opacity-55" : "hover:bg-blue-50/30",
                        ].join(" ")}
                      >
                        {/* Model name + version */}
                        <td className="px-4 py-3">
                          <div className="flex flex-col gap-0.5">
                            <span className="font-semibold text-slate-900 leading-snug">
                              {model.model_name}
                            </span>
                            <Badge
                              variant="outline"
                              className="w-fit text-[10px] font-normal text-slate-400 border-slate-200"
                            >
                              {model.version}
                            </Badge>
                          </div>
                        </td>

                        {/* Type + timeframe */}
                        <td className="px-4 py-3">
                          <div className="flex flex-col gap-0.5">
                            <span className="text-xs text-slate-700">{model.model_type}</span>
                            {model.timeframe && (
                              <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">
                                {model.timeframe}
                              </span>
                            )}
                          </div>
                        </td>

                        {/* State badge */}
                        <td className="px-4 py-3">
                          <Badge
                            variant="outline"
                            className={`text-xs font-semibold ${stateBadgeClass(model.deployment_state)}`}
                          >
                            {stateLabel(model.deployment_state)}
                          </Badge>
                        </td>

                        {/* Metrics — stacked, no grid squeeze */}
                        <td className="px-4 py-3">
                          <MetricsCell metrics={model.metrics} />
                        </td>

                        {/* Drift — score + sparkline stacked */}
                        <td className="px-4 py-3">
                          <DriftSparkline history={driftHistory} />
                        </td>

                        {/* Training date */}
                        <td className="px-4 py-3">
                          {model.training_date ? (
                            <div className="flex flex-col gap-0.5">
                              <span className="text-xs text-slate-700">
                                {format(new Date(model.training_date), "MMM d, yyyy")}
                              </span>
                              <span className="text-[10px] text-slate-400">
                                {formatDistanceToNow(new Date(model.training_date), { addSuffix: true })}
                              </span>
                            </div>
                          ) : (
                            <span className="text-xs text-slate-400">—</span>
                          )}
                        </td>

                        {/* Admin: drift check trigger */}
                        {isAdmin && (
                          <td className="px-2 py-3">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 w-7 p-0 text-slate-300 hover:text-slate-600 hover:bg-slate-100"
                              title="Run drift check"
                              disabled={isDriftChecking(model.model_id)}
                              onClick={() => handleDriftCheck(model.model_id, model.model_name)}
                            >
                              <RefreshCw
                                className={`h-3.5 w-3.5 ${isDriftChecking(model.model_id) ? "animate-spin text-blue-500" : ""}`}
                              />
                            </Button>
                          </td>
                        )}

                        {/* Admin: state transition buttons */}
                        {isAdmin && (
                          <td className="px-4 py-3">
                            <TransitionButtons
                              model={model}
                              onTransition={handleTransitionClick}
                            />
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination hint */}
          {data && data.total > models.length && (
            <p className="mt-3 text-center text-xs text-slate-400">
              Showing {models.length} of {data.total} models
            </p>
          )}
        </CardContent>
      </Card>

      {/* State change dialog */}
      {selectedModel && targetState && (
        <StateChangeDialog
          model={selectedModel}
          targetState={targetState}
          isPending={updateState.isPending}
          onConfirm={handleStateConfirm}
          onClose={() => {
            setSelectedModel(null);
            setTargetState(null);
          }}
        />
      )}
    </>
  );
}
