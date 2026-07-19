"use client";

/**
 * PortfolioInsightSection
 * ========================
 * Read-only advisory panel for the paper portfolio (Portfolio Insight & Advise).
 * Renders nothing unless the backend feature is enabled (GET /stats succeeds) —
 * a 404 (INSIGHT_ENABLED off, or no active portfolio) hides it with no flash.
 *
 * Three stacked zones:
 *   1. Live P(hit TP before SL) meter per open position — the signature element,
 *      color-graded and animating on each ~500 ms tick from the shared P&L
 *      stream (usePnLStream). De-emphasised when the value is stale (no live ML
 *      edge); "—" when undefined (missing TP1/SL).
 *   2. Risk stats — capital-at-risk, single-name & sector concentration
 *      (amber/rose near the 10%/25% single-name limits), correlation, stress.
 *   3. On-demand AI advice — assessment (typewriter), key risks, considerations,
 *      per-position notes, and the verbatim regulatory disclaimer.
 *
 * All figures come pre-computed from the backend; this component only formats.
 */

import { useMemo } from "react";
import {
  AlertTriangle,
  Gauge,
  Layers,
  Lightbulb,
  Radar,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  Waves,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useTypewriter } from "@/hooks/useTypewriter";
import { usePositions } from "@/hooks/usePaperTrading";
import { usePnLStream } from "@/contexts/PnLStreamContext";
import {
  useLivePositionPnL,
  usePortfolioAdvice,
  usePortfolioInsight,
} from "@/hooks/usePortfolioInsight";
import type { MutableRefObject } from "react";
import type { LivePositionPnL, PaperPosition } from "@/types/paper_trading";
import type { PortfolioInsightStats } from "@/types/portfolio_insight";

// ── Formatters ──────────────────────────────────────────────────────────────

const INR0 = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 0,
});

const pct = (v: number, digits = 1) => `${v.toFixed(digits)}%`;

// ── Colour scales ─────────────────────────────────────────────────────────────

/** P(hit TP) → green (likely) / amber (coin-flip) / rose (unlikely). */
function probTone(p: number): { bar: string; text: string } {
  if (p >= 0.6) return { bar: "bg-emerald-500", text: "text-emerald-600" };
  if (p >= 0.4) return { bar: "bg-amber-500", text: "text-amber-600" };
  return { bar: "bg-rose-500", text: "text-rose-600" };
}

/** Single-name weight bands: 10% warn, 25% high (industry convention). */
function nameConcentrationTone(weightPct: number): { bar: string; text: string } {
  if (weightPct >= 25) return { bar: "bg-rose-500", text: "text-rose-600" };
  if (weightPct >= 10) return { bar: "bg-amber-500", text: "text-amber-600" };
  return { bar: "bg-emerald-500", text: "text-emerald-600" };
}

/** Sector weight bands: 40% warn, 60% high. */
function sectorConcentrationTone(weightPct: number): { bar: string; text: string } {
  if (weightPct >= 60) return { bar: "bg-rose-500", text: "text-rose-600" };
  if (weightPct >= 40) return { bar: "bg-amber-500", text: "text-amber-600" };
  return { bar: "bg-slate-400", text: "text-slate-600" };
}

// ── Zone 1: live probability rows ─────────────────────────────────────────────

