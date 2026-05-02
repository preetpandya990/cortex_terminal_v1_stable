"use client";

import { useState } from "react";
import { X, Wallet, Info } from "lucide-react";
import { useCreatePortfolio } from "@/hooks/usePaperTrading";
import type { CreatePortfolioRequest } from "@/types/paper_trading";

interface Props {
  onClose: () => void;
  onCreated?: () => void;
}

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const CAPITAL_PRESETS = [100_000, 500_000, 1_000_000, 5_000_000];

export function CreatePortfolioModal({ onClose, onCreated }: Props) {
  const { mutateAsync: createPortfolio, isPending, error } = useCreatePortfolio();

  const [form, setForm] = useState<CreatePortfolioRequest>({
    name: "My Paper Portfolio",
    initial_capital: 500_000,
    risk_per_trade_pct: 2.0,
    max_open_positions: 10,
  });
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  function validate(): boolean {
    const errors: Record<string, string> = {};
    if (!form.name.trim() || form.name.trim().length < 2) {
      errors.name = "Name must be at least 2 characters.";
    }
    if (form.initial_capital <= 0 || form.initial_capital > 100_000_000) {
      errors.initial_capital = "Capital must be between ₹1 and ₹10 Cr.";
    }
    if (!form.risk_per_trade_pct || form.risk_per_trade_pct <= 0 || form.risk_per_trade_pct > 10) {
      errors.risk_per_trade_pct = "Risk must be between 0.1% and 10%.";
    }
    if (!form.max_open_positions || form.max_open_positions < 1 || form.max_open_positions > 100) {
      errors.max_open_positions = "Max positions must be between 1 and 100.";
    }
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;
    try {
      await createPortfolio({ ...form, name: form.name.trim() });
      onCreated?.();
      onClose();
    } catch {
      // error surfaced via `error` from useMutation
    }
  }

  const errorMessage = error
    ? ((error as any)?.message ?? "Failed to create portfolio.")
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white shadow-2xl">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-200 px-6 py-4">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-blue-50">
              <Wallet className="h-4 w-4 text-blue-600" />
            </span>
            <div>
              <h2 className="text-base font-semibold text-slate-900">Create Paper Portfolio</h2>
              <p className="text-xs text-slate-500">Simulate trades with virtual capital.</p>
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

          {/* Portfolio Name */}
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Portfolio Name
            </label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              placeholder="My Paper Portfolio"
              maxLength={100}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 placeholder:text-slate-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
            />
            {fieldErrors.name && (
              <p className="text-xs text-rose-600">{fieldErrors.name}</p>
            )}
          </div>

          {/* Initial Capital */}
          <div className="space-y-2">
            <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
              Initial Capital (INR)
            </label>
            <div className="flex flex-wrap gap-2 mb-2">
              {CAPITAL_PRESETS.map((preset) => (
                <button
                  key={preset}
                  type="button"
                  onClick={() => setForm((f) => ({ ...f, initial_capital: preset }))}
                  className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
                    form.initial_capital === preset
                      ? "bg-blue-600 text-white"
                      : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {INR_FORMAT.format(preset)}
                </button>
              ))}
            </div>
            <input
              type="number"
              value={form.initial_capital}
              onChange={(e) =>
                setForm((f) => ({ ...f, initial_capital: Number(e.target.value) }))
              }
              min={1}
              max={100_000_000}
              step={10000}
              className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
            />
            {fieldErrors.initial_capital && (
              <p className="text-xs text-rose-600">{fieldErrors.initial_capital}</p>
            )}
          </div>

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
                value={form.risk_per_trade_pct}
                onChange={(e) =>
                  setForm((f) => ({ ...f, risk_per_trade_pct: Number(e.target.value) }))
                }
                className="flex-1 accent-blue-600"
              />
              <span className="w-12 rounded-lg border border-slate-200 bg-slate-50 py-1 text-center text-sm font-semibold text-slate-900">
                {form.risk_per_trade_pct}%
              </span>
            </div>
            {fieldErrors.risk_per_trade_pct && (
              <p className="text-xs text-rose-600">{fieldErrors.risk_per_trade_pct}</p>
            )}
            <p className="text-[11px] text-slate-400">
              Percentage of cash risked per trade. Drives the system quantity suggestion.
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
                  onClick={() => setForm((f) => ({ ...f, max_open_positions: n }))}
                  className={`rounded-full px-3 py-1 text-xs font-semibold transition ${
                    form.max_open_positions === n
                      ? "bg-blue-600 text-white"
                      : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                  }`}
                >
                  {n}
                </button>
              ))}
              <input
                type="number"
                value={form.max_open_positions}
                onChange={(e) =>
                  setForm((f) => ({ ...f, max_open_positions: Number(e.target.value) }))
                }
                min={1}
                max={100}
                className="w-16 rounded-lg border border-slate-300 bg-white px-2 py-1 text-center text-sm text-slate-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20 transition"
              />
            </div>
            {fieldErrors.max_open_positions && (
              <p className="text-xs text-rose-600">{fieldErrors.max_open_positions}</p>
            )}
          </div>

          {/* Summary */}
          <div className="rounded-lg bg-blue-50 border border-blue-200 px-3 py-2.5 text-xs text-blue-800">
            <strong>{INR_FORMAT.format(form.initial_capital)}</strong> capital ·{" "}
            <strong>{form.risk_per_trade_pct}%</strong> risk ={" "}
            <strong>
              {INR_FORMAT.format(
                (form.initial_capital * (form.risk_per_trade_pct ?? 2)) / 100
              )}
            </strong>{" "}
            risked per trade · up to{" "}
            <strong>{form.max_open_positions}</strong> concurrent positions.
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
              disabled={isPending}
              className="flex-1 rounded-xl bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-blue-700 disabled:opacity-60 disabled:cursor-not-allowed transition"
            >
              {isPending ? "Creating…" : "Create Portfolio"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
