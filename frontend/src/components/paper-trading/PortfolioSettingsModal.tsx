"use client";

import { useEffect, useState } from "react";
import { X, SlidersHorizontal, Info, Activity } from "lucide-react";
import { useUpdatePortfolioSettings, useMonitoringConfig, useUpdateMonitoringConfig } from "@/hooks/usePaperTrading";
import type { PortfolioSummary, UpdatePortfolioSettingsRequest, MonitoringMode } from "@/types/paper_trading";

interface Props {
  portfolio: PortfolioSummary;
  onClose: () => void;
  onUpdated?: () => void;
}

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

export function PortfolioSettingsModal({ portfolio, onClose, onUpdated }: Props) {
  const { mutateAsync: updateSettings, isPending, error } = useUpdatePortfolioSettings();
  const { data: monConfig } = useMonitoringConfig();
  const { mutateAsync: updateMonConfig, isPending: isMonPending, error: monError } = useUpdateMonitoringConfig();

  const [riskPct, setRiskPct]     = useState(portfolio.risk_per_trade_pct);
  const [maxPos,  setMaxPos]      = useState(portfolio.max_open_positions);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const [monMode, setMonMode] = useState<MonitoringMode>(monConfig?.mode ?? "end_of_session");
  const [monHours, setMonHours] = useState<number>(monConfig?.duration_hours ?? 2);

  // useState initialises exactly once at mount. If the monitoring-config query
  // hasn't resolved yet (common: modal opens before the fetch completes), the
  // state is stale. This effect syncs it as soon as the server value arrives.
  useEffect(() => {
    if (monConfig) {
      setMonMode(monConfig.mode);
      setMonHours(monConfig.duration_hours ?? 2);
    }
  }, [monConfig]);

  const isDirty =
    riskPct !== portfolio.risk_per_trade_pct ||
    maxPos  !== portfolio.max_open_positions;

  // Guard on monConfig being loaded so the button stays disabled while fetching.
  const isMonDirty =
    !!monConfig && (
      monMode !== monConfig.mode ||
      (monMode === "fixed_duration" && monHours !== (monConfig.duration_hours ?? 2))
    );

  function validate(): boolean {
    const errors: Record<string, string> = {};
    if (!riskPct || riskPct < 0.1 || riskPct > 10) {
      errors.risk = "Risk must be between 0.1% and 10%.";
    }
    if (!maxPos || maxPos < 1 || maxPos > 100) {
      errors.maxPos = "Max positions must be between 1 and 100.";
    }
    if (monMode === "fixed_duration" && (monHours <= 0 || monHours > 72)) {
      errors.monHours = "Duration must be between 0.5 and 72 hours.";
    }
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;

    const saves: Promise<unknown>[] = [];

    if (isDirty) {
      const payload: UpdatePortfolioSettingsRequest = {};
      if (riskPct !== portfolio.risk_per_trade_pct) payload.risk_per_trade_pct = riskPct;
      if (maxPos  !== portfolio.max_open_positions) payload.max_open_positions = maxPos;
      saves.push(updateSettings(payload));
    }

    if (isMonDirty) {
      saves.push(
        updateMonConfig({
          mode: monMode,
          duration_hours: monMode === "fixed_duration" ? monHours : null,
        })
      );
    }

    try {
      await Promise.all(saves);
      onUpdated?.();
      onClose();
    } catch {
      // errors surfaced via `error` / `monError`
    }
  }

  const errorMessage = error
    ? ((error as { message?: string })?.message ?? "Failed to update settings.")
    : monError
    ? ((monError as { message?: string })?.message ?? "Failed to update monitoring config.")
    : null;

  const riskedPerTrade = (portfolio.current_cash * riskPct) / 100;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white shadow-2xl">

        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-slate-100">
              <SlidersHorizontal className="h-4 w-4 text-slate-600" />
            </span>
            <div>
              <h2 className="text-base font-semibold text-slate-900">Portfolio Settings</h2>
              <p className="text-xs text-slate-500">{portfolio.name}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600 transition-colors"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* Form */}
        <form onSubmit={handleSubmit} className="px-6 py-5 space-y-5">
          {errorMessage && (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-700">
              {errorMessage}
            </div>
          )}

          {/* Risk Per Trade */}
          <div className="space-y-1.5">
            <div className="flex items-center gap-1.5">
              <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Risk Per Trade
              </label>
              <span
                title="Kelly-style position sizing: qty = floor(cash × risk% / stop_distance)"
                className="text-slate-400 cursor-help"
              >
                <Info className="h-3 w-3" />
              </span>
            </div>
            <div className="flex items-center gap-3">
              <input
                type="range"
                min={0.5}
                max={5}
                step={0.5}
                value={riskPct}
                onChange={(e) => {
                  setRiskPct(Number(e.target.value));
                  setFieldErrors((prev) => ({ ...prev, risk: "" }));
                }}
                className="flex-1 accent-blue-600"
              />
              <span className="w-12 rounded-lg border border-slate-200 bg-slate-50 py-1 text-center text-sm font-semibold text-slate-900">
                {riskPct}%
              </span>
            </div>
            {riskPct !== portfolio.risk_per_trade_pct && (
              <p className="text-[11px] text-slate-400">
                Was{" "}
                <span className="font-semibold text-slate-600">
                  {portfolio.risk_per_trade_pct}%
                </span>
              </p>
            )}
            {fieldErrors.risk && (
              <p className="text-xs text-rose-600">{fieldErrors.risk}</p>
            )}
            <p className="text-[11px] text-slate-400">
              Percentage of available cash risked per trade. Drives the system quantity
              suggestion.
            </p>
          </div>

          {/* Max Open Positions */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Max Open Positions
            </label>
            <div className="flex items-center gap-2">
              {[5, 10, 15, 20].map((n) => (
                <button
                  key={n}
                  type="button"
                  onClick={() => {
                    setMaxPos(n);
                    setFieldErrors((prev) => ({ ...prev, maxPos: "" }));
                  }}
                  className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
                    maxPos === n
                      ? "bg-blue-600 text-white"
                      : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {n}
                </button>
              ))}
              <input
                type="number"
                value={maxPos}
                onChange={(e) => {
                  setMaxPos(Number(e.target.value));
                  setFieldErrors((prev) => ({ ...prev, maxPos: "" }));
                }}
                min={1}
                max={100}
                className="w-16 rounded-lg border border-slate-300 bg-white px-2 py-1 text-center text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
              />
            </div>
            {maxPos !== portfolio.max_open_positions && (
              <p className="text-[11px] text-slate-400">
                Was{" "}
                <span className="font-semibold text-slate-600">
                  {portfolio.max_open_positions}
                </span>
              </p>
            )}
            {fieldErrors.maxPos && (
              <p className="text-xs text-rose-600">{fieldErrors.maxPos}</p>
            )}
          </div>

          {/* Impact summary */}
          <div className="rounded-lg bg-blue-50 border border-blue-200 px-3 py-2.5 text-xs text-blue-800">
            <strong>{riskPct}%</strong> of{" "}
            <strong>{INR_FORMAT.format(portfolio.current_cash)}</strong> cash ={" "}
            <strong>{INR_FORMAT.format(riskedPerTrade)}</strong> risked per trade · up to{" "}
            <strong>{maxPos}</strong> concurrent positions.
          </div>

          {/* Divider */}
          <div className="border-t border-slate-100 pt-1" />

          {/* Post-Close Monitoring */}
          <div className="space-y-3">
            <div className="flex items-center gap-1.5">
              <Activity className="h-3.5 w-3.5 text-violet-500" />
              <label className="text-xs font-semibold uppercase tracking-wide text-slate-500">
                Post-Close Monitoring
              </label>
              <span
                title="After a trade is auto-closed by SL or TP, the system can continue tracking price to capture counterfactual outcomes."
                className="text-slate-400 cursor-help"
              >
                <Info className="h-3 w-3" />
              </span>
            </div>

            {/* Mode selector */}
            <div className="grid grid-cols-3 gap-1.5">
              {(
                [
                  { value: "disabled",        label: "Off",               desc: "No tracking" },
                  { value: "fixed_duration",  label: "Fixed Duration",    desc: "Track for N hours" },
                  { value: "end_of_session",  label: "End of Session",    desc: "Until 3:30 PM IST" },
                ] as { value: MonitoringMode; label: string; desc: string }[]
              ).map(({ value, label, desc }) => (
                <button
                  key={value}
                  type="button"
                  onClick={() => setMonMode(value)}
                  className={`rounded-xl border px-2 py-2 text-left transition ${
                    monMode === value
                      ? "border-violet-300 bg-violet-50 text-violet-800"
                      : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                  }`}
                >
                  <div className="text-xs font-semibold">{label}</div>
                  <div className="text-[10px] text-slate-400 mt-0.5">{desc}</div>
                </button>
              ))}
            </div>

            {/* Duration input — only for fixed_duration */}
            {monMode === "fixed_duration" && (
              <div className="space-y-1">
                <div className="flex items-center gap-3">
                  <input
                    type="range"
                    min={0.5}
                    max={24}
                    step={0.5}
                    value={monHours}
                    onChange={(e) => {
                      setMonHours(Number(e.target.value));
                      setFieldErrors((prev) => ({ ...prev, monHours: "" }));
                    }}
                    className="flex-1 accent-violet-600"
                  />
                  <span className="w-16 rounded-lg border border-slate-200 bg-slate-50 py-1 text-center text-sm font-semibold text-slate-900 shrink-0">
                    {monHours}h
                  </span>
                </div>
                {fieldErrors.monHours && (
                  <p className="text-xs text-rose-600">{fieldErrors.monHours}</p>
                )}
                <p className="text-[11px] text-slate-400">
                  Track price for {monHours} hour{monHours !== 1 ? "s" : ""} after each auto-close. Max 72h.
                </p>
              </div>
            )}

            {monMode === "end_of_session" && (
              <p className="text-[11px] text-slate-400">
                Tracks price until 3:30 PM IST on the day of the close. No monitor is created if the trade closes after market hours.
              </p>
            )}

            {monMode === "disabled" && (
              <p className="text-[11px] text-slate-400">
                Post-close tracking is off. Enable to see counterfactual data on closed trades.
              </p>
            )}
          </div>

          {/* Actions */}
          <div className="flex gap-3 pt-1">
            <button
              type="button"
              onClick={onClose}
              className="flex-1 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 hover:bg-slate-50 transition"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={(isPending || isMonPending) || (!isDirty && !isMonDirty)}
              className="flex-1 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed transition"
            >
              {(isPending || isMonPending) ? "Saving…" : "Save Changes"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
