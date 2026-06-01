"use client";

import { useEffect, useState } from "react";
import {
  Rocket, ChevronDown, ChevronUp, AlertTriangle,
  RotateCcw, Sparkles, CheckCircle2, Clock,
} from "lucide-react";
import type { CheckpointStatus, LaunchMode, LaunchRequest, PreflightReport } from "@/types/admin_training";
import { PIPELINE_STEPS } from "@/types/admin_training";

// ── Checkpoint summary chip ───────────────────────────────────────────────────

function CheckpointSummary({ cp }: { cp: CheckpointStatus }) {
  const doneCount  = cp.completed_steps.length;
  const totalSteps = PIPELINE_STEPS.length;
  const nextLabel  = PIPELINE_STEPS.find(s => s.key === cp.next_step)?.label ?? cp.next_step ?? "Unknown";

  return (
    <div className="mt-2 space-y-1.5 text-[12px] text-slate-600">
      <div className="flex items-center gap-2">
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
        <span>
          <span className="font-semibold">{doneCount} / {totalSteps} steps complete</span>
          {cp.model_version && (
            <span className="ml-1.5 text-slate-400">· v{cp.model_version}</span>
          )}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <RotateCcw className="h-3.5 w-3.5 text-blue-500" />
        <span>
          Next: <span className="font-semibold text-blue-700">{nextLabel}</span>
          {cp.next_step === "step_6_gru" && cp.gru_last_epoch != null && (
            <span className="ml-1 text-slate-400">
              — resuming from epoch {cp.gru_last_epoch}
            </span>
          )}
        </span>
      </div>
      {cp.run_id && (
        <div className="flex items-center gap-2 text-slate-400">
          <Clock className="h-3 w-3" />
          <span className="font-mono text-[11px]">{cp.run_id}</span>
        </div>
      )}
    </div>
  );
}

// ── Launch mode selector ──────────────────────────────────────────────────────

interface ModeSelectorProps {
  mode: LaunchMode;
  onChange: (m: LaunchMode) => void;
  checkpoint: CheckpointStatus | null;
  disabled: boolean;
}