function LiveProbabilityRow({
  position,
  positionPnLMap,
}: {
  position: PaperPosition;
  positionPnLMap: MutableRefObject<Map<string, LivePositionPnL>>;
}) {
  const live = useLivePositionPnL(position.id, positionPnLMap);
  const p = live?.hit_probability;
  const stale = live?.hit_prob_stale === true;
  const hasValue = typeof p === "number";
  const tone = hasValue ? probTone(p as number) : null;
  const widthPct = hasValue ? Math.round((p as number) * 100) : 0;

  return (
    <div className={cn("flex items-center gap-3 py-1.5", stale && "opacity-60")}>
      <div className="w-24 shrink-0">
        <div className="truncate text-sm font-semibold text-slate-800">{position.symbol}</div>
        <div className="text-[10px] uppercase tracking-wide text-slate-400">
          {position.side === "LONG" ? "Long" : "Short"}
        </div>
      </div>

      {/* The signature element: a live barrier-hit probability meter. */}
      <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-slate-100">
        {hasValue && (
          <div
            className={cn("h-full rounded-full transition-[width] duration-500 ease-out", tone!.bar)}
            style={{ width: `${widthPct}%` }}
          />
        )}
      </div>

      <div className="flex w-20 shrink-0 items-center justify-end gap-1.5">
        {hasValue ? (
          <span className={cn("text-sm font-bold tabular-nums", tone!.text)}>{widthPct}%</span>
        ) : (
          <span className="text-sm text-slate-300">—</span>
        )}
        {stale && (
          <span
            className="rounded bg-slate-100 px-1 py-0.5 text-[9px] font-semibold uppercase text-slate-400"
            title="No live ML edge yet — showing the neutral distance-based estimate"
          >
            est
          </span>
        )}
      </div>
    </div>
  );
}

// ── Zone 2: risk stats ─────────────────────────────────────────────────────────

function StatTile({
  label,
  value,
  sub,
  valueClass,
}: {
  label: string;
  value: string;
  sub?: string;
  valueClass?: string;
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
      <div className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</div>
      <div className={cn("mt-0.5 text-base font-bold leading-tight text-slate-900", valueClass)}>{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-slate-400">{sub}</div>}
    </div>
  );
}

function ConcentrationBar({
  label,
  name,
  weightPct,
  tone,
}: {
  label: string;
  name: string;
  weightPct: number;
  tone: { bar: string; text: string };
}) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white px-3 py-2.5">
      <div className="flex items-baseline justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wide text-slate-400">{label}</span>
        <span className={cn("text-sm font-bold tabular-nums", tone.text)}>{pct(weightPct)}</span>
      </div>
      <div className="mt-1 truncate text-xs font-medium text-slate-700">{name}</div>
      <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-slate-100">
        <div
          className={cn("h-full rounded-full transition-[width] duration-500", tone.bar)}
          style={{ width: `${Math.min(100, Math.max(0, weightPct))}%` }}
        />
      </div>
    </div>
  );
}

