"use client";

import { useCallback, useEffect, useState } from "react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import {
  Zap, RefreshCw, Loader2, PackagePlus, CheckCircle2,
  AlertTriangle, Calendar, Hash, TrendingUp, ChevronDown, ChevronUp,
} from "lucide-react";
import type { FeedbackBundleInfo } from "@/types/admin_training";
import { adminTrainingAPI, apiErrorMessage } from "@/lib/api";
import { useToast } from "@/components/ui/toast";

// ── Histogram chart ───────────────────────────────────────────────────────────

function WeightHistogram({ bins, counts }: { bins: number[]; counts: number[] }) {
  const data = counts.map((count, i) => ({
    range: `${bins[i].toFixed(1)}–${bins[i + 1].toFixed(1)}`,
    count,
    midpoint: (bins[i] + bins[i + 1]) / 2,
  }));

  return (
    <div className="h-48">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 4, right: 8, bottom: 16, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="#f1f5f9" />
          <XAxis
            dataKey="range"
            tick={{ fontSize: 10, fill: "#94a3b8" }}
            tickLine={false}
            interval={3}
          />
          <YAxis tick={{ fontSize: 10, fill: "#94a3b8" }} tickLine={false} axisLine={false} />
          <Tooltip
            contentStyle={{ fontSize: 12, borderRadius: 8, border: "1px solid #e2e8f0" }}
            labelStyle={{ fontWeight: 600, color: "#334155" }}
          />
          <ReferenceLine x="1.0–1.1" stroke="#94a3b8" strokeDasharray="4 2" label={{ value: "neutral", fontSize: 9, fill: "#94a3b8" }} />
          <Bar dataKey="count" fill="#6366f1" radius={[3, 3, 0, 0]} name="Outcomes" />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ── Stat chip ─────────────────────────────────────────────────────────────────

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="flex flex-col items-center rounded-xl border border-slate-100 bg-slate-50 px-3 py-2">
      <span className="text-[11px] font-medium text-slate-400">{label}</span>
      <span className="mt-0.5 font-mono text-sm font-semibold text-slate-700">{value}</span>
    </div>
  );
}

// ── Top symbols table ─────────────────────────────────────────────────────────

