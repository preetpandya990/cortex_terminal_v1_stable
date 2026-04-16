"use client";

import { useMemo } from "react";
import {
  Area,
  Bar,
  CartesianGrid,
  ComposedChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import type { UpstoxCandle, UpstoxLtpTick } from "@/types/upstox";

type ChartPoint = {
  timestamp: number;
  timeLabel: string;
  price: number;
  volume: number;
};

const TIME_LABEL_FORMAT = new Intl.DateTimeFormat("en-IN", {
  timeZone: "Asia/Kolkata",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
});

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

const VOLUME_FORMAT = new Intl.NumberFormat("en-IN", {
  notation: "compact",
  maximumFractionDigits: 2,
});

function normalizeTimestamp(value: string | number): number {
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
    return Date.now();
  }
  if (!Number.isFinite(value)) return Date.now();
  return value > 10_000_000_000 ? value : value * 1000;
}

function getMinuteBucket(timestampMs: number): number {
  return Math.floor(timestampMs / 60_000) * 60_000;
}

function toChartPoints(candles: UpstoxCandle[]): ChartPoint[] {
  return candles
    .map((candle) => {
      const [timestamp, , , , close, volume] = candle;
      const timestampMs = normalizeTimestamp(timestamp);
      return {
        timestamp: timestampMs,
        timeLabel: TIME_LABEL_FORMAT.format(new Date(timestampMs)),
        price: Number(close),
        volume: Math.max(0, Number(volume || 0)),
      };
    })
    .sort((a, b) => a.timestamp - b.timestamp);
}

function mergeLiveTick(points: ChartPoint[], tick: UpstoxLtpTick | null | undefined): ChartPoint[] {
  if (!tick || !Number.isFinite(Number(tick.last_price)) || points.length === 0) {
    return points;
  }

  const tickTsMs = normalizeTimestamp(tick.last_trade_time ?? tick.server_timestamp ?? Date.now());
  const tickBucket = getMinuteBucket(tickTsMs);
  const tickPrice = Number(tick.last_price);
  const tickVolumeDelta = Math.max(0, Number(tick.volume_delta ?? 0));

  const next = [...points];
  const last = next[next.length - 1];
  const lastBucket = getMinuteBucket(last.timestamp);

  if (tickBucket < lastBucket) {
    return next;
  }

  if (tickBucket > lastBucket) {
    next.push({
      timestamp: tickBucket,
      timeLabel: TIME_LABEL_FORMAT.format(new Date(tickBucket)),
      price: tickPrice,
      volume: tickVolumeDelta,
    });
    return next;
  }

  next[next.length - 1] = {
    ...last,
    price: tickPrice,
    volume: Math.max(0, last.volume + tickVolumeDelta),
  };
  return next;
}

export function LivePriceVolumeChart({
  candles,
  liveTick,
  height = 300,
}: {
  candles?: UpstoxCandle[];
  liveTick?: UpstoxLtpTick | null;
  height?: number;
}) {
  const points = useMemo(() => {
    const base = toChartPoints(candles ?? []);
    return mergeLiveTick(base, liveTick);
  }, [candles, liveTick]);

  if (points.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center rounded-lg border border-dashed border-muted-foreground/40 bg-muted/20 text-sm text-muted-foreground">
        No candle data available yet.
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart data={points} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
          <CartesianGrid strokeDasharray="3 3" stroke="rgba(148,163,184,0.25)" vertical={false} />
          <XAxis
            dataKey="timeLabel"
            minTickGap={18}
            tick={{ fill: "#94a3b8", fontSize: 11 }}
            axisLine={{ stroke: "rgba(148,163,184,0.2)" }}
            tickLine={false}
          />
          <YAxis
            yAxisId="price"
            orientation="right"
            width={88}
            tick={{ fill: "#cbd5e1", fontSize: 11 }}
            tickFormatter={(value) => INR_FORMAT.format(value)}
            domain={["dataMin - 0.5", "dataMax + 0.5"]}
            axisLine={{ stroke: "rgba(148,163,184,0.2)" }}
            tickLine={false}
          />
          <YAxis yAxisId="volume" hide domain={[0, "dataMax"]} />
          <Tooltip
            contentStyle={{
              borderRadius: 12,
              border: "1px solid rgba(51,65,85,0.7)",
              background: "rgba(2,6,23,0.95)",
              color: "#e2e8f0",
            }}
            labelStyle={{ color: "#94a3b8" }}
            formatter={(value: number | undefined, name: string | undefined) => {
              if (value === undefined) return ["—", name ?? ""];
              if (name === "price") return [INR_FORMAT.format(value), "Price"];
              return [VOLUME_FORMAT.format(value), "Volume"];
            }}
          />
          <Bar yAxisId="volume" dataKey="volume" barSize={4} fill="rgba(59,130,246,0.35)" />
          <Area
            yAxisId="price"
            type="monotone"
            dataKey="price"
            stroke="#22c55e"
            strokeWidth={2}
            fill="rgba(34,197,94,0.16)"
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}
