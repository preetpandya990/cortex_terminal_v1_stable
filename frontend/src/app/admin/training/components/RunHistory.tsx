"use client";

import { RefreshCw, CheckCircle2, XCircle, Loader2, HelpCircle, ExternalLink } from "lucide-react";
import type { RunSummary, RunStatus } from "@/types/admin_training";
import { PIPELINE_STEPS } from "@/types/admin_training";

// ── Status helpers ────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<RunStatus, {
  label: string;
  icon: typeof CheckCircle2;
  color: string;
  bg: string;
}> = {
  running: {
    label: "Running",
    icon: Loader2,
    color: "text-blue-600",
    bg: "bg-blue-50",
  },
  completed: {
    label: "Completed",
    icon: CheckCircle2,
    color: "text-emerald-600",
    bg: "bg-emerald-50",
  },
  failed: {
    label: "Failed",
    icon: XCircle,
    color: "text-rose-600",
    bg: "bg-rose-50",
  },
  unknown: {
    label: "Unknown",
    icon: HelpCircle,
    color: "text-slate-400",
    bg: "bg-slate-50",
  },
};

function StatusBadge({ status }: { status: RunStatus }) {
  const { label, icon: Icon, color, bg } = STATUS_CONFIG[status];
  return (
    <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-semibold ${color} ${bg}`}>
      <Icon className={`h-3 w-3 ${status === "running" ? "animate-spin" : ""}`} />
      {label}
    </span>
  );
}

function formatDuration(s: number | null | undefined): string {
  if (!s) return "—";
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${Math.floor(s % 60)}s`;
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  });
}

// ── Row component ─────────────────────────────────────────────────────────────

function RunRow({ run }: { run: RunSummary }) {
  const stepsText = run.steps_completed.length > 0
    ? `${run.steps_completed.length}/10`
    : "—";

  return (
    <tr className="border-b border-slate-100 text-[13px] hover:bg-slate-50/50 transition-colors">
      <td className="py-3 pl-4 pr-2">
        <div className="flex items-center gap-2">
          <StatusBadge status={run.status} />
        </div>
      </td>
      <td className="py-3 pr-2">
        <span className="font-mono text-[11px] text-slate-600">{run.run_id}</span>
        {run.source !== "unknown" && (
          <span className="ml-1.5 rounded bg-slate-100 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-slate-400">
            {run.source}
          </span>
        )}
      </td>
      <td className="py-3 pr-2 text-slate-600">{formatDate(run.started_at)}</td>
      <td className="py-3 pr-2 text-slate-500">{formatDuration(run.duration_s)}</td>
      <td className="py-3 pr-2">
        <span className="font-mono text-[11px] text-slate-500">{run.model_version ?? "—"}</span>
      </td>
      <td className="py-3 pr-2">
        <div className="flex items-center gap-1">
          <div className="flex gap-0.5">
            {PIPELINE_STEPS.map(({ key }) => (
              <div
                key={key}
                className={`h-1.5 w-1.5 rounded-full ${
                  run.steps_completed.includes(key)
                    ? "bg-emerald-400"
                    : "bg-slate-200"
                }`}
                title={key}
              />
            ))}
          </div>
          <span className="ml-1 text-[11px] text-slate-400">{stepsText}</span>
        </div>
      </td>
      <td className="py-3 pr-4">
        {run.mlflow_run_id ? (
          <a
            href={`http://localhost:5000/#/experiments/1/runs/${run.mlflow_run_id}`}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 text-[12px] text-blue-600 hover:text-blue-800 hover:underline"
          >
            MLflow <ExternalLink className="h-3 w-3" />
          </a>
        ) : run.error_summary ? (
          <span className="text-[11px] text-rose-500 line-clamp-1" title={run.error_summary}>
            {run.error_summary}
          </span>
        ) : (
          <span className="text-[11px] text-slate-300">—</span>
        )}
      </td>
    </tr>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface RunHistoryProps {
  runs: RunSummary[];
  isLoading: boolean;
  onRefresh: () => void;
}

export function RunHistory({ runs, isLoading, onRefresh }: RunHistoryProps) {
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Run History</h2>
          <p className="mt-0.5 text-[13px] text-slate-500">
            Aggregated from checkpoint, MLflow, and error_state files.
          </p>
        </div>
        <button
          onClick={onRefresh}
          disabled={isLoading}
          className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-600 shadow-sm transition hover:bg-slate-50 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? "animate-spin" : ""}`} />
          Refresh
        </button>
      </div>

      <div className="overflow-hidden rounded-xl border border-slate-100">
        {isLoading && runs.length === 0 ? (
          <div className="flex h-40 items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-slate-300" />
          </div>
        ) : runs.length === 0 ? (
          <div className="flex h-40 items-center justify-center text-sm text-slate-400">
            No training runs found.
          </div>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-100 bg-slate-50 text-left text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <th className="py-2.5 pl-4 pr-2">Status</th>
                <th className="py-2.5 pr-2">Run ID</th>
                <th className="py-2.5 pr-2">Started</th>
                <th className="py-2.5 pr-2">Duration</th>
                <th className="py-2.5 pr-2">Version</th>
                <th className="py-2.5 pr-2">Steps</th>
                <th className="py-2.5 pr-4">Detail</th>
              </tr>
            </thead>
            <tbody>
              {runs.map(run => <RunRow key={run.run_id} run={run} />)}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