function TopSymbols({ symbols }: { symbols: FeedbackBundleInfo["top_symbols"] }) {
  return (
    <div className="overflow-hidden rounded-xl border border-slate-100">
      <table className="w-full text-[12px]">
        <thead>
          <tr className="border-b border-slate-100 bg-slate-50 text-left">
            <th className="py-2 pl-3 pr-2 font-semibold uppercase tracking-wide text-slate-400">Symbol</th>
            <th className="py-2 pr-2 font-semibold uppercase tracking-wide text-slate-400">Mean weight</th>
            <th className="py-2 pr-3 font-semibold uppercase tracking-wide text-slate-400">Outcomes</th>
          </tr>
        </thead>
        <tbody>
          {symbols.map((s, i) => (
            <tr key={i} className="border-b border-slate-50 hover:bg-slate-50/60">
              <td className="py-1.5 pl-3 pr-2 font-mono text-slate-600">
                {s.symbol.split("|").pop() ?? s.symbol}
              </td>
              <td className="py-1.5 pr-2">
                <span className={`font-semibold ${
                  s.mean_weight >= 2 ? "text-emerald-600" :
                  s.mean_weight <= 0.6 ? "text-rose-600" : "text-slate-600"
                }`}>
                  {s.mean_weight.toFixed(3)}×
                </span>
              </td>
              <td className="py-1.5 pr-3 text-slate-400">{s.n_outcomes}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Bundle card ───────────────────────────────────────────────────────────────

function BundleCard({
  bundle,
  isSelected,
  onSelect,
}: {
  bundle: FeedbackBundleInfo;
  isSelected: boolean;
  onSelect: (path: string | null) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const filename = bundle.bundle_path.split("/").pop() ?? bundle.bundle_path;

  return (
    <div className={`rounded-xl border transition-colors ${
      isSelected ? "border-blue-300 bg-blue-50/40" : "border-slate-200 bg-white"
    }`}>
      <div className="flex items-center gap-3 px-4 py-3">
        <input
          type="radio"
          checked={isSelected}
          onChange={() => onSelect(isSelected ? null : bundle.bundle_path)}
          className="h-4 w-4 accent-blue-600"
        />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12px] font-medium text-slate-700 truncate">{filename}</span>
            <span className="flex-shrink-0 rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[9px] text-slate-400">
              {bundle.sha256.slice(0, 8)}
            </span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-slate-400">
            <span className="flex items-center gap-1">
              <Hash className="h-3 w-3" />
              {bundle.total_raw_outcomes.toLocaleString()} trades
              <span className="text-slate-300">→</span>
              {bundle.row_count.toLocaleString()} bars
            </span>
            <span className="flex items-center gap-1">
              <Calendar className="h-3 w-3" />
              {bundle.window_start} → {bundle.window_end}
            </span>
            <span className="flex items-center gap-1">
              <TrendingUp className="h-3 w-3" />
              mean {bundle.weight_mean.toFixed(3)}×
            </span>
          </div>
        </div>
        <button
          onClick={() => setExpanded(v => !v)}
          className="flex-shrink-0 rounded-lg p-1.5 text-slate-400 hover:bg-slate-100 hover:text-slate-600"
        >
          {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
        </button>
      </div>

      {expanded && (
        <div className="border-t border-slate-100 px-4 pb-4 pt-3 space-y-4">
          {/* Distribution stats */}
          <div className="grid grid-cols-5 gap-2">
            <Stat label="P5"   value={`${bundle.weight_p5.toFixed(2)}×`} />
            <Stat label="P50"  value={`${bundle.weight_p50.toFixed(2)}×`} />
            <Stat label="P95"  value={`${bundle.weight_p95.toFixed(2)}×`} />
            <Stat label="Mean" value={`${bundle.weight_mean.toFixed(3)}×`} />
            <Stat label="Std"  value={`${bundle.weight_std.toFixed(3)}`} />
          </div>

          {/* Histogram */}
          <div>
            <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
              Weight Distribution
            </p>
            <WeightHistogram bins={bundle.histogram_bins} counts={bundle.histogram_counts} />
          </div>

          {/* Top symbols */}
          {bundle.top_symbols.length > 0 && (
            <div>
              <p className="mb-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                Top 10 by Mean Weight
              </p>
              <TopSymbols symbols={bundle.top_symbols} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

interface FeedbackPreviewProps {
  selectedBundlePath: string | null;
  onBundleSelect: (path: string | null) => void;
}

export function FeedbackPreview({ selectedBundlePath, onBundleSelect }: FeedbackPreviewProps) {
  const { success: toastOk, error: toastErr } = useToast();
  const [bundles,   setBundles]   = useState<FeedbackBundleInfo[]>([]);
  const [loading,   setLoading]   = useState(false);
  const [building,  setBuilding]  = useState(false);

  const fetchBundles = useCallback(async () => {
    setLoading(true);
    try {
      const res = await adminTrainingAPI.listFeedbackBundles();
      setBundles(res.bundles);
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [toastErr]);

  useEffect(() => { fetchBundles(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleBuild = async () => {
    setBuilding(true);
    try {
      const bundle = await adminTrainingAPI.buildFeedbackBundle();
      toastOk(`Bundle built: ${bundle.row_count.toLocaleString()} outcomes weighted`);
      await fetchBundles();
      // Auto-select the newly built bundle
      onBundleSelect(bundle.bundle_path);
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setBuilding(false);
    }
  };

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-slate-800">Feedback Weights</h2>
          <p className="mt-0.5 text-[13px] text-slate-500">
            Build a B1×B2 weight bundle from matured paper-trade outcomes, preview
            the distribution, and activate it for the next training launch.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fetchBundles}
            disabled={loading}
            className="flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-600 shadow-sm transition hover:bg-slate-50 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          </button>
          <button
            onClick={handleBuild}
            disabled={building}
            className="flex items-center gap-1.5 rounded-xl bg-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-700 disabled:opacity-50"
          >
            {building ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <PackagePlus className="h-4 w-4" />
            )}
            {building ? "Building…" : "Build Bundle"}
          </button>
        </div>
      </div>

      {/* Active selection banner */}
      {selectedBundlePath && (
        <div className="flex items-center gap-3 rounded-xl border border-blue-200 bg-blue-50 px-4 py-2.5 text-sm">
          <CheckCircle2 className="h-4 w-4 flex-shrink-0 text-blue-600" />
          <span className="text-blue-800">
            <span className="font-semibold">Active bundle:</span>{" "}
            <span className="font-mono text-[12px]">{selectedBundlePath.split("/").pop()}</span>
          </span>
          <button
            onClick={() => onBundleSelect(null)}
            className="ml-auto text-[12px] text-blue-500 hover:text-blue-700 hover:underline"
          >
            Clear
          </button>
        </div>
      )}

      {/* Formula reference */}
      <div className="rounded-xl border border-slate-100 bg-slate-50 px-4 py-3">
        <p className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
          Weight Formula (B1 × B2, clipped [0.1, 5.0])
        </p>
        <div className="grid gap-2 text-[12px] sm:grid-cols-2">
          <div>
            <p className="font-semibold text-slate-600 mb-1">Outcome factor (B1):</p>
            {[
              ["Hit TP3", "3.0×", "emerald"],
              ["Hit TP2", "2.0×", "emerald"],
              ["Hit TP1", "1.5×", "teal"],
              ["Direction correct", "1.0×", "slate"],
              ["Hit SL", "0.5×", "rose"],
            ].map(([label, val, color]) => (
              <div key={label} className="flex items-center justify-between py-0.5">
                <span className="text-slate-500">{label}</span>
                <span className={`font-mono font-bold text-${color}-600`}>{val}</span>
              </div>
            ))}
          </div>
          <div>
            <p className="font-semibold text-slate-600 mb-1">Confidence factor (B2):</p>
            <div className="flex items-center justify-between py-0.5">
              <span className="text-slate-500">Conf ≥ 70% AND wrong</span>
              <span className="font-mono font-bold text-amber-600">×2.0 (hard negative)</span>
            </div>
            <div className="flex items-center justify-between py-0.5">
              <span className="text-slate-500">Everything else</span>
              <span className="font-mono font-bold text-slate-500">×1.0</span>
            </div>
            <div className="mt-2 rounded-lg bg-amber-50 border border-amber-100 px-2 py-1.5 text-[11px] text-amber-700">
              <AlertTriangle className="inline h-3 w-3 mr-1" />
              Confident-and-wrong = highest-priority signal for learning
            </div>
          </div>
        </div>
      </div>

      {/* Bundle list */}
      {loading && bundles.length === 0 ? (
        <div className="flex h-32 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-slate-300" />
        </div>
      ) : bundles.length === 0 ? (
        <div className="flex h-32 flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-slate-200 text-slate-400">
          <Zap className="h-6 w-6" />
          <p className="text-sm">No bundles yet. Click <strong>Build Bundle</strong> to create one.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {bundles.map(bundle => (
            <BundleCard
              key={bundle.bundle_path}
              bundle={bundle}
              isSelected={selectedBundlePath === bundle.bundle_path}
              onSelect={onBundleSelect}
            />
          ))}
        </div>
      )}
    </div>
  );
}
