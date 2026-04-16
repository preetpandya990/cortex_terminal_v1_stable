/**
 * ScanResults Component
 * Displays market scan results including top gainers, losers, and volume spikes
 */

"use client";

import { useMemo, useState } from 'react';
import { TrendingUp, TrendingDown, Volume2, BarChart3 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { StockAnalysis } from '@/types/market';
import { PredictionBadge } from '@/components/ml/PredictionBadge';

type ScannerTab = 'gainers' | 'losers' | 'volume' | 'breakouts';

interface ScanResultsProps {
  topGainers: StockAnalysis[];
  topLosers: StockAnalysis[];
  volumeSpikes: StockAnalysis[];
  breakouts: StockAnalysis[];
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

export function ScanResults({ topGainers, topLosers, volumeSpikes, breakouts }: ScanResultsProps) {
  const [activeTab, setActiveTab] = useState<ScannerTab>('gainers');

  const activeStocks = useMemo(() => {
    if (activeTab === 'gainers') return topGainers;
    if (activeTab === 'losers') return topLosers;
    if (activeTab === 'volume') return volumeSpikes;
    return breakouts;
  }, [activeTab, topGainers, topLosers, volumeSpikes, breakouts]);

  const title =
    activeTab === 'gainers'
      ? 'Top Gainers'
      : activeTab === 'losers'
        ? 'Top Losers'
        : activeTab === 'volume'
          ? 'Volume Spikes'
          : 'Breakout Stocks';

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-2">
        <Button
          variant={activeTab === 'gainers' ? 'default' : 'outline'}
          onClick={() => setActiveTab('gainers')}
        >
          <TrendingUp className="mr-2 h-4 w-4" />
          Top Gainers
        </Button>
        <Button
          variant={activeTab === 'losers' ? 'default' : 'outline'}
          onClick={() => setActiveTab('losers')}
        >
          <TrendingDown className="mr-2 h-4 w-4" />
          Top Losers
        </Button>
        <Button
          variant={activeTab === 'volume' ? 'default' : 'outline'}
          onClick={() => setActiveTab('volume')}
        >
          <Volume2 className="mr-2 h-4 w-4" />
          Volume Spikes
        </Button>
        <Button
          variant={activeTab === 'breakouts' ? 'default' : 'outline'}
          onClick={() => setActiveTab('breakouts')}
        >
          <BarChart3 className="mr-2 h-4 w-4" />
          Breakouts
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{title}</CardTitle>
        </CardHeader>
        <CardContent>
          <StockTable stocks={activeStocks} />
        </CardContent>
      </Card>
    </div>
  );
}

function StockTable({ stocks }: { stocks: StockAnalysis[] }) {
  if (stocks.length === 0) {
    return <div className="py-12 text-center text-sm text-muted-foreground">No stocks in this category.</div>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[760px]">
        <thead className="border-b">
          <tr>
            <th className="px-4 py-3 text-left text-xs font-semibold tracking-wide text-muted-foreground uppercase">Symbol</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">Price</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">Change</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">Change %</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">Volume</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">Vol Ratio</th>
            <th className="px-4 py-3 text-right text-xs font-semibold tracking-wide text-muted-foreground uppercase">RSI</th>
            <th className="px-4 py-3 text-center text-xs font-semibold tracking-wide text-muted-foreground uppercase">ML Signal</th>
          </tr>
        </thead>
        <tbody>
          {stocks.map((stock) => (
            <tr key={stock.symbol} className="border-b last:border-0 hover:bg-muted/40">
              <td className="px-4 py-3 font-medium">{stock.symbol}</td>
              <td className="px-4 py-3 text-right tabular-nums">{rupeeFormatter.format(stock.current_price)}</td>
              <td className={`px-4 py-3 text-right tabular-nums ${stock.price_change >= 0 ? 'text-emerald-600' : 'text-red-600'}`}>
                {rupeeFormatter.format(stock.price_change)}
              </td>
              <td className="px-4 py-3 text-right">
                <Badge variant={stock.price_change_pct >= 0 ? 'default' : 'destructive'}>
                  {stock.price_change_pct >= 0 ? '+' : ''}
                  {stock.price_change_pct.toFixed(2)}%
                </Badge>
              </td>
              <td className="px-4 py-3 text-right tabular-nums">{volumeFormatter.format(stock.volume)}</td>
              <td className="px-4 py-3 text-right tabular-nums">{stock.volume_ratio.toFixed(2)}x</td>
              <td className="px-4 py-3 text-right">
                {stock.rsi !== null ? (
                  <Badge variant={stock.rsi > 70 ? 'destructive' : stock.rsi < 30 ? 'default' : 'secondary'}>
                    {stock.rsi.toFixed(1)}
                  </Badge>
                ) : (
                  <span className="text-muted-foreground">-</span>
                )}
              </td>
              <td className="px-4 py-3 text-center">
                {stock.ml ? (
                  <PredictionBadge
                    signal={stock.ml.signal}
                    confidence={stock.ml.confidence}
                    compact
                  />
                ) : (
                  <span className="text-muted-foreground text-xs">-</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
