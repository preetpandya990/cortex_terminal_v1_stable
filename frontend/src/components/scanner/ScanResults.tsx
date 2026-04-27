"use client";

import { useMemo, useState } from 'react';
import { TrendingUp, TrendingDown, Volume2, AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { StockAnalysis } from '@/types/market';

export type ScannerTab = 'gainers' | 'losers' | 'volume';

interface ScanResultsProps {
  topGainers: StockAnalysis[];
  topLosers: StockAnalysis[];
  volumeSpikes: StockAnalysis[];
  onStockSelect?: (stock: StockAnalysis, listType: ScannerTab) => void;
}

// Translates internal warning codes (e.g. "STALE_DATA:5d") into readable copy.
function decodeWarning(raw: string): string {
  const staleMatch = raw.match(/^STALE_DATA:(\d+)d$/);
  if (staleMatch) return `Live price unavailable — last stored data is ${staleMatch[1]} day(s) old`;
  return raw;
}

const rupeeFormatter = new Intl.NumberFormat('en-IN', {
  style: 'currency',
  currency: 'INR',
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const volumeFormatter = new Intl.NumberFormat('en-IN', {
  notation: 'compact',
  maximumFractionDigits: 2,
});

const TABS: { id: ScannerTab; label: string; icon: typeof TrendingUp }[] = [
  { id: 'gainers', label: 'Gainers', icon: TrendingUp },
  { id: 'losers', label: 'Losers', icon: TrendingDown },
  { id: 'volume', label: 'Volume Spikes', icon: Volume2 },
];

function getSectors(stocks: StockAnalysis[]): string[] {
  const seen = new Set<string>();
  for (const s of stocks) {
    if (s.sector) seen.add(s.sector);
  }
  return Array.from(seen).sort();
}

export function ScanResults({ topGainers, topLosers, volumeSpikes, onStockSelect }: ScanResultsProps) {
  const [activeTab, setActiveTab] = useState<ScannerTab>('gainers');
  const [selectedSector, setSelectedSector] = useState<string | null>(null);

  const rawStocks = useMemo(() => {
    if (activeTab === 'gainers') return topGainers;
    if (activeTab === 'losers') return topLosers;
    return volumeSpikes;
  }, [activeTab, topGainers, topLosers, volumeSpikes]);

  const sectors = useMemo(() => getSectors(rawStocks), [rawStocks]);

  const activeStocks = useMemo(
    () => (selectedSector ? rawStocks.filter((s) => s.sector === selectedSector) : rawStocks),
    [rawStocks, selectedSector],
  );

  const activeTitle = TABS.find((t) => t.id === activeTab)?.label ?? '';

  function handleTabChange(tab: ScannerTab) {
    setActiveTab(tab);
    setSelectedSector(null);
  }

  return (
    <div className="space-y-4">
      {/* Tab switcher */}
      <div className="flex flex-wrap gap-2">
        {TABS.map(({ id, label, icon: Icon }) => (
          <Button
            key={id}
            variant={activeTab === id ? 'default' : 'outline'}
            onClick={() => handleTabChange(id)}
          >
            <Icon className="mr-2 h-4 w-4" />
            {label}
          </Button>
        ))}
      </div>

      {/* Sector filter chips */}
      {sectors.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-slate-400 uppercase tracking-wide mr-1">Sector</span>
          <button
            onClick={() => setSelectedSector(null)}
            className={`inline-flex items-center rounded-lg border px-3 py-1.5 text-xs font-medium transition-all duration-150 ${
              selectedSector === null
                ? 'border-slate-800 bg-slate-900 text-white shadow-sm'
                : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900'
            }`}
          >
            All
          </button>
          {sectors.map((sector) => (
            <button
              key={sector}
              onClick={() => setSelectedSector(sector === selectedSector ? null : sector)}
              className={`inline-flex items-center rounded-lg border px-3 py-1.5 text-xs font-medium transition-all duration-150 ${
                selectedSector === sector
                  ? 'border-slate-800 bg-slate-900 text-white shadow-sm'
                  : 'border-slate-200 bg-white text-slate-600 hover:border-slate-300 hover:bg-slate-50 hover:text-slate-900'
              }`}
            >
              {sector}
            </button>
          ))}
        </div>
      )}

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2">
            {activeTitle}
            <span className="text-sm font-normal text-muted-foreground">
              ({activeStocks.length})
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <StockTable
            stocks={activeStocks}
            onRowClick={onStockSelect ? (stock) => onStockSelect(stock, activeTab) : undefined}
          />
        </CardContent>
      </Card>
    </div>
  );
}

function StockTable({
  stocks,
  onRowClick,
}: {
  stocks: StockAnalysis[];
  onRowClick?: (stock: StockAnalysis) => void;
}) {
  if (stocks.length === 0) {
    return (
      <div className="py-12 text-center text-sm text-muted-foreground">
        No stocks in this category.
      </div>
    );
  }

  return (
    <div className="overflow-auto max-h-[calc(100vh-280px)]">
      <table className="w-full min-w-[820px]">
        <thead className="border-b bg-muted/30 sticky top-0 z-10">
          <tr>
            <th className="px-6 py-3 text-left text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Symbol
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Price
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Change
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Change %
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Volume
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              Vol Ratio
            </th>
            <th className="px-6 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">
              RSI
            </th>
          </tr>
        </thead>
        <tbody>
          {stocks.map((stock) => (
            <tr
              key={stock.symbol}
              className={`border-b last:border-0 transition-colors hover:bg-muted/40 ${
                onRowClick ? 'cursor-pointer select-none' : ''
              }`}
              onClick={onRowClick ? () => onRowClick(stock) : undefined}
              tabIndex={onRowClick ? 0 : undefined}
              onKeyDown={onRowClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onRowClick(stock); } } : undefined}
              role={onRowClick ? 'button' : undefined}
              aria-label={onRowClick ? `View details for ${stock.trading_symbol ?? stock.symbol}` : undefined}
            >
              <td className="px-6 py-3">
                <div className="flex items-center gap-2">
                  <span className="font-medium">
                    {stock.trading_symbol ?? stock.symbol}
                  </span>
                  {(stock.warnings?.length ?? 0) > 0 && (
                    <span
                      title={stock.warnings!.map(decodeWarning).join(' · ')}
                      className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-700"
                    >
                      <AlertTriangle className="h-3 w-3 shrink-0" />
                      Stale
                    </span>
                  )}
                </div>
                {stock.name ? (
                  <span
                    className="block max-w-[180px] truncate text-xs text-muted-foreground"
                    title={stock.name}
                  >
                    {stock.name}
                  </span>
                ) : null}
              </td>
              <td className="px-6 py-3 text-right tabular-nums">
                {rupeeFormatter.format(stock.current_price)}
              </td>
              <td
                className={`px-6 py-3 text-right tabular-nums font-medium ${
                  stock.price_change >= 0 ? 'text-emerald-600' : 'text-red-600'
                }`}
              >
                {stock.price_change >= 0 ? '+' : ''}
                {rupeeFormatter.format(stock.price_change)}
              </td>
              <td className="px-6 py-3 text-right">
                <Badge variant={stock.price_change_pct >= 0 ? 'default' : 'destructive'}>
                  {stock.price_change_pct >= 0 ? '+' : ''}
                  {stock.price_change_pct.toFixed(2)}%
                </Badge>
              </td>
              <td className="px-6 py-3 text-right tabular-nums">
                {volumeFormatter.format(stock.volume)}
              </td>
              <td className="px-6 py-3 text-right tabular-nums">
                {stock.volume_ratio.toFixed(2)}x
              </td>
              <td className="px-6 py-3 text-right">
                {stock.rsi !== null && stock.rsi !== undefined ? (
                  <Badge
                    variant={
                      stock.rsi > 70 ? 'destructive' : stock.rsi < 30 ? 'default' : 'secondary'
                    }
                  >
                    {stock.rsi.toFixed(1)}
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">—</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