function ModeSelector({ mode, onChange, checkpoint, disabled }: ModeSelectorProps) {
  const hasResumable = !!checkpoint?.resumable;

  return (
    <div className="space-y-2">
      <p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">
        Launch Mode
      </p>

      {/* Resume card */}
      <button
        type="button"
        disabled={disabled || !hasResumable}
        onClick={() => hasResumable && onChange("resume")}
        className={`w-full rounded-xl border px-4 py-3 text-left transition ${
          mode === "resume" && hasResumable
            ? "border-blue-300 bg-blue-50 ring-1 ring-blue-200"
            : hasResumable
            ? "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
            : "cursor-not-allowed border-slate-100 bg-slate-50 opacity-50"
        }`}
      >
        <div className="flex items-start gap-3">
          <div className={`mt-0.5 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full border-2 transition ${
            mode === "resume" && hasResumable
              ? "border-blue-500 bg-blue-500"
              : "border-slate-300"
          }`}>
            {mode === "resume" && hasResumable && (
              <div className="h-1.5 w-1.5 rounded-full bg-white" />
            )}
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <RotateCcw className="h-3.5 w-3.5 text-blue-600" />
              <span className="text-[13px] font-semibold text-slate-800">
                Resume Checkpoint
              </span>
              {hasResumable && (
                <span className="rounded-full bg-blue-100 px-2 py-0.5 text-[10px] font-semibold text-blue-700">
                  Recommended
                </span>
              )}
            </div>
            {hasResumable && checkpoint ? (
              <CheckpointSummary cp={checkpoint} />
            ) : (
              <p className="mt-1 text-[12px] text-slate-400">
                No resumable checkpoint available.
              </p>
            )}
          </div>
        </div>
      </button>

      {/* Fresh run card */}
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange("fresh")}
        className={`w-full rounded-xl border px-4 py-3 text-left transition ${
          mode === "fresh"
            ? "border-slate-400 bg-white ring-1 ring-slate-300"
            : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50"
        }`}
      >
        <div className="flex items-start gap-3">
          <div className={`mt-0.5 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-full border-2 transition ${
            mode === "fresh" ? "border-slate-600 bg-slate-600" : "border-slate-300"
          }`}>
            {mode === "fresh" && (
              <div className="h-1.5 w-1.5 rounded-full bg-white" />
            )}
          </div>
          <div>
            <div className="flex items-center gap-2">
              <Sparkles className="h-3.5 w-3.5 text-slate-600" />
              <span className="text-[13px] font-semibold text-slate-800">Fresh Run</span>
            </div>
            <p className="mt-1 text-[12px] text-slate-500">
              Start a completely new training run.
              {checkpoint?.resumable && (
                <span className="ml-1 text-amber-600 font-medium">
                  The existing checkpoint will be discarded.
                </span>
              )}
            </p>
          </div>
        </div>
      </button>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface LaunchFormProps {
  preflight: PreflightReport | null;
  checkpoint: CheckpointStatus | null;
  isLaunching: boolean;
  onLaunch: (req: LaunchRequest) => void;
  activeFeedbackBundlePath?: string | null;
}

export function LaunchForm({
  preflight,
  checkpoint,
  isLaunching,
  onLaunch,
  activeFeedbackBundlePath,
}: LaunchFormProps) {
  const hasResumable = !!checkpoint?.resumable;

  const [mode,             setMode]            = useState<LaunchMode>(
    hasResumable ? "resume" : "fresh"
  );
  const [reason,           setReason]          = useState("");
  const [overrideSchedule, setOverrideSchedule] = useState(false);
  const [useFeedback,      setUseFeedback]     = useState(!!activeFeedbackBundlePath);
  const [showAdvanced,     setShowAdvanced]    = useState(false);
  const [modelVersion,     setModelVersion]    = useState("");
  const [nSymbols,         setNSymbols]        = useState("");
  const [lookbackYears,    setLookbackYears]   = useState("");
  const [xgbTrials,        setXgbTrials]       = useState("");
  const [gruTrials,        setGruTrials]       = useState("");
  const [showConfirm,      setShowConfirm]     = useState(false);

  // Keep mode in sync if checkpoint changes (e.g. after page refresh)
  useEffect(() => {
    if (hasResumable && mode === "fresh") {
      // Don't auto-switch if operator explicitly chose fresh — only set on first load
    }
    if (!hasResumable && mode === "resume") {
      setMode("fresh");
    }
  }, [hasResumable]); // eslint-disable-line react-hooks/exhaustive-deps

  const scheduleWarn = preflight?.probes.find(
    p => p.name === "schedule" && p.status === "warn"
  );
  const hasFailure   = preflight ? preflight.probes.some(p => p.status === "fail") : true;
  const needsOverride = !!scheduleWarn && !overrideSchedule;

  const canLaunch = (
    !hasFailure &&
    !needsOverride &&
    reason.trim().length >= 5 &&
    !isLaunching
  );

  function handleSubmit() {
    if (!canLaunch) return;

    const req: LaunchRequest = {
      reason: reason.trim(),
      mode,
      override_schedule_warning: overrideSchedule,
      // config overrides and feedback weights are only honoured in fresh mode
      ...(mode === "fresh" && {
        feedback_weights_path: (useFeedback && activeFeedbackBundlePath)
          ? activeFeedbackBundlePath
          : null,
        config: {
          model_version: modelVersion.trim() || undefined,
          n_symbols:      nSymbols      ? parseInt(nSymbols)     : undefined,
          lookback_years: lookbackYears ? parseInt(lookbackYears) : undefined,
          xgboost_trials: xgbTrials     ? parseInt(xgbTrials)    : undefined,
          gru_trials:     gruTrials     ? parseInt(gruTrials)    : undefined,
        },
      }),
    };
    onLaunch(req);
    setShowConfirm(false);
  }

  const confirmCopy = mode === "resume"
    ? (() => {
        const next = PIPELINE_STEPS.find(s => s.key === checkpoint?.next_step)?.label ?? "next step";
        const gruNote = checkpoint?.next_step === "step_6_gru" && checkpoint.gru_last_epoch != null
          ? ` from epoch ${checkpoint.gru_last_epoch}`
          : "";
        return `This will resume run ${checkpoint?.run_id ?? ""} — continuing from ${next}${gruNote}. Already-completed steps will be skipped.`;
      })()
    : "This will start a full production training run (hours long). The existing checkpoint will be overwritten by the --fresh flag.";

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold text-slate-800">Launch Training Run</h2>
        <p className="mt-0.5 text-[13px] text-slate-500">
          Select a mode, document your reason, and confirm launch. All actions are audited.
        </p>
      </div>

      {/* Preflight failure block */}
      {hasFailure && (
        <div className="flex items-start gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-rose-500" />
          <p className="text-sm text-rose-800">
            Pre-flight has failing gates. Fix them on the{" "}
            <span className="font-semibold">Preflight</span> tab before launching.
          </p>
        </div>
      )}

      {/* Launch mode selector */}
      <ModeSelector
        mode={mode}
        onChange={m => { setMode(m); setShowConfirm(false); }}
        checkpoint={checkpoint}
        disabled={hasFailure || isLaunching}
      />

      {/* Schedule override — shown for both modes */}
      {scheduleWarn && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3">
          <div className="flex items-start gap-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 flex-shrink-0 text-amber-600" />
            <p className="text-[13px] text-amber-800">{scheduleWarn.message}</p>
          </div>
          <label className="mt-3 flex items-center gap-2 text-[13px] font-medium text-amber-900">
            <input
              type="checkbox"
              checked={overrideSchedule}
              onChange={e => setOverrideSchedule(e.target.checked)}
              className="h-4 w-4 rounded border-amber-400 text-amber-600"
            />
            I acknowledge the timing conflict — launch anyway
          </label>
        </div>
      )}

      {/* Feedback bundle toggle — fresh mode only */}
      {mode === "fresh" && activeFeedbackBundlePath && (
        <div className="rounded-xl border border-indigo-200 bg-indigo-50 px-4 py-3">
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              checked={useFeedback}
              onChange={e => setUseFeedback(e.target.checked)}
              disabled={hasFailure || isLaunching}
              className="mt-0.5 h-4 w-4 rounded border-indigo-400 accent-indigo-600"
            />
            <div>
              <span className="font-semibold text-indigo-900">Use feedback bundle (B1+B2 weights)</span>
              <p className="mt-0.5 text-[12px] text-indigo-600">
                {activeFeedbackBundlePath.split("/").pop()}
              </p>
              <p className="mt-1 text-[11px] text-indigo-500">
                Reinforces profitable signals (TP3 → 3×) and up-weights confident wrong calls (2×).
              </p>
            </div>
          </label>
        </div>
      )}

      {/* Reason field */}
      <div>
        <label className="block text-sm font-medium text-slate-700">
          Reason <span className="text-rose-500">*</span>
        </label>
        <textarea
          value={reason}
          onChange={e => setReason(e.target.value)}
          rows={3}
          maxLength={500}
          placeholder={
            mode === "resume"
              ? "E.g. Resuming after OOM kill on GRU step — checkpointed at epoch 179"
              : "E.g. Weekly challenger run after fundamentals backfill completed"
          }
          className="mt-1.5 w-full resize-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] text-slate-800 placeholder-slate-400 shadow-sm outline-none focus:border-blue-400 focus:ring-2 focus:ring-blue-100 disabled:opacity-50"
          disabled={hasFailure || isLaunching}
        />
        <div className="mt-1 flex justify-between text-[11px] text-slate-400">
          <span>{reason.trim().length < 5 ? "Minimum 5 characters required" : "✓"}</span>
          <span>{reason.length}/500</span>
        </div>
      </div>

      {/* Config overrides — fresh mode only */}
      {mode === "fresh" && (
        <div className="rounded-xl border border-slate-100 bg-slate-50">
          <button
            type="button"
            onClick={() => setShowAdvanced(v => !v)}
            className="flex w-full items-center justify-between px-4 py-3 text-sm font-medium text-slate-600 transition hover:text-slate-900"
          >
            <span>Config overrides (optional)</span>
            {showAdvanced ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
          </button>
          {showAdvanced && (
            <div className="border-t border-slate-100 px-4 pb-4 pt-3">
              <p className="mb-3 text-[11px] text-slate-400">
                Leave blank to use production defaults.
              </p>
              <div className="grid gap-3 sm:grid-cols-2">
                {[
                  { label: "Model version",  value: modelVersion,  setter: setModelVersion,  placeholder: "e.g. 1.2.0" },
                  { label: "N symbols",      value: nSymbols,      setter: setNSymbols,      placeholder: "default 2557", type: "number" },
                  { label: "Lookback years", value: lookbackYears, setter: setLookbackYears, placeholder: "default 10",   type: "number" },
                  { label: "XGBoost trials", value: xgbTrials,     setter: setXgbTrials,     placeholder: "default 100",  type: "number" },
                  { label: "GRU trials",     value: gruTrials,     setter: setGruTrials,     placeholder: "default 15",   type: "number" },
                ].map(({ label, value, setter, placeholder, type }) => (
                  <div key={label}>
                    <label className="mb-1 block text-[11px] font-medium text-slate-500">{label}</label>
                    <input
                      type={type ?? "text"}
                      value={value}
                      onChange={e => setter(e.target.value)}
                      placeholder={placeholder}
                      className="w-full rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-[13px] text-slate-700 placeholder-slate-400 outline-none focus:border-blue-300 focus:ring-1 focus:ring-blue-100"
                    />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Launch / confirm */}
      {!showConfirm ? (
        <button
          onClick={() => setShowConfirm(true)}
          disabled={!canLaunch}
          className={`flex w-full items-center justify-center gap-2 rounded-xl px-4 py-3 text-sm font-semibold text-white shadow-sm transition disabled:cursor-not-allowed disabled:opacity-40 ${
            mode === "resume"
              ? "bg-blue-600 hover:bg-blue-700"
              : "bg-slate-700 hover:bg-slate-800"
          }`}
        >
          {mode === "resume" ? (
            <RotateCcw className="h-4 w-4" />
          ) : (
            <Rocket className="h-4 w-4" />
          )}
          {mode === "resume" ? "Resume Training Run" : "Launch Fresh Run"}
        </button>
      ) : (
        <div className={`rounded-xl border p-4 ${
          mode === "resume"
            ? "border-blue-200 bg-blue-50"
            : "border-slate-200 bg-slate-50"
        }`}>
          <p className={`text-sm font-semibold ${
            mode === "resume" ? "text-blue-900" : "text-slate-800"
          }`}>
            Confirm {mode === "resume" ? "resume" : "launch"}?
          </p>
          <p className={`mt-1 text-[13px] ${
            mode === "resume" ? "text-blue-700" : "text-slate-600"
          }`}>
            {confirmCopy}
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={handleSubmit}
              disabled={isLaunching}
              className={`flex-1 rounded-lg py-2 text-sm font-semibold text-white transition disabled:opacity-50 ${
                mode === "resume"
                  ? "bg-blue-600 hover:bg-blue-700"
                  : "bg-slate-700 hover:bg-slate-800"
              }`}
            >
              {isLaunching ? "Launching…" : "Confirm"}
            </button>
            <button
              onClick={() => setShowConfirm(false)}
              className="flex-1 rounded-lg border border-slate-200 bg-white py-2 text-sm font-medium text-slate-600 transition hover:bg-slate-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
