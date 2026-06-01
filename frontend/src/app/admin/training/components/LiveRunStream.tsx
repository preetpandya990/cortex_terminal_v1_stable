"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Wifi, WifiOff, CheckCircle2, Circle, Loader2,
  XCircle, StopCircle, AlertCircle, ChevronDown, ChevronUp, Clock,
} from "lucide-react";
import type { RunLogEntry } from "@/types/admin_training";
import {
  PIPELINE_STEPS,
  STEP_MEDIAN_DURATIONS_S,
  TOTAL_EXPECTED_DURATION_S,
} from "@/types/admin_training";
import type { StreamConnectionState } from "@/hooks/useTrainingRunStream";

// ── ETA utilities ─────────────────────────────────────────────────────────────

/** Format a duration in seconds as a human-readable string. */
function formatDuration(seconds: number): string {
  if (seconds <= 0)   return "< 1 min";
  if (seconds < 60)   return `${Math.round(seconds)}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
  return s > 0 && m < 10 ? `${m}m ${s}s` : `${m}m`;
}

/** Format elapsed seconds as MM:SS for the step bar label. */
function formatElapsed(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
  return `${String(m).padStart(2, "0")}m ${String(s).padStart(2, "0")}s`;
}

// ── Step status ───────────────────────────────────────────────────────────────

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

// ── Step tracker ──────────────────────────────────────────────────────────────

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
        const status = getStepStatus(key, completedSteps, currentStep, runFailed);
        return (
          <div key={key} className="flex items-center gap-3">
            <div className="flex w-5 flex-shrink-0 items-center justify-center">
              {status === "done"    ? <CheckCircle2 className="h-4 w-4 text-emerald-500" /> :
               status === "running" ? <Loader2      className="h-4 w-4 animate-spin text-blue-500" /> :
                                      <Circle       className="h-4 w-4 text-slate-200" />}
            </div>
            <span className={`text-[13px] ${
              status === "done"    ? "font-medium text-emerald-700" :
              status === "running" ? "font-semibold text-blue-700"  :
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

// ── Event log ─────────────────────────────────────────────────────────────────

function EventRow({ entry }: { entry: RunLogEntry }) {
  const time = new Date(entry.ts).toLocaleTimeString();
  let badge = entry.event;
  let color = "text-slate-500";

  if      (entry.event === "run_start")                   { badge = "START";       color = "text-blue-600";   }
  else if (entry.event === "step_complete")               { badge = `STEP ${entry.step_num ?? "?"}`; color = "text-emerald-600"; }
  else if (entry.event === "run_finished")                { badge = "DONE";        color = "text-emerald-600";}
  else if (entry.event === "run_failed")                  { badge = "FAILED";      color = "text-rose-600";   }
  else if (entry.event === "c2_report_generated")         { badge = "C2 REPORT";   color = "text-violet-600"; }
  else if (entry.event === "f1_event_backtest_complete")  { badge = "F1 BACKTEST"; color = "text-indigo-600"; }

  const detail = (() => {
    if (entry.event === "step_complete") {
      const dur  = entry.duration_s ? `${entry.duration_s.toFixed(1)}s` : "";
      const step = PIPELINE_STEPS.find(s => s.key === entry.step)?.label ?? entry.step ?? "";
      return `${step}${dur ? ` — ${dur}` : ""}`;
    }
    if (entry.event === "run_start") {
      return `run_id=${entry.run_id} · v${entry.model_version ?? "?"}${entry.resumed ? " (resumed)" : ""}`;
    }
    if (entry.event === "c2_report_generated") return `status=${entry.status}`;
    if (entry.event === "f1_event_backtest_complete") {
      const fills  = entry.total_fills    != null ? `${entry.total_fills} fills`          : "";
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

// ── Connection chip ───────────────────────────────────────────────────────────

function ConnectionChip({ state }: { state: StreamConnectionState }) {
  const cfg: Record<StreamConnectionState, { label: string; color: string; Icon: typeof Wifi }> = {
    connecting:   { label: "Connecting…", color: "text-slate-500 bg-slate-100",     Icon: Loader2      },
    connected:    { label: "Live",        color: "text-emerald-700 bg-emerald-100", Icon: Wifi         },
    disconnected: { label: "Offline",     color: "text-slate-500 bg-slate-100",     Icon: WifiOff      },
    error:        { label: "Error",       color: "text-rose-700 bg-rose-100",       Icon: XCircle      },
    complete:     { label: "Complete",    color: "text-blue-700 bg-blue-100",       Icon: CheckCircle2 },
  };
  const { label, color, Icon } = cfg[state];
  return (
    <span className={`flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-semibold ${color}`}>
      <Icon className={`h-3 w-3 ${state === "connecting" ? "animate-spin" : ""}`} />
      {label}
    </span>
  );
}

// ── Overall progress bar ──────────────────────────────────────────────────────

interface OverallProgressBarProps {
  doneCount: number;
  totalSteps: number;
  isRunning: boolean;
  runFailed: boolean;
  runSuccess: boolean;
  overallEtaS: number | null;
  elapsedSinceRunStart: number;
}

function OverallProgressBar({
  doneCount, totalSteps, isRunning, runFailed, runSuccess, overallEtaS, elapsedSinceRunStart,
}: OverallProgressBarProps) {
  const pct = Math.round((doneCount / totalSteps) * 100);

  const trackColor = runFailed  ? "bg-rose-400"
                   : runSuccess ? "bg-emerald-400"
                   : "bg-blue-500";

  const etaLabel = (() => {
    if (runSuccess) return "Completed";
    if (runFailed)  return "Failed";
    if (!isRunning) return null;
    if (overallEtaS === null) return null;
    if (overallEtaS <= 0)    return "Finishing up…";
    return `~${formatDuration(overallEtaS)} remaining`;
  })();

  const elapsedLabel = elapsedSinceRunStart > 0
    ? `${formatElapsed(elapsedSinceRunStart)} elapsed`
    : null;

  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2 text-[12px]">
        <div className="flex items-center gap-2 text-slate-500">
          <span className="font-medium text-slate-700">{doneCount} / {totalSteps} steps</span>
          {elapsedLabel && (
            <span className="flex items-center gap-1 text-slate-400">
              <Clock className="h-3 w-3" />
              {elapsedLabel}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {etaLabel && (
            <span className={`font-medium ${
              runSuccess ? "text-emerald-600" :
              runFailed  ? "text-rose-600"    : "text-blue-600"
            }`}>
              {etaLabel}
            </span>
          )}
          <span className="font-mono text-slate-400">{pct}%</span>
        </div>
      </div>
      <div className="h-2.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={`h-full rounded-full transition-all duration-700 ${trackColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Step progress bar ─────────────────────────────────────────────────────────

interface StepProgressBarProps {
  currentStep: string;
  elapsedS: number;
  stepEtaS: number;
  isOverrunning: boolean;
  isHighVariance: boolean;
}

function StepProgressBar({
  currentStep, elapsedS, stepEtaS, isOverrunning, isHighVariance,
}: StepProgressBarProps) {
  const stepLabel = PIPELINE_STEPS.find(s => s.key === currentStep)?.label ?? currentStep;
  const medianS   = STEP_MEDIAN_DURATIONS_S[currentStep] ?? 1;

  // Cap at 95% so the bar never "completes" before the step_complete event arrives.
  const pct = Math.min(95, Math.round((elapsedS / medianS) * 100));

  const barColor = isOverrunning ? "bg-amber-400" : "bg-indigo-500";

  const etaLabel = (() => {
    if (stepEtaS <= 0) return isHighVariance ? "finishing (high variance)" : "finishing up…";
    const base = `~${formatDuration(stepEtaS)}`;
    return isHighVariance ? `${base} (high variance)` : base;
  })();

  return (
    <div className="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3">
      <div className="mb-1.5 flex items-center justify-between gap-2 text-[12px]">
        <div className="flex items-center gap-2">
          <Loader2 className="h-3 w-3 animate-spin text-indigo-500" />
          <span className="font-semibold text-slate-700">{stepLabel}</span>
          {isOverrunning && (
            <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
              overrunning
            </span>
          )}
        </div>
        <div className="flex items-center gap-3 text-slate-500">
          <span>{formatElapsed(elapsedS)} elapsed</span>
          <span className={`font-medium ${isOverrunning ? "text-amber-600" : "text-indigo-600"}`}>
            {etaLabel}
          </span>
        </div>
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-slate-200">
        <div
          className={`h-full rounded-full transition-all duration-1000 ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface LiveRunStreamProps {
  runId: string | null;
  connectionState: StreamConnectionState;
  events: RunLogEntry[];
  completedSteps: Set<string>;
  currentStep: string | null;
  stepDurations: Map<string, number>;
  stepStartTimes: Map<string, number>;
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
  stepDurations,
  stepStartTimes,
  exitCode,
  onCancel,
  isCancelling,
}: LiveRunStreamProps) {
  const logEndRef   = useRef<HTMLDivElement>(null);
  const [showLog,   setShowLog]   = useState(true);
  const [showCancel, setShowCancel] = useState(false);

  // 1-second ticker for elapsed/ETA computation.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!currentStep) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [currentStep]);

  // Auto-scroll log.
  useEffect(() => {
    if (showLog) logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [events, showLog]);

  const runFailed  = exitCode !== null && exitCode !== 0;
  const runSuccess = exitCode === 0;
  const isRunning  = connectionState === "connected" || connectionState === "connecting";

  // ── Run start time ──────────────────────────────────────────────────────────
  const runStartMs = useMemo(() => {
    const runStart = events.find(e => e.event === "run_start");
    return runStart ? Date.parse(runStart.ts) : null;
  }, [events]);

  const elapsedSinceRunStart = runStartMs ? Math.max(0, (now - runStartMs) / 1000) : 0;

  // ── Current step elapsed ────────────────────────────────────────────────────
  const currentStepStartMs = currentStep ? (stepStartTimes.get(currentStep) ?? null) : null;
  const elapsedInStepS     = currentStepStartMs ? Math.max(0, (now - currentStepStartMs) / 1000) : 0;

  // ── Overall ETA ─────────────────────────────────────────────────────────────
  // Sum expected remaining time:
  //   • Current step: max(0, median − elapsed) — may be overrunning
  //   • Future steps: their medians (actuals unknown yet)
  // Completed steps' actual durations are already "spent time" — not included.
  const overallEtaS = useMemo(() => {
    if (!isRunning) return null;

    let remaining = 0;

    for (const { key } of PIPELINE_STEPS) {
      if (completedSteps.has(key)) continue;   // already done

      const median = STEP_MEDIAN_DURATIONS_S[key] ?? 0;

      if (key === currentStep) {
        remaining += Math.max(0, median - elapsedInStepS);
      } else {
        remaining += median;
      }
    }

    return remaining;
  }, [isRunning, completedSteps, currentStep, elapsedInStepS]);

  // ── Step-level ETA ──────────────────────────────────────────────────────────
  const stepMedianS   = currentStep ? (STEP_MEDIAN_DURATIONS_S[currentStep] ?? 0) : 0;
  const stepEtaS      = Math.max(0, stepMedianS - elapsedInStepS);
  const isOverrunning = currentStep !== null && elapsedInStepS > stepMedianS * 1.2;
  const isHighVariance = currentStep === "step_6_gru";

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
          <p className="mt-0.5 font-mono text-[11px] text-slate-400">run_id: {runId}</p>
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
          {runSuccess
            ? <CheckCircle2 className="h-5 w-5 text-emerald-600" />
            : <AlertCircle  className="h-5 w-5 text-rose-600"    />}
          {runSuccess
            ? "Training run completed successfully. Models are registered — promote via ML Governance."
            : `Run exited with code ${exitCode}. Check the History tab and error_state files.`}
        </div>
      )}

      {/* Overall progress bar */}
      <OverallProgressBar
        doneCount={completedSteps.size}
        totalSteps={PIPELINE_STEPS.length}
        isRunning={isRunning}
        runFailed={runFailed}
        runSuccess={runSuccess}
        overallEtaS={overallEtaS}
        elapsedSinceRunStart={elapsedSinceRunStart}
      />

      {/* Current step progress bar — only shown while a step is actively running */}
      {isRunning && currentStep && (
        <StepProgressBar
          currentStep={currentStep}
          elapsedS={elapsedInStepS}
          stepEtaS={stepEtaS}
          isOverrunning={isOverrunning}
          isHighVariance={isHighVariance}
        />
      )}

      {/* Step tracker + event log */}
      <div className="grid gap-4 md:grid-cols-2">
        <div className="rounded-xl border border-slate-100 bg-slate-50 p-4">
          <h3 className="mb-3 text-[12px] font-semibold uppercase tracking-wide text-slate-400">Pipeline Steps</h3>
          <StepTracker
            completedSteps={completedSteps}
            currentStep={currentStep}
            runFailed={runFailed}
          />
        </div>

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
                  {events.map((e, i) => <EventRow key={i} entry={e} />)}
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
