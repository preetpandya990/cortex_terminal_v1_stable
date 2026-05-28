"use client";

import { CheckCircle2, AlertTriangle, XCircle, RefreshCw, Cpu, Package, Database, Lock, Calendar, HardDrive } from "lucide-react";
import type { PreflightReport, ProbeResult, ProbeStatus } from "@/types/admin_training";

// ── Status helpers ────────────────────────────────────────────────────────────

const STATUS_CONFIG: Record<ProbeStatus, {
  icon: typeof CheckCircle2;
  color: string;
  bg: string;
  border: string;
  badge: string;
}> = {
  pass: {
    icon: CheckCircle2,
    color: "text-emerald-600",
    bg: "bg-emerald-50",
    border: "border-emerald-200",
    badge: "bg-emerald-100 text-emerald-700",
  },
  warn: {
    icon: AlertTriangle,
    color: "text-amber-600",
    bg: "bg-amber-50",
    border: "border-amber-200",
    badge: "bg-amber-100 text-amber-700",
  },
  fail: {
    icon: XCircle,
    color: "text-rose-600",
    bg: "bg-rose-50",
    border: "border-rose-200",
    badge: "bg-rose-100 text-rose-700",
  },
};

const PROBE_ICONS: Record<string, typeof Cpu> = {
  gpu:      Cpu,
  env:      Package,
  data:     Database,
  lock:     Lock,
  schedule: Calendar,
  disk:     HardDrive,
};

// ── Sub-components ────────────────────────────────────────────────────────────

function ProbeCard({ probe }: { probe: ProbeResult }) {
  const cfg   = STATUS_CONFIG[probe.status];
  const Icon  = PROBE_ICONS[probe.name] ?? CheckCircle2;
  const SIcon = cfg.icon;

  return (
    <div className={`rounded-xl border p-4 transition-colors ${cfg.border} ${cfg.bg}`}>
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 flex-shrink-0 rounded-lg p-1.5 ${cfg.bg}`}>
          <Icon className={`h-4 w-4 ${cfg.color}`} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-2">
            <span className="text-sm font-semibold text-slate-800">{probe.label}</span>
            <span className={`flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-bold uppercase tracking-wide ${cfg.badge}`}>
              <SIcon className="h-3 w-3" />
              {probe.status}
            </span>
          </div>
          <p className="mt-0.5 text-[13px] text-slate-600">{probe.message}</p>
          {probe.remediation && probe.status !== "pass" && (
            <p className="mt-1.5 rounded-lg bg-white/60 px-2.5 py-1.5 text-[11px] leading-relaxed text-slate-500">
              💡 {probe.remediation}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface PreflightBoardProps {
  report: PreflightReport | null;
  isLoading: boolean;
  onRefresh: () => void;
}

export function PreflightBoard({ report, isLoading, onRefresh }: PreflightBoardProps) {
  const failCount = report?.probes.filter(p => p.status === "fail").length ?? 0;
  const warnCount = report?.probes.filter(p => p.status === "warn").length ?? 0;

  return (
    <div className="space-y-5">
      {/* Header + refresh */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Pre-flight Checks</h2>
          <p className="mt-0.5 text-[13px] text-slate-500">
            All 6 gates must pass (or warn with override) before launch is enabled.
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

      {/* Summary banner */}
      {report && (
        <div className={`flex items-center gap-3 rounded-xl border px-4 py-3 text-sm font-medium ${
          report.can_launch
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-rose-200 bg-rose-50 text-rose-800"
        }`}>
          {report.can_launch ? (
            <CheckCircle2 className="h-5 w-5 text-emerald-600" />
          ) : (
            <XCircle className="h-5 w-5 text-rose-600" />
          )}
          <span>
            {report.can_launch
              ? `Ready to launch${warnCount > 0 ? ` (${warnCount} warning${warnCount > 1 ? "s" : ""})` : ""}`
              : `${failCount} gate${failCount > 1 ? "s" : ""} failing — launch blocked`}
          </span>
          <span className="ml-auto text-[11px] font-normal text-slate-400">
            Checked {new Date(report.checked_at).toLocaleTimeString()}
          </span>
        </div>
      )}

      {/* Probe grid */}
      {isLoading && !report ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="h-[88px] animate-pulse rounded-xl border border-slate-100 bg-slate-50" />
          ))}
        </div>
      ) : report ? (
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {report.probes.map(probe => (
            <ProbeCard key={probe.name} probe={probe} />
          ))}
        </div>
      ) : (
        <div className="flex h-40 items-center justify-center rounded-xl border border-dashed border-slate-200 text-sm text-slate-400">
          Click <span className="mx-1 font-semibold">Refresh</span> to run pre-flight checks.
        </div>
      )}
    </div>
  );
}