function RiskStats({ stats }: { stats: PortfolioInsightStats }) {
  const car = stats.capital_at_risk;
  const sn = stats.single_name;
  const sec = stats.sector;
  const cor = stats.correlation;

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <StatTile
          label="Capital at risk"
          value={pct(car.capital_at_risk_pct)}
          sub={`${INR0.format(car.capital_at_risk)}${
            car.positions_without_stop > 0 ? ` · ${car.positions_without_stop} without a stop` : ""
          }`}
          valueClass={car.capital_at_risk_pct >= 5 ? "text-rose-600" : car.capital_at_risk_pct >= 2 ? "text-amber-600" : undefined}
        />
        <StatTile
          label="Effective names"
          value={sn.effective_positions.toFixed(1)}
          sub={`HHI ${sn.hhi.toFixed(3)}`}
        />
        <StatTile
          label="Correlation (max)"
          value={cor.max_pair_correlation != null ? cor.max_pair_correlation.toFixed(2) : "—"}
          sub={
            cor.max_pair
              ? `${cor.max_pair[0]} · ${cor.max_pair[1]}`
              : cor.excluded_positions > 0
                ? `${cor.excluded_positions} excluded (short history)`
                : "avg " + (cor.avg_pairwise_correlation?.toFixed(2) ?? "—")
          }
        />
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {sn.max_weight_symbol && (
          <ConcentrationBar
            label="Largest position"
            name={sn.max_weight_symbol}
            weightPct={sn.max_weight_pct}
            tone={nameConcentrationTone(sn.max_weight_pct)}
          />
        )}
        {sec.max_sector && (
          <ConcentrationBar
            label="Top sector"
            name={sec.max_sector}
            weightPct={sec.max_sector_weight_pct}
            tone={sectorConcentrationTone(sec.max_sector_weight_pct)}
          />
        )}
      </div>

      {stats.stress.scenarios.length > 0 && (
        <div>
          <div className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
            <Waves className="h-3 w-3" aria-hidden="true" />
            Stress scan
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            {stats.stress.scenarios.map((s) => (
              <div key={s.key} className="rounded-xl border border-slate-200 bg-white px-3 py-2.5" title={s.detail ?? undefined}>
                <div className="truncate text-[11px] text-slate-500">{s.label}</div>
                <div className={cn("mt-0.5 text-base font-bold tabular-nums", s.delta_pct < 0 ? "text-rose-600" : "text-emerald-600")}>
                  {s.delta_pct > 0 ? "+" : ""}
                  {pct(s.delta_pct)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {stats.notes.length > 0 && (
        <ul className="space-y-0.5 pt-0.5">
          {stats.notes.map((note, i) => (
            <li key={i} className="text-[11px] leading-snug text-slate-400">· {note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Zone 3: AI advice ───────────────────────────────────────────────────────────

function AdviceZone() {
  const { advice, isStale, isFetching, isError, fetchAdvice } = usePortfolioAdvice();
  const { revealedLength, isComplete } = useTypewriter(advice?.assessment ?? "");
  const typedAssessment = (advice?.assessment ?? "").slice(0, revealedLength);
  const revealRest = isComplete || !advice; // once the assessment finishes, reveal the details

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
          <Sparkles className="h-4 w-4 text-violet-500" aria-hidden="true" />
          AI advice
          {advice && isStale && (
            <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
              cached
            </span>
          )}
        </div>
        <button
          type="button"
          onClick={fetchAdvice}
          disabled={isFetching}
          className={cn(
            "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-semibold transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-violet-400 focus-visible:ring-offset-1",
            isFetching
              ? "cursor-not-allowed bg-slate-100 text-slate-400"
              : "border border-violet-200 bg-violet-50 text-violet-700 hover:bg-violet-100 active:bg-violet-200",
          )}
        >
          <RefreshCw className={cn("h-3.5 w-3.5", isFetching && "animate-spin")} aria-hidden="true" />
          {isFetching ? "Analyzing…" : advice ? "Refresh" : "Get advice"}
        </button>
      </div>

      {/* Prompt (no advice yet) */}
      {!advice && !isFetching && !isError && (
        <p className="text-sm text-slate-500">
          Generate an AI read on your portfolio&apos;s concentration, correlation, and risk — on demand.
        </p>
      )}

      {/* Loading skeleton */}
      {isFetching && !advice && (
        <div className="space-y-2" role="status" aria-busy="true" aria-label="Generating advice">
          {[90, 80, 60].map((w) => (
            <div key={w} className="h-3 rounded bg-slate-100" style={{ width: `${w}%` }} />
          ))}
        </div>
      )}

      {/* Error (non-404 — the section itself hides on feature-off) */}
      {isError && !advice && (
        <div className="flex items-center gap-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-500" role="status">
          <AlertTriangle className="h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />
          Couldn&apos;t generate advice right now. Try again in a moment.
        </div>
      )}

      {/* Advice */}
      {advice && (
        <div className="space-y-3">
          <p className="whitespace-pre-line text-sm leading-relaxed text-slate-700">{typedAssessment}</p>

          {revealRest && advice.key_risks.length > 0 && (
            <div>
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <ShieldAlert className="h-3.5 w-3.5" aria-hidden="true" />
                Key risks
              </div>
              <ul className="space-y-1">
                {advice.key_risks.map((risk, i) => (
                  <li key={i} className="flex gap-2 text-sm text-slate-600">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-rose-400" aria-hidden="true" />
                    {risk}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {revealRest && advice.considerations.length > 0 && (
            <div>
              <div className="mb-1 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
                <Lightbulb className="h-3.5 w-3.5" aria-hidden="true" />
                Considerations
              </div>
              <ul className="space-y-1">
                {advice.considerations.map((c, i) => (
                  <li key={i} className="flex gap-2 text-sm text-slate-600">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-violet-400" aria-hidden="true" />
                    {c}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {revealRest && advice.per_position.length > 0 && (
            <div className="space-y-1.5">
              {advice.per_position.map((note) => (
                <div key={note.symbol} className="rounded-lg border border-slate-100 bg-slate-50/70 px-3 py-2">
                  <span className="text-xs font-semibold text-slate-700">{note.symbol}</span>
                  <span className="ml-2 text-xs text-slate-500">{note.note}</span>
                </div>
              ))}
            </div>
          )}

          {/* Verbatim regulatory disclaimer (server-authored) — amber alert,
              styled to match the platform's risk warnings. Always shown. */}
          <div
            role="alert"
            className="flex items-start gap-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3"
          >
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
            <p className="text-xs leading-relaxed text-amber-700">{advice.disclaimer.trim()}</p>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Section ────────────────────────────────────────────────────────────────────

function ZoneHeading({ icon, title, hint }: { icon: React.ReactNode; title: string; hint?: string }) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
        {icon}
        {title}
      </div>
      {hint && <span className="text-[11px] text-slate-400">{hint}</span>}
    </div>
  );
}

export function PortfolioInsightSection() {
  const stats = usePortfolioInsight();
  const { positionPnLMap } = usePnLStream();
  const { data: positionsData } = usePositions(undefined, stats.isSuccess);

  const openPositions = useMemo(
    () => positionsData?.positions.filter((p) => p.status === "OPEN") ?? [],
    [positionsData],
  );

  // Hide entirely until the feature responds successfully — a 404 (INSIGHT_ENABLED
  // off, or no active portfolio) leaves the dashboard exactly as it was, no flash.
  if (!stats.isSuccess || !stats.data) return null;
  const data = stats.data;

  return (
    <section
      className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5"
      aria-label="Portfolio insight and advice"
    >
      {/* Header */}
      <div className="mb-4 flex items-center gap-2.5">
        <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-violet-600 shadow-sm">
          <Radar className="h-5 w-5 text-white" aria-hidden="true" />
        </div>
        <div>
          <h2 className="text-base font-semibold text-slate-900">Portfolio Insight</h2>
          <p className="text-xs text-slate-500">Live risk read on your book — you decide what to act on.</p>
        </div>
      </div>

      {data.open_position_count === 0 ? (
        <p className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-4 py-6 text-center text-sm text-slate-400">
          No open positions to analyze yet. Insight appears once you hold a position.
        </p>
      ) : (
        <div className="space-y-5">
          {/* Zone 1 — live probability */}
          <div className="space-y-1">
            <ZoneHeading
              icon={<Gauge className="h-4 w-4 text-slate-400" aria-hidden="true" />}
              title="Reach probability"
              hint="P(take-profit before stop)"
            />
            <div className="divide-y divide-slate-50">
              {openPositions.map((position) => (
                <LiveProbabilityRow
                  key={position.id}
                  position={position}
                  positionPnLMap={positionPnLMap}
                />
              ))}
            </div>
          </div>

          {/* Zone 2 — risk stats */}
          <div className="space-y-2 border-t border-slate-100 pt-4">
            <ZoneHeading
              icon={<Layers className="h-4 w-4 text-slate-400" aria-hidden="true" />}
              title="Risk profile"
              hint={`${INR0.format(data.portfolio_value)} book`}
            />
            <RiskStats stats={data} />
          </div>

          {/* Zone 3 — AI advice */}
          <div className="border-t border-slate-100 pt-4">
            <AdviceZone />
          </div>
        </div>
      )}
    </section>
  );
}
