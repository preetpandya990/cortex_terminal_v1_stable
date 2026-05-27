"use client";

import { useCallback, useRef, useState } from "react";
import { Cell, Pie, PieChart as RechartsPieChart, ResponsiveContainer, Tooltip } from "recharts";
import {
  AlertCircle,
  BarChart3,
  Building2,
  Calendar,
  Coins,
  Droplets,
  Landmark,
  LineChart,
  Minus,
  PieChart,
  TrendingDown,
  TrendingUp,
  Trophy,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useFundamentals } from "@/hooks/useFundamentals";
import type {
  BalanceSheetEntry,
  CategoryHistory,
  CompetitorEntry,
  CorporateAction,
  FundamentalsFullResponse,
  IncomeStatementData,
  KeyRatioItem,
  ShareHoldingEntry,
} from "@/types/fundamentals";

// ─── Types ────────────────────────────────────────────────────────────────────

type FundamentalsSection =
  | "profile"
  | "key_ratios"
  | "income"
  | "balance_sheet"
  | "cash_flow"
  | "holdings"
  | "corporate_actions"
  | "competitors";

interface SectionDef {
  id: FundamentalsSection;
  label: string;
  Icon: React.ElementType;
  active: string;
  inactive: string;
  iconActive: string;
  iconInactive: string;
  countActive: string;
  countInactive: string;
  badge?: (data: FundamentalsFullResponse) => number | undefined;
}

// ─── Section config ───────────────────────────────────────────────────────────

const SECTIONS: SectionDef[] = [
  {
    id: "profile",
    label: "Profile",
    Icon: Building2,
    active:       "border-blue-500 bg-gradient-to-br from-blue-500 to-blue-600 text-white shadow-md shadow-blue-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-blue-300 hover:bg-blue-50/60 hover:text-blue-700",
    iconActive:   "text-blue-100",
    iconInactive: "text-blue-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-blue-50 text-blue-600 border border-blue-200",
  },
  {
    id: "key_ratios",
    label: "Key Ratios",
    Icon: LineChart,
    active:       "border-indigo-500 bg-gradient-to-br from-indigo-500 to-indigo-600 text-white shadow-md shadow-indigo-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-indigo-300 hover:bg-indigo-50/60 hover:text-indigo-700",
    iconActive:   "text-indigo-100",
    iconInactive: "text-indigo-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-indigo-50 text-indigo-600 border border-indigo-200",
  },
  {
    id: "income",
    label: "Income",
    Icon: TrendingUp,
    active:       "border-emerald-500 bg-gradient-to-br from-emerald-500 to-emerald-600 text-white shadow-md shadow-emerald-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-emerald-300 hover:bg-emerald-50/60 hover:text-emerald-700",
    iconActive:   "text-emerald-100",
    iconInactive: "text-emerald-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-emerald-50 text-emerald-600 border border-emerald-200",
  },
  {
    id: "balance_sheet",
    label: "Balance Sheet",
    Icon: Landmark,
    active:       "border-slate-600 bg-gradient-to-br from-slate-600 to-slate-700 text-white shadow-md shadow-slate-300",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-slate-400 hover:bg-slate-50 hover:text-slate-800",
    iconActive:   "text-slate-200",
    iconInactive: "text-slate-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-slate-100 text-slate-600 border border-slate-200",
  },
  {
    id: "cash_flow",
    label: "Cash Flow",
    Icon: Droplets,
    active:       "border-cyan-500 bg-gradient-to-br from-cyan-500 to-cyan-600 text-white shadow-md shadow-cyan-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-cyan-300 hover:bg-cyan-50/60 hover:text-cyan-700",
    iconActive:   "text-cyan-100",
    iconInactive: "text-cyan-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-cyan-50 text-cyan-600 border border-cyan-200",
  },
  {
    id: "holdings",
    label: "Holdings",
    Icon: PieChart,
    active:       "border-violet-500 bg-gradient-to-br from-violet-500 to-violet-600 text-white shadow-md shadow-violet-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-violet-300 hover:bg-violet-50/60 hover:text-violet-700",
    iconActive:   "text-violet-100",
    iconInactive: "text-violet-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-violet-50 text-violet-600 border border-violet-200",
  },
  {
    id: "corporate_actions",
    label: "Corp Actions",
    Icon: Calendar,
    active:       "border-amber-500 bg-gradient-to-br from-amber-500 to-orange-500 text-white shadow-md shadow-amber-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-amber-300 hover:bg-amber-50/60 hover:text-amber-700",
    iconActive:   "text-amber-100",
    iconInactive: "text-amber-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-amber-50 text-amber-600 border border-amber-200",
    badge: (data) => data.corporate_actions?.length || undefined,
  },
  {
    id: "competitors",
    label: "Competitors",
    Icon: Trophy,
    active:       "border-orange-500 bg-gradient-to-br from-orange-500 to-orange-600 text-white shadow-md shadow-orange-200",
    inactive:     "border-slate-200 bg-white text-slate-600 hover:border-orange-300 hover:bg-orange-50/60 hover:text-orange-700",
    iconActive:   "text-orange-100",
    iconInactive: "text-orange-500",
    countActive:  "bg-white/20 text-white",
    countInactive:"bg-orange-50 text-orange-600 border border-orange-200",
    badge: (data) => data.competitors?.length || undefined,
  },
];

