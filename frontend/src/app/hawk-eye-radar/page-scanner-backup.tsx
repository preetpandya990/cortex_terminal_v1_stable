/**
 * Cortex AI — HawkEye Radar Page
 * ================================
 * Multi-timeframe signal scanner UI.
 * Fully typed — replaces useState<any>(null) + JSON.stringify.
 *
 * Features:
 *  - Timeframe filter toggles
 *  - Composite signal badges (buy/sell/neutral) with strength indicator
 *  - Per-instrument timeframe breakdown on expand
 *  - Loading skeleton, error state, empty state
 *  - Auto-refresh every 60s
 */
"use client";

import { useCallback, useEffect, useState } from "react";
import { useAuthFetch } from "@/hooks/useAuthFetch";
import type { HawkEyeResponse, HawkEyeResult, HawkEyeSignal, SignalDirection } from "@/types/hawk_eye";

const ALL_TIMEFRAMES = ["1w", "1d", "4h", "1h", "15m"] as const;

// ── Signal badge ───────────────────────────────────────────────────────────────
function SignalBadge({ signal, size = "sm" }: { signal: SignalDirection; size?: "sm" | "md" }) {
  const styles: Record<SignalDirection, string> = {
    buy: "bg-emerald-900/40 text-emerald-400 border border-emerald-800",
    sell: "bg-red-900/40 text-red-400 border border-red-800",
    neutral: "bg-gray-800 text-gray-400 border border-gray-700",
  };
  const sizeClass = size === "md" ? "px-3 py-1 text-sm font-semibold" : "px-2 py-0.5 text-xs font-medium";
  return (
    <span className={`inline-block rounded ${sizeClass} uppercase tracking-wide ${styles[signal]}`}>
      {signal}
    </span>
  );
}

// ── Strength indicator ─────────────────────────────────────────────────────────
function StrengthDots({ strength }: { strength: "strong" | "moderate" | "weak" }) {
  const filled = strength === "strong" ? 3 : strength === "moderate" ? 2 : 1;
  return (
    <span className="flex items-center gap-0.5" title={strength}>
      {[1, 2, 3].map((i) => (
        <span
          key={i}
          className={`h-2 w-2 rounded-full ${
            i <= filled ? "bg-amber-400" : "bg-gray-700"
          }`}
        />
      ))}
    </span>
  );
}

// ── Timeframe signal row ───────────────────────────────────────────────────────
function TimeframeSignalRow({ signal }: { signal: HawkEyeSignal }) {
  return (
    <div className="flex items-center justify-between py-1 text-xs text-gray-400">
      <span className="font-mono w-10 shrink-0 text-gray-500">{signal.timeframe}</span>
      <span className="flex-1 px-3 truncate">{signal.name.replace(/_/g, " ")}</span>
      <SignalBadge signal={signal.direction} />
      <span className="ml-3 font-mono text-gray-500 w-16 text-right">
        {Math.abs(signal.value).toFixed(2)}
      </span>
    </div>
  );
}

