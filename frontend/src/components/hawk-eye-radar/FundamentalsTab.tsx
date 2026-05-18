"use client";

import { useEffect, useRef, useState } from "react";
import {
  createChart,
  IChartApi,
  ISeriesApi,
  LineSeries,
  UTCTimestamp,
  ColorType,
} from "lightweight-charts";
import {
  ChevronDown,
  TrendingUp,
  TrendingDown,
  Minus,
  AlertCircle,
  Building2,
  Coins,
  BarChart3,
  Users,
  Calendar,
  Trophy,
} from "lucide-react";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";
import { useFundamentals } from "@/hooks/useFundamentals";
import type {
  CategoryHistory,
  KeyRatioItem,
  BalanceSheetEntry,
  ShareHoldingEntry,
  CorporateAction,
  CompetitorEntry,
} from "@/types/fundamentals";

// ─── Formatters ───────────────────────────────────────────────────────────────

function formatCrore(val: number | null | undefined): string {
  if (val == null || !Number.isFinite(val)) return "—";
  const abs = Math.abs(val);
  const sign = val < 0 ? "-" : "";
  if (abs >= 100_000) return `${sign}${(abs / 100_000).toFixed(2)} L Cr`;
  if (abs >= 1_000) return `${sign}${abs.toLocaleString("en-IN", { maximumFractionDigits: 0 })} Cr`;
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

// ─── Pivot helper for category-history financial tables ───────────────────────

function pivotCategoryHistory(data: CategoryHistory[]) {
  const periodSet = new Set<string>();
  const byCategory: Record<string, Record<string, { value: number; change_pct: number | null }>> =
    {};

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

  // ISO date strings sort lexicographically = chronologically
  const periods = Array.from(periodSet).sort();
  // Keep only the last 4 years
  const trimmed = periods.slice(-4);

  return { periods: trimmed, byCategory };
}

// Period label from balance sheet entries (uses the period string from the entry)
function bsPeriodLabel(entry: BalanceSheetEntry): string {
  return entry.period;
}

// ─── Collapsible Section ──────────────────────────────────────────────────────

interface CollapsibleSectionProps {
  title: string;
  icon?: React.ReactNode;
  defaultOpen?: boolean;
  children: React.ReactNode;
  badge?: string;
}

function CollapsibleSection({
  title,
  icon,
  defaultOpen = false,
  children,
  badge,
}: CollapsibleSectionProps) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div className="border border-slate-200 rounded-xl overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="w-full flex items-center justify-between px-4 py-3.5 bg-slate-50 hover:bg-slate-100 transition-colors text-left"
      >
        <div className="flex items-center gap-2.5">
          {icon && <span className="text-slate-500">{icon}</span>}
          <span className="text-sm font-semibold text-slate-900">{title}</span>
          {badge && (
            <span className="rounded-full bg-slate-200 px-2 py-0.5 text-xs text-slate-600 font-medium">
              {badge}
            </span>
          )}
        </div>
        <ChevronDown
          className={cn(
            "h-4 w-4 text-slate-400 transition-transform duration-200",
            open && "rotate-180",
          )}
        />
      </button>
      {open && <div className="px-4 py-4">{children}</div>}
    </div>
  );
}

// ─── Section: Company Profile ─────────────────────────────────────────────────