// ─── Formatters ───────────────────────────────────────────────────────────────

function formatCrore(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return "—";
  const abs = Math.abs(val);
  const sign = val < 0 ? "-" : "";
  if (abs >= 100_000) return `${sign}${(abs / 100_000).toFixed(2)} L Cr`;
  if (abs >= 1_000)
    return `${sign}${abs.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
  return `${sign}${abs.toFixed(2)} Cr`;
}

function formatRatio(val: number | null | undefined, pct = false): string {
  if (val == null || !Number.isFinite(val)) return "—";
  return pct ? `${val.toFixed(2)}%` : val.toFixed(2);
}

function formatChangePct(
  val: number | null | undefined,
): { text: string; positive: boolean } | null {
  if (val == null || !Number.isFinite(val)) return null;
  return { text: `${val >= 0 ? "+" : ""}${val.toFixed(2)}%`, positive: val >= 0 };
}

function formatMcap(
  val: number | null | undefined,
  unit: string | null | undefined,
): string {
  if (val == null) return "—";
  return `${val.toFixed(2)} ${unit ?? ""}`.trim();
}

function formatActionDate(isoDate: string | null | undefined, fallback: string | null | undefined): string {
  if (isoDate) {
    try {
      return new Date(isoDate).toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric",
      });
    } catch {
      // fall through to raw string
    }
  }
  return fallback ?? "—";
}

// ─── Pivot helper ─────────────────────────────────────────────────────────────

function pivotCategoryHistory(data: CategoryHistory[], maxPeriods = 4) {
  const periodSet = new Set<string>();
  const byCategory: Record<string, Record<string, { value: number; change_pct: number | null }>> = {};

  for (const cat of data) {
    byCategory[cat.category] = {};
    for (const entry of cat.history) {
      periodSet.add(entry.period_date);
      byCategory[cat.category][entry.period_date] = {
        value: entry.value,
        change_pct: entry.change_pct ?? null,
      };
    }
  }

  const periods = Array.from(periodSet).sort().slice(-maxPeriods);
  return { periods, byCategory };
}

function bsPeriodLabel(entry: BalanceSheetEntry): string {
  return entry.period;
}

// ─── Shared: Empty state ──────────────────────────────────────────────────────

function EmptyState({ message }: { message: string }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-200 bg-slate-50/60 px-6 py-10 text-center">
      <p className="text-sm font-medium text-slate-500">{message}</p>
    </div>
  );
}

// ─── Section: Company Profile ─────────────────────────────────────────────────

function ProfileSection({
  profile,
}: {
  profile: FundamentalsFullResponse["profile"];
}) {
  const [expanded, setExpanded] = useState(false);

  if (!profile) return <EmptyState message="No profile data available" />;

  const description = profile.company_profile ?? "";
  const isLong = description.length > 280;
  const displayText =
    isLong && !expanded ? description.slice(0, 280).trimEnd() + "…" : description;

  return (
    <div className="space-y-4">
      {description && (
        <div>
          <p className="text-sm text-slate-700 leading-relaxed">{displayText}</p>
          {isLong && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 text-xs font-semibold text-slate-500 hover:text-slate-700 transition-colors"
            >
              {expanded ? "Show less" : "Show more"}
            </button>
          )}
        </div>
      )}

      <div className="flex flex-wrap gap-2">
        {profile.sector && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-xs font-semibold text-blue-700">
            <Building2 className="h-3 w-3" />
            {profile.sector}
          </span>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3">
        {profile.sector_mcap_inr != null && (
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-xs text-slate-500 mb-1">Sector MCap (INR)</p>
            <p className="text-sm font-bold text-slate-900">
              ₹{formatCrore(profile.sector_mcap_inr)}
            </p>
          </div>
        )}
        {profile.sector_mcap_usd != null && (
          <div className="rounded-lg border border-slate-200 bg-white p-3">
            <p className="text-xs text-slate-500 mb-1">Sector MCap (USD)</p>
            <p className="text-sm font-bold text-slate-900">
              ${formatMcap(profile.sector_mcap_usd, profile.sector_mcap_usd_unit)}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Section: Key Ratios ──────────────────────────────────────────────────────

const RATIO_META: Record<string, { label: string; higherBetter: boolean; pct?: boolean }> = {
  "P/E":       { label: "P/E Ratio",  higherBetter: false },
  "P/B":       { label: "P/B Ratio",  higherBetter: false },
  "ROA":       { label: "ROA",        higherBetter: true,  pct: true },
  "ROE":       { label: "ROE",        higherBetter: true,  pct: true },
  "ROCE":      { label: "ROCE",       higherBetter: true,  pct: true },
  "EV/EBITDA": { label: "EV/EBITDA",  higherBetter: false },
};

function RatioDelta({
  company,
  sector,
  higherBetter,
}: {
  company: number | null;
  sector: number | null;
  higherBetter: boolean;
}) {
  if (company == null || sector == null) return null;
  const isGood = higherBetter ? company >= sector : company <= sector;
  const Icon = isGood ? TrendingUp : TrendingDown;
  return (
    <span className={cn("inline-flex items-center gap-0.5", isGood ? "text-emerald-600" : "text-red-500")}>
      <Icon className="h-3.5 w-3.5" />
    </span>
  );
}

function KeyRatiosSection({ keyRatios }: { keyRatios: KeyRatioItem[] }) {
  if (!keyRatios.length) return <EmptyState message="No ratio data available" />;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {keyRatios.map((ratio) => {
        const meta = RATIO_META[ratio.name] ?? { label: ratio.name, higherBetter: true };
        const company = ratio.company_value;
        const sector = ratio.sector_value;
        const isGood =
          company != null && sector != null
            ? meta.higherBetter
              ? company >= sector
              : company <= sector
            : null;

        return (
          <div
            key={ratio.name}
            className={cn(
              "rounded-lg border p-3 flex flex-col gap-1.5 transition-colors",
              isGood === true
                ? "border-emerald-200 bg-emerald-50/40"
                : isGood === false
                ? "border-red-200 bg-red-50/30"
                : "border-slate-200 bg-white",
            )}
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-slate-500">{meta.label}</span>
              <RatioDelta company={company} sector={sector} higherBetter={meta.higherBetter} />
            </div>
            <p className="text-lg font-bold text-slate-900 tabular-nums">
              {formatRatio(company, meta.pct)}
            </p>
            {sector != null && (
              <p className="text-xs text-slate-400 tabular-nums">
                Sector: {formatRatio(sector, meta.pct)}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Shared: Financial table ──────────────────────────────────────────────────

interface FinancialTableProps {
  periods: string[];
  rows: {
    label: string;
    values: (number | null | undefined)[];
    changes?: (number | null | undefined)[];
    alwaysPositive?: boolean;
  }[];
  periodLabels: string[];
}

function FinancialTable({ rows, periodLabels }: FinancialTableProps) {
  if (!periodLabels.length) return <EmptyState message="No data available" />;

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[380px] text-sm">
        <thead>
          <tr className="border-b border-slate-200">
            <th className="text-left py-2.5 px-3 text-xs font-semibold text-slate-500 w-36" />
            {periodLabels.map((label) => (
              <th
                key={label}
                className="text-right py-2.5 px-3 text-xs font-semibold text-slate-700 whitespace-nowrap"
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr
              key={row.label}
              className={cn(
                "border-b border-slate-100 last:border-0",
                ri % 2 === 0 ? "bg-white" : "bg-slate-50/60",
              )}
            >
              <td className="py-3 px-3 text-xs font-medium text-slate-600 whitespace-nowrap">
                {row.label}
              </td>
              {row.values.map((val, ci) => {
                const change = row.changes?.[ci];
                const changeFmt = formatChangePct(change);
                const isNegativeVal = typeof val === "number" && val < 0;
                return (
                  <td key={ci} className="py-3 px-3 text-right align-top">
                    <span
                      className={cn(
                        "text-xs font-semibold tabular-nums block",
                        !row.alwaysPositive && isNegativeVal
                          ? "text-red-600"
                          : "text-slate-900",
                      )}
                    >
                      {val != null ? formatCrore(val) : "—"}
                    </span>
                    {changeFmt && (
                      <span
                        className={cn(
                          "text-xs tabular-nums",
                          changeFmt.positive ? "text-emerald-600" : "text-red-500",
                        )}
                      >
                        {changeFmt.text}
                      </span>
                    )}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ─── Section: Income Statement ────────────────────────────────────────────────

const IS_CATEGORIES = [
  { key: "revenue",          label: "Revenue" },
  { key: "operating_profit", label: "Operating Profit" },
  { key: "net_profit",       label: "Net Profit" },
];

type IncomeView = "yearly" | "quarterly";

function IncomeStatementSection({ data }: { data: IncomeStatementData }) {
  const hasYearly    = data.yearly.length > 0;
  const hasQuarterly = data.quarterly.length > 0;
  const hasAnyData   = hasYearly || hasQuarterly;

  const [view, setView] = useState<IncomeView>(hasYearly ? "yearly" : "quarterly");

  const activeData = view === "yearly" ? data.yearly : data.quarterly;
  const maxPeriods = view === "yearly" ? 4 : 8;
  const { periods, byCategory } = pivotCategoryHistory(activeData, maxPeriods);

  const periodLabels = periods.map((p) => {
    for (const cat of activeData) {
      const entry = cat.history.find((h) => h.period_date === p);
      if (entry) return entry.period;
    }
    return p;
  });

  const rows = IS_CATEGORIES.map(({ key, label }) => ({
    label,
    values:  periods.map((p) => byCategory[key]?.[p]?.value   ?? null),
    changes: periods.map((p) => byCategory[key]?.[p]?.change_pct ?? null),
  }));

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <div className="inline-flex items-center rounded-lg bg-slate-100 p-0.5 gap-0.5">
          {(["yearly", "quarterly"] as IncomeView[]).map((v) => {
            const label      = v === "yearly" ? "Annual" : "Quarterly";
            const isActive   = view === v;
            const isDisabled = v === "quarterly" ? !hasQuarterly : !hasYearly;
            return (
              <button
                key={v}
                type="button"
                disabled={isDisabled}
                onClick={() => setView(v)}
                className={cn(
                  "rounded-md px-3 py-1 text-xs font-semibold transition-colors",
                  isActive
                    ? "bg-white text-slate-900 shadow-sm"
                    : isDisabled
                    ? "text-slate-300 cursor-not-allowed"
                    : "text-slate-500 hover:text-slate-700 cursor-pointer",
                )}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>

      {hasAnyData ? (
        activeData.length > 0 ? (
          <FinancialTable rows={rows} periods={periods} periodLabels={periodLabels} />
        ) : (
          <EmptyState message="No data for this period type yet." />
        )
      ) : (
        <div className="rounded-lg border border-slate-200 bg-slate-50 px-4 py-5 text-center">
          <p className="text-sm font-medium text-slate-600">Data not yet available</p>
          <p className="mt-1 text-xs text-slate-400">
            Income statement data is being populated. Check back soon.
          </p>
        </div>
      )}
    </div>
  );
}

// ─── Section: Balance Sheet ───────────────────────────────────────────────────

function BalanceSheetSection({ data }: { data: BalanceSheetEntry[] }) {
  const trimmed      = data.slice(-4);
  const periodLabels = trimmed.map(bsPeriodLabel);

  const rows = [
    { label: "Total Assets",      values: trimmed.map((e) => e.total_asset),     alwaysPositive: true },
    { label: "Total Liabilities", values: trimmed.map((e) => e.total_liability), alwaysPositive: true },
    { label: "Net Worth",         values: trimmed.map((e) => e.net_worth) },
  ];

  return (
    <FinancialTable
      rows={rows}
      periods={trimmed.map((e) => e.period_date)}
      periodLabels={periodLabels}
    />
  );
}

// ─── Section: Cash Flow ───────────────────────────────────────────────────────

const CF_CATEGORIES = [
  { key: "operating", label: "Operating CF" },
  { key: "investing", label: "Investing CF" },
  { key: "financing", label: "Financing CF" },
];

function CashFlowSection({ data }: { data: CategoryHistory[] }) {
  const { periods, byCategory } = pivotCategoryHistory(data);

  const periodLabels = periods.map((p) => {
    for (const cat of data) {
      const entry = cat.history.find((h) => h.period_date === p);
      if (entry) return entry.period;
    }
    return p;
  });

  const rows = CF_CATEGORIES.map(({ key, label }) => ({
    label,
    values:  periods.map((p) => byCategory[key]?.[p]?.value      ?? null),
    changes: periods.map((p) => byCategory[key]?.[p]?.change_pct ?? null),
  }));

  return <FinancialTable rows={rows} periods={periods} periodLabels={periodLabels} />;
}

// ─── Section: Share Holdings ──────────────────────────────────────────────────

const HOLDING_SEGMENTS: {
  key: keyof Pick<
    ShareHoldingEntry,
    "promoters_pct" | "fii_pct" | "mutual_funds_pct" | "other_dii_pct" | "retail_and_other_pct"
  >;
  label: string;
  color: string;
}[] = [
  { key: "promoters_pct",        label: "Promoters",      color: "#3b82f6" },
  { key: "fii_pct",              label: "FII",            color: "#8b5cf6" },
  { key: "mutual_funds_pct",     label: "Mutual Funds",   color: "#f59e0b" },
  { key: "other_dii_pct",        label: "Other DII",      color: "#10b981" },
  { key: "retail_and_other_pct", label: "Retail & Other", color: "#94a3b8" },
];

// Tooltip payload shape coming from Recharts — name/value/color are on the
// nested `payload` object because we pass the full data record to the Pie.
interface HoldingTooltipProps {
  active?: boolean;
  payload?: Array<{ payload: { name: string; value: number; color: string } }>;
}

function HoldingTooltip({ active, payload }: HoldingTooltipProps) {
  if (!active || !payload?.length) return null;
  const { name, value, color } = payload[0].payload;
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2 shadow-lg">
      <div className="flex items-center gap-2">
        <span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: color }} />
        <span className="text-xs font-semibold text-slate-900">{name}</span>
      </div>
      <p className="mt-1 text-xs tabular-nums text-slate-500">{value.toFixed(2)}%</p>
    </div>
  );
}

function ShareHoldingsSection({ data }: { data: ShareHoldingEntry[] }) {
  const latest = data[data.length - 1];
  const prior  = data.length >= 2 ? data[data.length - 2] : null;

  if (!latest) return <EmptyState message="No shareholding data available" />;

  // Build pie slices — filter zero/null values so they don't create invisible slivers.
  const pieData = HOLDING_SEGMENTS
    .map(({ key, label, color }) => ({
      name:  label,
      value: latest[key] ?? 0,
      color,
    }))
    .filter((d) => d.value > 0);

  return (
    <div className="space-y-4">
      {/* ── Donut chart ──────────────────────────────────────────────────── */}
      <div className="relative">
        <ResponsiveContainer width="100%" height={220}>
          <RechartsPieChart>
            <Pie
              data={pieData}
              dataKey="value"
              nameKey="name"
              cx="50%"
              cy="50%"
              innerRadius="54%"
              outerRadius="78%"
              paddingAngle={2}
              animationBegin={0}
              animationDuration={600}
            >
              {pieData.map((entry, i) => (
                <Cell
                  key={`cell-${i}`}
                  fill={entry.color}
                  stroke="white"
                  strokeWidth={2}
                />
              ))}
            </Pie>
            <Tooltip
              content={<HoldingTooltip />}
              isAnimationActive={false}
            />
          </RechartsPieChart>
        </ResponsiveContainer>

        {/* Center label — absolutely positioned so it scales with ResponsiveContainer */}
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <div className="text-center">
            <p className="text-sm font-bold text-slate-900">{latest.period}</p>
            <p className="text-xs text-slate-400">Latest Quarter</p>
          </div>
        </div>
      </div>

      {/* ── Breakdown legend with QoQ deltas ─────────────────────────────── */}
      <div className="grid grid-cols-2 gap-x-6 gap-y-2">
        {HOLDING_SEGMENTS.map(({ key, label, color }) => {
          const val      = latest[key];
          const priorVal = prior?.[key] ?? null;
          const delta    = val != null && priorVal != null ? val - priorVal : null;
          return (
            <div key={key} className="flex items-center gap-2">
              <span className="h-2 w-2 rounded-full shrink-0" style={{ backgroundColor: color }} />
              <span className="text-xs text-slate-600 flex-1 truncate">{label}</span>
              <span className="text-xs font-semibold tabular-nums text-slate-900">
                {val != null ? `${val.toFixed(2)}%` : "—"}
              </span>
              {delta != null && (
                <span
                  className={cn(
                    "text-xs tabular-nums",
                    delta > 0 ? "text-emerald-600" : delta < 0 ? "text-red-500" : "text-slate-400",
                  )}
                >
                  {delta > 0 ? "+" : ""}{delta.toFixed(2)}%
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Section: Corporate Actions ───────────────────────────────────────────────

const ACTION_ICONS: Record<string, React.ReactNode> = {
  Dividend: <Coins    className="h-3.5 w-3.5 text-amber-600"   />,
  Bonus:    <BarChart3 className="h-3.5 w-3.5 text-emerald-600" />,
  Split:    <TrendingUp className="h-3.5 w-3.5 text-blue-600"  />,
};

function CorporateActionsSection({ data }: { data: CorporateAction[] }) {
  if (!data.length) return <EmptyState message="No corporate actions found" />;

  return (
    <div className="divide-y divide-slate-100">
      {data.map((action, i) => (
        <div key={i} className="flex items-start gap-3 py-3 first:pt-0 last:pb-0">
          <div className="mt-0.5 h-6 w-6 rounded-full border border-slate-200 bg-slate-50 flex items-center justify-center shrink-0">
            {ACTION_ICONS[action.action_type] ?? (
              <Calendar className="h-3.5 w-3.5 text-slate-500" />
            )}
          </div>
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-sm font-semibold text-slate-900">{action.action_type}</span>
              {action.amount != null && (
                <span className="rounded bg-amber-50 border border-amber-200 px-1.5 py-0.5 text-xs font-semibold text-amber-700">
                  ₹{action.amount}
                </span>
              )}
              {action.ratio && (
                <span className="rounded bg-blue-50 border border-blue-200 px-1.5 py-0.5 text-xs font-medium text-blue-700">
                  {action.ratio}
                </span>
              )}
            </div>
            <p className="mt-0.5 text-xs text-slate-500">
              Ex-Date: {formatActionDate(action.expiry_date, action.expiry_date_str)}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Section: Competitors ─────────────────────────────────────────────────────

function CompetitorsSection({ data }: { data: CompetitorEntry[] }) {
  if (!data.length) return <EmptyState message="No competitor data available" />;

  return (
    <div className="space-y-2">
      {data.map((comp, i) => (
        <div
          key={i}
          className="flex items-start gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3"
        >
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-center gap-2 mb-0.5">
              {comp.trading_symbol ? (
                <span className="font-mono text-sm font-bold text-slate-900">
                  {comp.trading_symbol}
                </span>
              ) : (
                <span className="font-mono text-xs font-semibold text-slate-500">
                  {comp.instrument_key.split("|")[1] ?? comp.instrument_key}
                </span>
              )}
              {comp.sector && (
                <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-xs font-medium text-blue-600 truncate max-w-[160px]">
                  {comp.sector}
                </span>
              )}
            </div>
            {comp.name && (
              <p className="text-xs text-slate-600 truncate">{comp.name}</p>
            )}
          </div>
          <div className="shrink-0 text-right">
            {comp.mcap_inr_crore != null && (
              <p className="text-xs font-bold text-slate-900">
                ₹{formatCrore(comp.mcap_inr_crore)}
              </p>
            )}
            {comp.mcap_usd != null && (
              <p className="text-xs text-slate-400">
                ${formatMcap(comp.mcap_usd, comp.mcap_usd_unit)}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Skeleton ─────────────────────────────────────────────────────────────────

function FundamentalsTabSkeleton() {
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap gap-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-24 rounded-xl" />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-20 rounded-lg" />
        ))}
      </div>
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

interface FundamentalsTabProps {
  instrumentKey: string;
}

export function FundamentalsTab({ instrumentKey }: FundamentalsTabProps) {
  const { data, isLoading, isError } = useFundamentals(instrumentKey);
  const [activeSection, setActiveSection] = useState<FundamentalsSection>("key_ratios");

  // Roving tabindex: only the active tab button is in the natural tab order.
  // Arrow keys navigate within the tablist per WAI-ARIA Tab Pattern.
  const tabRefs = useRef<Map<FundamentalsSection, HTMLButtonElement>>(new Map());

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLButtonElement>) => {
      const ids = SECTIONS.map((s) => s.id);
      const currentIndex = ids.indexOf(activeSection);
      let nextIndex = -1;

      if (e.key === "ArrowRight") {
        e.preventDefault();
        nextIndex = (currentIndex + 1) % ids.length;
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        nextIndex = (currentIndex - 1 + ids.length) % ids.length;
      } else if (e.key === "Home") {
        e.preventDefault();
        nextIndex = 0;
      } else if (e.key === "End") {
        e.preventDefault();
        nextIndex = ids.length - 1;
      }

      if (nextIndex >= 0) {
        const nextId = ids[nextIndex];
        setActiveSection(nextId);
        tabRefs.current.get(nextId)?.focus();
      }
    },
    [activeSection],
  );

  if (isLoading) return <FundamentalsTabSkeleton />;

  if (isError) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-700">
        <AlertCircle className="h-4 w-4 shrink-0" />
        <span>Failed to load fundamentals data. Please try again later.</span>
      </div>
    );
  }

  if (!data) return null;

  if (!data.available) {
    return (
      <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 p-4">
        <Minus className="h-4 w-4 text-slate-400 shrink-0" />
        <div>
          <p className="text-sm font-medium text-slate-700">Fundamentals not available</p>
          <p className="text-xs text-slate-500 mt-0.5">
            {data.reason === "derivatives_not_supported"
              ? "Fundamental data is not available for derivatives (futures, options, indices)."
              : "This instrument does not have fundamental data."}
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ── Tab strip ─────────────────────────────────────────────────────── */}
      <div
        role="tablist"
        aria-label="Fundamentals sections"
        className="flex flex-wrap gap-2"
      >
        {SECTIONS.map((section) => {
          const isActive   = activeSection === section.id;
          const badgeCount = section.badge?.(data);
          return (
            <button
              key={section.id}
              ref={(el) => {
                if (el) tabRefs.current.set(section.id, el);
                else    tabRefs.current.delete(section.id);
              }}
              role="tab"
              type="button"
              id={`fund-tab-${section.id}`}
              aria-selected={isActive}
              aria-controls={`fund-panel-${section.id}`}
              tabIndex={isActive ? 0 : -1}
              onClick={() => setActiveSection(section.id)}
              onKeyDown={handleKeyDown}
              className={cn(
                "inline-flex items-center gap-2 rounded-xl border px-4 py-2.5 text-sm font-semibold",
                "transition-all duration-200",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2",
                isActive ? section.active : section.inactive,
              )}
            >
              <section.Icon
                className={cn("h-4 w-4", isActive ? section.iconActive : section.iconInactive)}
              />
              {section.label}
              {badgeCount != null && (
                <span
                  className={cn(
                    "rounded-md px-1.5 py-0.5 tabular-nums text-xs font-bold",
                    isActive ? section.countActive : section.countInactive,
                  )}
                >
                  {badgeCount}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* ── Content panel ─────────────────────────────────────────────────── */}
      <div
        role="tabpanel"
        id={`fund-panel-${activeSection}`}
        aria-labelledby={`fund-tab-${activeSection}`}
        tabIndex={0}
        className="focus-visible:outline-none"
      >
        {activeSection === "profile" && (
          <ProfileSection profile={data.profile} />
        )}

        {activeSection === "key_ratios" && (
          data.key_ratios
            ? <KeyRatiosSection keyRatios={data.key_ratios} />
            : <EmptyState message="No ratio data available" />
        )}

        {activeSection === "income" && (
          <IncomeStatementSection
            data={data.income_statement ?? { yearly: [], quarterly: [] }}
          />
        )}

        {activeSection === "balance_sheet" && (
          data.balance_sheet?.length
            ? <BalanceSheetSection data={data.balance_sheet} />
            : <EmptyState message="No balance sheet data available" />
        )}

        {activeSection === "cash_flow" && (
          data.cash_flow?.length
            ? <CashFlowSection data={data.cash_flow} />
            : <EmptyState message="No cash flow data available" />
        )}

        {activeSection === "holdings" && (
          data.share_holdings?.length
            ? <ShareHoldingsSection data={data.share_holdings} />
            : <EmptyState message="No shareholding data available" />
        )}

        {activeSection === "corporate_actions" && (
          data.corporate_actions?.length
            ? <CorporateActionsSection data={data.corporate_actions} />
            : <EmptyState message="No corporate actions found" />
        )}

        {activeSection === "competitors" && (
          data.competitors?.length
            ? <CompetitorsSection data={data.competitors} />
            : <EmptyState message="No competitor data available" />
        )}
      </div>
    </div>
  );
}
