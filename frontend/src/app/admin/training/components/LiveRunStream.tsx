"use client";

import { useEffect, useRef, useState } from "react";
import {
  Wifi, WifiOff, CheckCircle2, Circle, Loader2,
  XCircle, StopCircle, AlertCircle, ChevronDown, ChevronUp,
} from "lucide-react";
import type { RunLogEntry } from "@/types/admin_training";
import { PIPELINE_STEPS } from "@/types/admin_training";
import type { StreamConnectionState } from "@/hooks/useTrainingRunStream";

// ── Step status helpers ───────────────────────────────────────────────────────

type StepStatus = "done" | "running" | "pending" | "failed";

function getStepStatus(
  stepKey: string,
  completedSteps: Set<string>,
  currentStep: string | null,
  runFailed: boolean,
): StepStatus {
  if (completedSteps.has(stepKey)) return "done";
  if (currentStep === stepKey && !runFailed) return "running";
  return "pending";
}

// ── Step progress tracker ─────────────────────────────────────────────────────

function StepTracker({
  completedSteps,
  currentStep,
  runFailed,
}: {
  completedSteps: Set<string>;
  currentStep: string | null;
  runFailed: boolean;
}) {
  return (
    <div className="space-y-0.5">
      {PIPELINE_STEPS.map(({ key, label }, i) => {
        const stepStatus = getStepStatus(key, completedSteps, currentStep, runFailed);
        return (
          <div key={key} className="flex items-center gap-3">
            <div className="flex w-5 flex-shrink-0 items-center justify-center">
              {stepStatus === "done" ? (
                <CheckCircle2 className="h-4 w-4 text-emerald-500" />
              ) : stepStatus === "running" ? (
                <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
              ) : (
                <Circle className="h-4 w-4 text-slate-200" />
              )}
            </div>
            <span className={`text-[13px] ${
              stepStatus === "done"    ? "font-medium text-emerald-700" :
              stepStatus === "running" ? "font-semibold text-blue-700"  :
              "text-slate-400"
            }`}>
              <span className="mr-1.5 font-mono text-[10px] text-slate-300">{i + 1}</span>
              {label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ── Event log entry ───────────────────────────────────────────────────────────

function EventRow({ entry }: { entry: RunLogEntry }) {
  const time = new Date(entry.ts).toLocaleTimeString();
  let badge = entry.event;
  let color = "text-slate-500";

  if (entry.event === "run_start")         { badge = "START";    color = "text-blue-600";    }
  else if (entry.event === "step_complete") { badge = `STEP ${entry.step_num ?? "?"}`; color = "text-emerald-600"; }
  else if (entry.event === "run_finished")  { badge = "DONE";     color = "text-emerald-600"; }
  else if (entry.event === "run_failed")    { badge = "FAILED";   color = "text-rose-600";    }
  else if (entry.event === "c2_report_generated") { badge = "C2 REPORT"; color = "text-violet-600"; }
  else if (entry.event === "f1_event_backtest_complete") { badge = "F1 BACKTEST"; color = "text-indigo-600"; }

  const detail = (() => {
    if (entry.event === "step_complete") {
      const dur = entry.duration_s ? `${entry.duration_s.toFixed(1)}s` : "";
      const step = PIPELINE_STEPS.find(s => s.key === entry.step)?.label ?? entry.step ?? "";
      return `${step}${dur ? ` — ${dur}` : ""}`;
    }
    if (entry.event === "run_start") {
      return `run_id=${entry.run_id} · v${entry.model_version ?? "?"}${entry.resumed ? " (resumed)" : ""}`;
    }
    if (entry.event === "c2_report_generated") {
      return `status=${entry.status}`;
    }
    if (entry.event === "f1_event_backtest_complete") {
      const fills = entry.total_fills != null ? `${entry.total_fills} fills` : "";
      const sharpe = entry.mean_ann_sharpe != null ? `SR=${entry.mean_ann_sharpe.toFixed(4)}` : "";
      return [fills, sharpe].filter(Boolean).join(" · ");
    }
    return "";
  })();

  return (
    <div className="flex items-baseline gap-2 font-mono text-[12px]">
      <span className="flex-shrink-0 text-slate-300">{time}</span>
      <span className={`flex-shrink-0 font-bold ${color}`}>{badge}</span>
      {detail && <span className="text-slate-500">{detail}</span>}
    </div>
  );
}

// ── Connection status chip ────────────────────────────────────────────────────

function ConnectionChip({ state }: { state: StreamConnectionState }) {
  const cfg: Record<StreamConnectionState, { label: string; color: string; icon: typeof Wifi }> = {
    connecting:   { label: "Connecting…", color: "text-slate-500 bg-slate-100", icon: Loader2  },
    connected:    { label: "Live",        color: "text-emerald-700 bg-emerald-100", icon: Wifi  },
    disconnected: { label: "Offline",     color: "text-slate-500 bg-slate-100", icon: WifiOff  },
    error:        { label: "Error",       color: "text-rose-700 bg-rose-100",   icon: XCircle  },
    complete:     { label: "Complete",    color: "text-blue-700 bg-blue-100",   icon: CheckCircle2 },
  };
  const { label, color, icon: Icon } = cfg[state];
  return (
    <span className={`flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${color}`}>
      <Icon className={`h-3 w-3 ${state === "connecting" ? "animate-spin" : ""}`} />
      {label}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface LiveRunStreamProps {
  runId: string | null;
  connectionState: StreamConnectionState;
  events: RunLogEntry[];
  completedSteps: Set<string>;
  currentStep: string | null;
  exitCode: number | null;
  onCancel: () => void;
  isCancelling: boolean;
}

export function LiveRunStream({
  runId,
  connectionState,
  events,
  completedSteps,
  currentStep,
  exitCode,
  onCancel,
  isCancelling,
}: LiveRunStreamProps) {
  const logEndRef    = useRef<HTMLDivElement>(null);
  const [showLog,    setShowLog]    = useState(true);
  const [showCancel, setShowCancel] = useState(false);

  // Auto-scroll log
  useEffect(() => {
    if (showLog) {
      logEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [events, showLog]);

  const runFailed  = exitCode !== null && exitCode !== 0;
  const runSuccess = exitCode === 0;
  const isRunning  = connectionState === "connected" || connectionState === "connecting";
  const doneCount  = completedSteps.size;
  const progress   = Math.round((doneCount / 10) * 100);

  if (!runId) {
    return (
      <div className="flex h-64 items-center justify-center rounded-xl border border-dashed border-slate-200 text-sm text-slate-400">
        No active run. Launch a training run from the{" "}
        <span className="mx-1 font-semibold">Launch</span> tab.
      </div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Live Run</h2>
          <p className="mt-0.5 font-mono text-[11px] text-slate-400">
            run_id: {runId}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <ConnectionChip state={connectionState} />
          {isRunning && (
            <>
              {!showCancel ? (
                <button
                  onClick={() => setShowCancel(true)}
                  className="flex items-center gap-1.5 rounded-lg border border-rose-200 bg-rose-50 px-3 py-1.5 text-[12px] font-semibold text-rose-700 transition hover:bg-rose-100"
                >
                  <StopCircle className="h-3.5 w-3.5" />
                  Cancel
                </button>
              ) : (
                <div className="flex items-center gap-1.5">
                  <button
                    onClick={onCancel}
                    disabled={isCancelling}
                    className="rounded-lg bg-rose-600 px-3 py-1.5 text-[12px] font-semibold text-white transition hover:bg-rose-700 disabled:opacity-50"
                  >
                    {isCancelling ? "Cancelling…" : "Confirm cancel"}
                  </button>
                  <button
                    onClick={() => setShowCancel(false)}
                    className="rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[12px] text-slate-500 hover:bg-slate-50"
                  >
                    Keep
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Outcome banner */}
      {exitCode !== null && (
        <div className={`flex items-center gap-3 rounded-xl border px-4 py-3 text-sm font-medium ${
          runSuccess
            ? "border-emerald-200 bg-emerald-50 text-emerald-800"
            : "border-rose-200 bg-rose-50 text-rose-800"
        }`}>
          {runSuccess ? (
            <CheckCircle2 className="h-5 w-5 text-emerald-600" />
          ) : (
            <AlertCircle className="h-5 w-5 text-rose-600" />
          )}
          {runSuccess
            ? "Training run completed successfully. Models are registered — promote via ML Governance."
            : `Run exited with code ${exitCode}. Check the History tab and error_state files.`}
        </div>
      )}

      {/* Progress bar + step count */}
      <div>
        <div className="mb-1.5 flex items-center justify-between text-[12px] text-slate-500">
          <span>
            {doneCount} / 10 steps complete
            {isRunning && currentStep && (
              <span className="ml-2 font-medium text-blue-600">
                — running {PIPELINE_STEPS.find(s => s.key === currentStep)?.label ?? currentStep}
              </span>
            )}
          </span>
          <span>{progress}%</span>
        </div>
        <div className="h-2 overflow-hidden rounded-full bg-slate-100">
          <div
            className={`h-full rounded-full transition-all duration-700 ${
              runFailed ? "bg-rose-400" : runSuccess ? "bg-emerald-400" : "bg-blue-500"
            }`}
            style={{ width: `${progress}%` }}
          />
        </div>
      </div>

      {/* Step tracker + event log */}
      <div className="grid gap-4 md:grid-cols-2">
        {/* Step tracker */}
        <div className="rounded-xl border border-slate-100 bg-slate-50 p-4">
          <h3 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-slate-400">Pipeline Steps</h3>
          <StepTracker
            completedSteps={completedSteps}
            currentStep={currentStep}
            runFailed={runFailed}
          />
        </div>

        {/* Event log */}
        <div className="rounded-xl border border-slate-100 bg-[#0f1117]">
          <div className="flex items-center justify-between border-b border-slate-700 px-3 py-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">Structured Log</h3>
            <button
              onClick={() => setShowLog(v => !v)}
              className="text-slate-500 hover:text-slate-300"
            >
              {showLog ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </button>
          </div>
          {showLog && (
            <div className="max-h-72 overflow-y-auto p-3">
              {events.length === 0 ? (
                <p className="text-[11px] italic text-slate-600">Waiting for events…</p>
              ) : (
                <div className="space-y-1">
                  {events.map((e, i) => (
                    <EventRow key={i} entry={e} />
                  ))}
                  <div ref={logEndRef} />
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