function ProfileSection({
  profile,
}: {
  profile: NonNullable<ReturnType<typeof useFundamentals>["data"]>["profile"];
}) {
  if (!profile) return <p className="text-sm text-slate-400 italic">No profile data</p>;

  return (
    <div className="space-y-4">
      {profile.company_profile && (
        <p className="text-sm text-slate-700 leading-relaxed">{profile.company_profile}</p>
      )}
      <div className="flex flex-wrap gap-2">
        {profile.sector && (
          <span className="inline-flex items-center gap-1.5 rounded-full border border-blue-200 bg-blue-50 px-3 py-1 text-xs font-semibold text-blue-700">
            <Building2 className="h-3 w-3" />
            {profile.sector}
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-2">
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

const RATIO_META: Record<
  string,
  { label: string; higherBetter: boolean; pct?: boolean }
> = {
  "P/E":       { label: "P/E Ratio",   higherBetter: false },
  "P/B":       { label: "P/B Ratio",   higherBetter: false },
  "ROA":       { label: "ROA",          higherBetter: true,  pct: true },
  "ROE":       { label: "ROE",          higherBetter: true,  pct: true },
  "ROCE":      { label: "ROCE",         higherBetter: true,  pct: true },
  "EV/EBITDA": { label: "EV/EBITDA",   higherBetter: false },
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
    <span
      className={cn(
        "inline-flex items-center gap-0.5",
        isGood ? "text-emerald-600" : "text-red-500",
      )}
    >
      <Icon className="h-3 w-3" />
    </span>
  );
}

function KeyRatiosSection({ keyRatios }: { keyRatios: KeyRatioItem[] }) {
  if (!keyRatios.length)
    return <p className="text-sm text-slate-400 italic">No ratio data</p>;

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
      {keyRatios.map((ratio) => {
        const meta = RATIO_META[ratio.name] ?? { label: ratio.name, higherBetter: true };
        return (
          <div
            key={ratio.name}
            className="rounded-lg border border-slate-200 bg-white p-3 flex flex-col gap-1.5"
          >
            <div className="flex items-center justify-between">
              <span className="text-xs font-medium text-slate-500">{meta.label}</span>
              <RatioDelta
                company={ratio.company_value}
                sector={ratio.sector_value}
                higherBetter={meta.higherBetter}
              />
            </div>
            <p className="text-lg font-bold text-slate-900 tabular-nums">
              {formatRatio(ratio.company_value, meta.pct)}
            </p>
            {ratio.sector_value != null && (
              <p className="text-[11px] text-slate-400 tabular-nums">
                Sector: {formatRatio(ratio.sector_value, meta.pct)}
              </p>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Shared: Financial Table ──────────────────────────────────────────────────

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
  if (!periodLabels.length)
    return <p className="text-sm text-slate-400 italic">No data available</p>;

  return (
    <div className="overflow-x-auto -mx-1">
      <table className="w-full min-w-[360px] text-sm">
        <thead>
          <tr className="border-b border-slate-200">
            <th className="text-left py-2 px-2 text-xs font-semibold text-slate-500 w-32" />
            {periodLabels.map((label) => (
              <th
                key={label}
                className="text-right py-2 px-2 text-xs font-semibold text-slate-700 whitespace-nowrap"
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
              <td className="py-2.5 px-2 text-xs font-medium text-slate-600 whitespace-nowrap">
                {row.label}
              </td>
              {row.values.map((val, ci) => {
                const change = row.changes?.[ci];
                const changeFmt = formatChangePct(change);
                const isNegativeVal = typeof val === "number" && val < 0;
                return (
                  <td key={ci} className="py-2.5 px-2 text-right align-top">
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
                          "text-[10px] tabular-nums",
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

function IncomeStatementSection({ data }: { data: CategoryHistory[] }) {
  const { periods, byCategory } = pivotCategoryHistory(data);

  const periodLabels = periods.map((p) => {
    // Find a matching entry with a "period" label string (e.g. "Mar 2025")
    for (const cat of data) {
      const entry = cat.history.find((h) => h.period_date === p);
      if (entry) return entry.period;
    }
    return p;
  });

  const rows = IS_CATEGORIES.map(({ key, label }) => ({
    label,
    values: periods.map((p) => byCategory[key]?.[p]?.value ?? null),
    changes: periods.map((p) => byCategory[key]?.[p]?.change_pct ?? null),
  }));

  return <FinancialTable rows={rows} periods={periods} periodLabels={periodLabels} />;
}

// ─── Section: Balance Sheet ───────────────────────────────────────────────────

function BalanceSheetSection({ data }: { data: BalanceSheetEntry[] }) {
  const trimmed = data.slice(-4);
  const periodLabels = trimmed.map(bsPeriodLabel);

  const rows = [
    {
      label: "Total Assets",
      values: trimmed.map((e) => e.total_asset),
      alwaysPositive: true,
    },
    {
      label: "Total Liabilities",
      values: trimmed.map((e) => e.total_liability),
      alwaysPositive: true,
    },
    {
      label: "Net Worth",
      values: trimmed.map((e) => e.net_worth),
    },
  ];

  return <FinancialTable rows={rows} periods={trimmed.map((e) => e.period_date)} periodLabels={periodLabels} />;
}

// ─── Section: Cash Flow ───────────────────────────────────────────────────────

const CF_CATEGORIES = [
  { key: "operating",  label: "Operating CF" },
  { key: "investing",  label: "Investing CF" },
  { key: "financing",  label: "Financing CF" },
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
    values: periods.map((p) => byCategory[key]?.[p]?.value ?? null),
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
  bg: string;
}[] = [
  { key: "promoters_pct",        label: "Promoters",     color: "#3b82f6", bg: "bg-blue-500"    },
  { key: "fii_pct",              label: "FII",           color: "#8b5cf6", bg: "bg-violet-500"  },
  { key: "mutual_funds_pct",     label: "Mutual Funds",  color: "#f59e0b", bg: "bg-amber-400"   },
  { key: "other_dii_pct",        label: "Other DII",     color: "#10b981", bg: "bg-emerald-500" },
  { key: "retail_and_other_pct", label: "Retail & Other",color: "#94a3b8", bg: "bg-slate-400"   },
];

function ShareHoldingsSection({ data }: { data: ShareHoldingEntry[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef     = useRef<IChartApi | null>(null);
  const promoterRef  = useRef<ISeriesApi<"Line"> | null>(null);
  const fiiRef       = useRef<ISeriesApi<"Line"> | null>(null);

  const latest = data[data.length - 1];
  const prior  = data.length >= 2 ? data[data.length - 2] : null;

  // ── Lightweight Charts — promoter & FII trend ───────────────────────────────
  useEffect(() => {
    if (!containerRef.current || data.length < 2) return;

    const chart = createChart(containerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: "#6b7280",
        fontSize: 11,
      },
      grid: {
        vertLines: { color: "#f3f4f6" },
        horzLines: { color: "#f3f4f6" },
      },
      crosshair: {
        vertLine: { labelBackgroundColor: "#e5e7eb" },
        horzLine: { labelBackgroundColor: "#e5e7eb" },
      },
      timeScale: {
        borderColor: "#e5e7eb",
        timeVisible: false,
        secondsVisible: false,
      },
      rightPriceScale: {
        borderColor: "#e5e7eb",
        scaleMargins: { top: 0.1, bottom: 0.1 },
      },
      width: containerRef.current.clientWidth,
      height: 180,
    });

    chartRef.current = chart;

    const toTs = (isoDate: string): UTCTimestamp =>
      (new Date(isoDate).getTime() / 1000) as UTCTimestamp;

    const promoterSeries = chart.addSeries(LineSeries, {
      color: "#3b82f6",
      lineWidth: 2,
      title: "Promoters %",
      lastValueVisible: true,
      priceLineVisible: false,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
    });
    const fiiSeries = chart.addSeries(LineSeries, {
      color: "#8b5cf6",
      lineWidth: 2,
      title: "FII %",
      lastValueVisible: true,
      priceLineVisible: false,
      crosshairMarkerVisible: true,
      crosshairMarkerRadius: 4,
    });

    promoterRef.current = promoterSeries;
    fiiRef.current = fiiSeries;

    const promoterData = data
      .filter((d) => d.promoters_pct != null)
      .map((d) => ({ time: toTs(d.period_date), value: d.promoters_pct! }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    const fiiData = data
      .filter((d) => d.fii_pct != null)
      .map((d) => ({ time: toTs(d.period_date), value: d.fii_pct! }))
      .sort((a, b) => (a.time as number) - (b.time as number));

    if (promoterData.length) promoterSeries.setData(promoterData);
    if (fiiData.length) fiiSeries.setData(fiiData);

    chart.timeScale().fitContent();

    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry || !chartRef.current) return;
      chartRef.current.applyOptions({ width: entry.contentRect.width });
    });
    observer.observe(containerRef.current);

    return () => {
      observer.disconnect();
      chartRef.current?.remove();
      chartRef.current = null;
      promoterRef.current = null;
      fiiRef.current = null;
    };
    // Recreate chart when data changes (instrument change)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (!latest) return <p className="text-sm text-slate-400 italic">No shareholding data</p>;

  return (
    <div className="space-y-5">
      {/* Latest quarter stacked bar */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <p className="text-xs font-semibold text-slate-700">Latest Quarter — {latest.period}</p>
        </div>
        <div className="flex h-5 w-full overflow-hidden rounded-full bg-slate-100">
          {HOLDING_SEGMENTS.map(({ key, bg, color }) => {
            const val = latest[key];
            if (!val || val <= 0) return null;
            return (
              <div
                key={key}
                className={cn("h-full transition-all", bg)}
                style={{ width: `${val}%`, backgroundColor: color }}
                title={`${HOLDING_SEGMENTS.find((s) => s.key === key)?.label}: ${val.toFixed(2)}%`}
              />
            );
          })}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2">
          {HOLDING_SEGMENTS.map(({ key, label, color }) => {
            const val = latest[key];
            const priorVal = prior?.[key] ?? null;
            const delta =
              val != null && priorVal != null ? val - priorVal : null;
            return (
              <div key={key} className="flex items-center gap-2">
                <span
                  className="h-2 w-2 rounded-full shrink-0"
                  style={{ backgroundColor: color }}
                />
                <span className="text-xs text-slate-600 flex-1 truncate">{label}</span>
                <span className="text-xs font-semibold tabular-nums text-slate-900">
                  {val != null ? `${val.toFixed(2)}%` : "—"}
                </span>
                {delta != null && (
                  <span
                    className={cn(
                      "text-[10px] tabular-nums",
                      delta > 0
                        ? "text-emerald-600"
                        : delta < 0
                        ? "text-red-500"
                        : "text-slate-400",
                    )}
                  >
                    {delta > 0 ? "+" : ""}
                    {delta.toFixed(2)}%
                  </span>
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Trend chart */}
      {data.length >= 2 && (
        <div>
          <div className="flex items-center gap-3 mb-2">
            <p className="text-xs font-semibold text-slate-700">Trend</p>
            <div className="flex items-center gap-3">
              <span className="flex items-center gap-1 text-[11px] text-slate-500">
                <span className="h-2 w-4 rounded-full bg-blue-500 inline-block" />
                Promoters
              </span>
              <span className="flex items-center gap-1 text-[11px] text-slate-500">
                <span className="h-2 w-4 rounded-full bg-violet-500 inline-block" />
                FII
              </span>
            </div>
          </div>
          <div
            ref={containerRef}
            className="rounded-lg border border-slate-200 bg-white overflow-hidden"
            style={{ height: 180 }}
          />
        </div>
      )}
    </div>
  );
}

// ─── Section: Corporate Actions ───────────────────────────────────────────────

const ACTION_ICONS: Record<string, React.ReactNode> = {
  Dividend: <Coins className="h-3.5 w-3.5 text-amber-600" />,
  Bonus:    <Trophy className="h-3.5 w-3.5 text-emerald-600" />,
  Split:    <BarChart3 className="h-3.5 w-3.5 text-blue-600" />,
};

function CorporateActionsSection({ data }: { data: CorporateAction[] }) {
  if (!data.length)
    return <p className="text-sm text-slate-400 italic">No corporate actions found</p>;

  return (
    <div className="space-y-0 divide-y divide-slate-100">
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
                <span className="rounded bg-amber-50 border border-amber-200 px-1.5 py-0.5 text-[11px] font-semibold text-amber-700">
                  ₹{action.amount}
                </span>
              )}
              {action.ratio && (
                <span className="rounded bg-blue-50 border border-blue-200 px-1.5 py-0.5 text-[11px] font-medium text-blue-700">
                  {action.ratio}
                </span>
              )}
            </div>
            <p className="mt-0.5 text-xs text-slate-500">
              Ex-Date: {action.expiry_date_str ?? "—"}
            </p>
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Section: Competitors ─────────────────────────────────────────────────────

function CompetitorsSection({ data }: { data: CompetitorEntry[] }) {
  if (!data.length)
    return <p className="text-sm text-slate-400 italic">No competitor data</p>;

  return (
    <div className="space-y-2">
      {data.map((comp, i) => (
        <div
          key={i}
          className="flex items-start gap-3 rounded-lg border border-slate-200 bg-slate-50 p-3"
        >
          <div className="flex-1 min-w-0">
            <div className="flex flex-wrap items-center gap-2 mb-0.5">
              {/* Trading symbol is the human-readable ticker (e.g. "BPCL") */}
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
                <span className="rounded-full border border-blue-200 bg-blue-50 px-2 py-0.5 text-[10px] font-medium text-blue-600 truncate max-w-[160px]">
                  {comp.sector}
                </span>
              )}
            </div>
            {/* Full company name below the ticker */}
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
              <p className="text-[11px] text-slate-400">
                ${formatMcap(comp.mcap_usd, comp.mcap_usd_unit)}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Skeleton loader ──────────────────────────────────────────────────────────

function FundamentalsTabSkeleton() {
  return (
    <div className="space-y-3">
      {Array.from({ length: 4 }).map((_, i) => (
        <div key={i} className="rounded-xl border border-slate-200 overflow-hidden">
          <div className="px-4 py-3.5 bg-slate-50 flex items-center justify-between">
            <Skeleton className="h-4 w-32" />
            <Skeleton className="h-4 w-4 rounded" />
          </div>
        </div>
      ))}
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

interface FundamentalsTabProps {
  instrumentKey: string;
}

export function FundamentalsTab({ instrumentKey }: FundamentalsTabProps) {
  const { data, isLoading, isError } = useFundamentals(instrumentKey);

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

  const hasCompetitors = (data.competitors?.length ?? 0) > 0;

  return (
    <div className="space-y-3">
      {/* 1. Company Profile */}
      <CollapsibleSection
        title="Company Profile"
        icon={<Building2 className="h-4 w-4" />}
        defaultOpen
      >
        <ProfileSection profile={data.profile} />
      </CollapsibleSection>

      {/* 2. Key Ratios */}
      <CollapsibleSection
        title="Key Ratios"
        icon={<BarChart3 className="h-4 w-4" />}
        defaultOpen
      >
        {data.key_ratios ? (
          <KeyRatiosSection keyRatios={data.key_ratios} />
        ) : (
          <p className="text-sm text-slate-400 italic">No ratio data</p>
        )}
      </CollapsibleSection>

      {/* 3. Income Statement */}
      <CollapsibleSection
        title="Income Statement"
        icon={<TrendingUp className="h-4 w-4" />}
      >
        {data.income_statement?.length ? (
          <IncomeStatementSection data={data.income_statement} />
        ) : (
          <p className="text-sm text-slate-400 italic">No income statement data</p>
        )}
      </CollapsibleSection>

      {/* 4. Balance Sheet */}
      <CollapsibleSection
        title="Balance Sheet"
        icon={<BarChart3 className="h-4 w-4" />}
      >
        {data.balance_sheet?.length ? (
          <BalanceSheetSection data={data.balance_sheet} />
        ) : (
          <p className="text-sm text-slate-400 italic">No balance sheet data</p>
        )}
      </CollapsibleSection>

      {/* 5. Cash Flow */}
      <CollapsibleSection
        title="Cash Flow"
        icon={<TrendingUp className="h-4 w-4" />}
      >
        {data.cash_flow?.length ? (
          <CashFlowSection data={data.cash_flow} />
        ) : (
          <p className="text-sm text-slate-400 italic">No cash flow data</p>
        )}
      </CollapsibleSection>

      {/* 6. Share Holdings */}
      <CollapsibleSection
        title="Share Holdings"
        icon={<Users className="h-4 w-4" />}
      >
        {data.share_holdings?.length ? (
          <ShareHoldingsSection data={data.share_holdings} />
        ) : (
          <p className="text-sm text-slate-400 italic">No shareholding data</p>
        )}
      </CollapsibleSection>

      {/* 7. Corporate Actions */}
      <CollapsibleSection
        title="Corporate Actions"
        icon={<Calendar className="h-4 w-4" />}
        badge={data.corporate_actions?.length ? String(data.corporate_actions.length) : undefined}
      >
        {data.corporate_actions?.length ? (
          <CorporateActionsSection data={data.corporate_actions} />
        ) : (
          <p className="text-sm text-slate-400 italic">No recent corporate actions</p>
        )}
      </CollapsibleSection>

      {/* 8. Competitors */}
      <CollapsibleSection
        title="Competitors"
        icon={<Trophy className="h-4 w-4" />}
        badge={hasCompetitors ? String(data.competitors!.length) : undefined}
      >
        {hasCompetitors ? (
          <CompetitorsSection data={data.competitors!} />
        ) : (
          <p className="text-sm text-slate-400 italic">No competitor data</p>
        )}
      </CollapsibleSection>
    </div>
  );
}
