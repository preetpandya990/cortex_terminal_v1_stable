"use client";

import { useState } from "react";
import { Rocket, ChevronDown, ChevronUp, AlertTriangle } from "lucide-react";
import type { LaunchRequest, PreflightReport } from "@/types/admin_training";

interface LaunchFormProps {
  preflight: PreflightReport | null;
  isLaunching: boolean;
  onLaunch: (req: LaunchRequest) => void;
  activeFeedbackBundlePath?: string | null;
}

export function LaunchForm({ preflight, isLaunching, onLaunch, activeFeedbackBundlePath }: LaunchFormProps) {
  const [reason,            setReason]            = useState("");
  const [overrideSchedule,  setOverrideSchedule]  = useState(false);
  const [useFeedback,       setUseFeedback]        = useState(!!activeFeedbackBundlePath);
  const [showAdvanced,      setShowAdvanced]       = useState(false);
  const [modelVersion,      setModelVersion]       = useState("");
  const [nSymbols,          setNSymbols]           = useState("");
  const [lookbackYears,     setLookbackYears]      = useState("");
  const [xgbTrials,         setXgbTrials]          = useState("");
  const [gruTrials,         setGruTrials]          = useState("");
  const [showConfirm,       setShowConfirm]        = useState(false);

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
      override_schedule_warning: overrideSchedule,
      feedback_weights_path: (useFeedback && activeFeedbackBundlePath) ? activeFeedbackBundlePath : null,
      config: {
        model_version: modelVersion.trim() || undefined,
        n_symbols:      nSymbols     ? parseInt(nSymbols)     : undefined,
        lookback_years: lookbackYears ? parseInt(lookbackYears) : undefined,
        xgboost_trials: xgbTrials    ? parseInt(xgbTrials)    : undefined,
        gru_trials:     gruTrials    ? parseInt(gruTrials)    : undefined,
      },
    };
    onLaunch(req);
    setShowConfirm(false);
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold text-slate-800">Launch Training Run</h2>
        <p className="mt-0.5 text-[13px] text-slate-500">
          Starts a fresh challenger run (
          <code className="rounded bg-slate-100 px-1 text-[11px]">--fresh</code>
          ). The current checkpoint is preserved for resumption.
        </p>
      </div>

      {/* Blocked state */}
      {hasFailure && (
        <div className="flex items-start gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3">
          <AlertTriangle className="mt-0.5 h-5 w-5 flex-shrink-0 text-rose-500" />
          <p className="text-sm text-rose-800">
            Pre-flight has failing gates. Fix them on the{" "}
            <span className="font-semibold">Preflight</span> tab before launching.
          </p>
        </div>
      )}

      {/* Schedule override */}
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

      {/* Feedback bundle toggle */}
      {activeFeedbackBundlePath && (
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
                Reinforces profitable signals (TP3 → 3×) and up-weights confident wrong calls (2×)
                to help the model learn from high-confidence failures.
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
          placeholder="E.g. Weekly challenger run after fundamentals backfill completed"
          className="mt-1.5 w-full resize-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-[13px] text-slate-800 placeholder-slate-400 shadow-sm outline-none ring-0 focus:border-blue-400 focus:ring-2 focus:ring-blue-100 disabled:opacity-50"
          disabled={hasFailure || isLaunching}
        />
        <div className="mt-1 flex justify-between text-[11px] text-slate-400">
          <span>{reason.trim().length < 5 ? "Minimum 5 characters required" : "✓"}</span>
          <span>{reason.length}/500</span>
        </div>
      </div>

      {/* Advanced config overrides */}
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
              Leave blank to use production defaults. Overrides are logged but not yet forwarded
              to the orchestrator (Phase 2 wiring).
            </p>
            <div className="grid gap-3 sm:grid-cols-2">
              {[
                { label: "Model version", value: modelVersion, setter: setModelVersion, placeholder: "e.g. 1.2.0" },
                { label: "N symbols",     value: nSymbols,     setter: setNSymbols,     placeholder: "default 2557", type: "number" },
                { label: "Lookback years",value: lookbackYears,setter: setLookbackYears,placeholder: "default 10",   type: "number" },
                { label: "XGBoost trials",value: xgbTrials,    setter: setXgbTrials,    placeholder: "default 100",  type: "number" },
                { label: "GRU trials",    value: gruTrials,    setter: setGruTrials,    placeholder: "default 15",   type: "number" },
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

      {/* Launch button */}
      {!showConfirm ? (
        <button
          onClick={() => setShowConfirm(true)}
          disabled={!canLaunch}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-blue-600 px-4 py-3 text-sm font-semibold text-white shadow-sm transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          <Rocket className="h-4 w-4" />
          Launch Training Run
        </button>
      ) : (
        <div className="rounded-xl border border-blue-200 bg-blue-50 p-4">
          <p className="text-sm font-semibold text-blue-900">Confirm launch?</p>
          <p className="mt-1 text-[13px] text-blue-700">
            This will start a full production training run (hours long). The current
            checkpoint will be overwritten by the{" "}
            <code className="rounded bg-blue-100 px-1">--fresh</code> flag.
          </p>
          <div className="mt-3 flex gap-2">
            <button
              onClick={handleSubmit}
              disabled={isLaunching}
              className="flex-1 rounded-lg bg-blue-600 py-2 text-sm font-semibold text-white transition hover:bg-blue-700 disabled:opacity-50"
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