// ── Instrument card ────────────────────────────────────────────────────────────
function InstrumentCard({ result }: { result: HawkEyeResult }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-lg border border-gray-800 bg-gray-900 hover:border-gray-700 transition-colors">
      <button
        onClick={() => setExpanded((p) => !p)}
        className="w-full text-left px-4 py-3 flex items-center gap-3"
        aria-expanded={expanded}
      >
        {/* Symbol */}
        <div className="flex-1 min-w-0">
          <p className="font-semibold text-sm text-white truncate">{result.trading_symbol}</p>
          <p className="text-xs text-gray-500">{result.exchange}</p>
        </div>

        {/* Price */}
        <span className="font-mono text-sm text-gray-200 tabular-nums">
          ₹{result.last_price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}
        </span>

        {/* Score */}
        <span
          className={`font-mono text-sm tabular-nums w-16 text-right ${
            result.composite_score > 0
              ? "text-emerald-400"
              : result.composite_score < 0
              ? "text-red-400"
              : "text-gray-400"
          }`}
        >
          {result.composite_score > 0 ? "+" : ""}
          {result.composite_score.toFixed(1)}
        </span>

        {/* Signal + Strength */}
        <div className="flex items-center gap-2 shrink-0">
          <SignalBadge signal={result.composite_signal} size="md" />
          <StrengthDots strength={result.strength} />
        </div>

        {/* Agreement */}
        <span className="text-xs text-gray-500 w-12 text-right shrink-0">
          {result.agreement_pct.toFixed(0)}%
        </span>

        {/* Chevron */}
        <svg
          className={`h-4 w-4 text-gray-600 shrink-0 transition-transform ${expanded ? "rotate-180" : ""}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {/* Expanded timeframe breakdown */}
      {expanded && (
        <div className="border-t border-gray-800 px-4 pb-3 pt-2">
          <p className="text-xs font-medium text-gray-500 mb-2 uppercase tracking-wider">
            Timeframe signals
          </p>
          {result.timeframe_signals.length === 0 ? (
            <p className="text-xs text-gray-600">No signals on any timeframe.</p>
          ) : (
            result.timeframe_signals.map((sig, i) => (
              <TimeframeSignalRow key={`${sig.timeframe}-${sig.name}-${i}`} signal={sig} />
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Loading skeleton ───────────────────────────────────────────────────────────
function Skeleton() {
  return (
    <div className="space-y-2">
      {Array.from({ length: 8 }).map((_, i) => (
        <div
          key={i}
          className="h-14 rounded-lg bg-gray-800 animate-pulse"
          style={{ opacity: 1 - i * 0.08 }}
        />
      ))}
    </div>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────────
export default function HawkEyeRadarPage() {
  const authFetch = useAuthFetch(); // Use authenticated fetch
  const [data, setData] = useState<HawkEyeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedTimeframes, setSelectedTimeframes] = useState<Set<string>>(
    new Set(ALL_TIMEFRAMES)
  );
  const [signalFilter, setSignalFilter] = useState<SignalDirection | "all">("all");
  const [strengthFilter, setStrengthFilter] = useState<"all" | "strong" | "moderate" | "weak">("all");

  const fetchData = useCallback(
    async (forceRefresh = false) => {
      setLoading(true);
      setError(null);
      try {
        const params = new URLSearchParams();
        selectedTimeframes.forEach((tf) => params.append("timeframes", tf));
        if (forceRefresh) params.set("force_refresh", "true");

        const res = await authFetch(`/api/v1/hawk-eye/scan?${params.toString()}`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({ error: { message: "Unknown error" } }));
          throw new Error(err?.error?.message ?? `HTTP ${res.status}`);
        }
        const json: HawkEyeResponse = await res.json();
        setData(json);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load HawkEye data");
      } finally {
        setLoading(false);
      }
    },
    [selectedTimeframes, authFetch]
  );

  // Initial load + 60s auto-refresh
  useEffect(() => {
    fetchData();
    const interval = setInterval(() => fetchData(), 60_000);
    return () => clearInterval(interval);
  }, [fetchData]);

  const toggleTimeframe = (tf: string) => {
    setSelectedTimeframes((prev) => {
      const next = new Set(prev);
      if (next.has(tf)) {
        if (next.size === 1) return prev; // always keep at least one
        next.delete(tf);
      } else {
        next.add(tf);
      }
      return next;
    });
  };

  // Apply filters
  const filteredResults =
    data?.results.filter((r) => {
      if (signalFilter !== "all" && r.composite_signal !== signalFilter) return false;
      if (strengthFilter !== "all" && r.strength !== strengthFilter) return false;
      return true;
    }) ?? [];

  return (
    <main className="min-h-screen bg-gray-950 text-white">
      <div className="mx-auto max-w-6xl px-4 py-8">
        {/* Header */}
        <div className="mb-8 flex items-start justify-between">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">HawkEye Radar</h1>
            <p className="mt-1 text-sm text-gray-400">
              Multi-timeframe signal scanner
              {data && (
                <span className="ml-2 text-gray-600">
                  — {data.total} instruments · refreshed{" "}
                  {new Date(data.scanned_at).toLocaleTimeString("en-IN", {
                    timeZone: "Asia/Kolkata",
                  })}{" "}
                  IST
                </span>
              )}
            </p>
          </div>
          <button
            onClick={() => fetchData(true)}
            disabled={loading}
            className="flex items-center gap-2 rounded-md border border-gray-700 bg-gray-800 px-3 py-2 text-sm text-gray-300 hover:bg-gray-700 disabled:opacity-50 transition-colors"
          >
            <svg
              className={`h-4 w-4 ${loading ? "animate-spin" : ""}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              aria-hidden="true"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
              />
            </svg>
            Refresh
          </button>
        </div>

        {/* Timeframe toggles */}
        <div className="mb-4 flex flex-wrap gap-2">
          <span className="self-center text-xs text-gray-500 mr-1">Timeframes:</span>
          {ALL_TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => toggleTimeframe(tf)}
              className={`rounded px-3 py-1 text-xs font-medium transition-colors border ${
                selectedTimeframes.has(tf)
                  ? "border-indigo-600 bg-indigo-900/40 text-indigo-300"
                  : "border-gray-700 bg-gray-900 text-gray-500 hover:border-gray-600"
              }`}
            >
              {tf}
            </button>
          ))}
        </div>

        {/* Signal & strength filters */}
        <div className="mb-6 flex flex-wrap gap-3">
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Signal:</span>
            {(["all", "buy", "sell", "neutral"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setSignalFilter(f)}
                className={`rounded px-2.5 py-1 text-xs transition-colors ${
                  signalFilter === f
                    ? "bg-gray-700 text-white"
                    : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {f}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Strength:</span>
            {(["all", "strong", "moderate", "weak"] as const).map((f) => (
              <button
                key={f}
                onClick={() => setStrengthFilter(f)}
                className={`rounded px-2.5 py-1 text-xs transition-colors ${
                  strengthFilter === f
                    ? "bg-gray-700 text-white"
                    : "text-gray-500 hover:text-gray-300"
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>

        {/* Summary counts */}
        {data && !loading && (
          <div className="mb-6 grid grid-cols-3 gap-3">
            {(["buy", "sell", "neutral"] as const).map((s) => {
              const count = data.results.filter((r) => r.composite_signal === s).length;
              return (
                <div
                  key={s}
                  className="rounded-lg border border-gray-800 bg-gray-900 px-4 py-3 text-center cursor-pointer hover:border-gray-700"
                  onClick={() => setSignalFilter(signalFilter === s ? "all" : s)}
                >
                  <p className="text-2xl font-semibold tabular-nums">{count}</p>
                  <p className="mt-0.5 text-xs uppercase tracking-wider text-gray-500">{s}</p>
                </div>
              );
            })}
          </div>
        )}

        {/* Table header */}
        {!loading && !error && filteredResults.length > 0 && (
          <div className="mb-1 flex items-center gap-3 px-4 text-xs font-medium uppercase tracking-wider text-gray-600">
            <span className="flex-1">Symbol</span>
            <span className="font-mono w-24 text-right">Price</span>
            <span className="font-mono w-16 text-right">Score</span>
            <span className="w-20 text-center">Signal</span>
            <span className="w-16 text-right">Agree %</span>
            <span className="w-4" />
          </div>
        )}

        {/* Content */}
        {loading && <Skeleton />}

        {!loading && error && (
          <div
            className="rounded-lg border border-red-900 bg-red-950/30 px-4 py-6 text-center"
            role="alert"
          >
            <p className="text-sm text-red-400">{error}</p>
            <button
              onClick={() => fetchData()}
              className="mt-3 text-xs text-red-500 underline hover:text-red-400"
            >
              Try again
            </button>
          </div>
        )}

        {!loading && !error && filteredResults.length === 0 && (
          <div className="rounded-lg border border-gray-800 px-4 py-12 text-center">
            <p className="text-sm text-gray-500">
              {data?.total === 0
                ? "No data found. Ensure OHLCV data has been ingested."
                : "No results match the current filters."}
            </p>
          </div>
        )}

        {!loading && !error && filteredResults.length > 0 && (
          <div className="space-y-1.5">
            {filteredResults.map((result) => (
              <InstrumentCard key={result.instrument_key} result={result} />
            ))}
          </div>
        )}
      </div>
    </main>
  );
}
