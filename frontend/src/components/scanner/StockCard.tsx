/**
 * StockCard Component
 * Compact card displaying stock summary with price and change
 */

"use client";

import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';

type StockCardTone = 'default' | 'positive' | 'negative' | 'info';

interface StockCardProps {
  title: string;
  value: string | number;
  tone?: StockCardTone;
  subtitle?: string;
}

const toneStyles: Record<StockCardTone, string> = {
  default: 'text-foreground',
  positive: 'text-emerald-600',
  negative: 'text-red-600',
  info: 'text-sky-600',
};

export function StockCard({ title, value, tone = 'default', subtitle }: StockCardProps) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium text-muted-foreground">{title}</CardTitle>
      </CardHeader>
      <CardContent className="space-y-1">
        <div className={cn('text-2xl font-semibold tabular-nums', toneStyles[tone])}>{value}</div>
        {subtitle ? <p className="text-xs text-muted-foreground">{subtitle}</p> : null}
      </CardContent>
    </Card>
  );
}
