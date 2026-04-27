/**
 * Signal Detail Modal with full audit trail
 */
import * as React from "react";
import { format } from "date-fns";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useSignalAudit } from "@/hooks/useSignals";
import { SignalType, type TradingSignal } from "@/types/signals";
import { TrendingUp, TrendingDown, Minus, X } from "lucide-react";

// Never call format() directly on untrusted date strings — the DB may store
// nulls or malformed values that crash date-fns with RangeError.
function safeFormat(value: string | null | undefined, fmt: string): string {
  if (!value) return "—";
  const d = new Date(value);
  if (isNaN(d.getTime())) return "—";
  return format(d, fmt);
}

interface SignalDetailModalProps {
  signal: TradingSignal | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function SignalDetailModal({
  signal,
  open,
  onOpenChange,
}: SignalDetailModalProps) {
  const { data: auditData } = useSignalAudit(signal?.signal_id ?? "");

  const getSignalIcon = (type: SignalType) => {
    switch (type) {
      case SignalType.BUY:
        return <TrendingUp className="size-4" />;
      case SignalType.SELL:
        return <TrendingDown className="size-4" />;
      case SignalType.HOLD:
        return <Minus className="size-4" />;
    }
  };

  const getSignalColor = (type: SignalType) => {
    switch (type) {
      case SignalType.BUY:
        return "bg-green-500/10 text-green-700 dark:text-green-400";
      case SignalType.SELL:
        return "bg-red-500/10 text-red-700 dark:text-red-400";
      case SignalType.HOLD:
        return "bg-yellow-500/10 text-yellow-700 dark:text-yellow-400";
    }
  };

  if (!signal) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      {/*
       * !flex overrides the default `grid` on DialogContent.
       * !p-0 removes the default p-6 so we control padding per section.
       * max-h-[90vh] + overflow-hidden constrains to the viewport.
       * flex-col lets the header pin at top and body grow+scroll.
       */}
      <DialogContent className="max-w-4xl !flex flex-col max-h-[90vh] overflow-hidden !p-0">

        {/* ── Sticky header — always visible, close button always reachable ── */}
        <div className="flex-shrink-0 border-b border-slate-100 px-6 pt-6 pb-4">
          <DialogHeader>
            <div className="flex items-center justify-between">
              <DialogTitle>Signal Details</DialogTitle>
              <Button
                variant="ghost"
                size="icon-sm"
                onClick={() => onOpenChange(false)}
              >
                <X className="size-4" />
              </Button>
            </div>
            <DialogDescription>
              {signal.symbol} • Generated {safeFormat(signal.generated_at, "PPp")}
            </DialogDescription>
          </DialogHeader>
        </div>

        {/* ── Scrollable body ── */}
        {signal ? (
          <div className="flex-1 min-h-0 overflow-y-auto px-6 py-6">
            <div className="space-y-6">
            {/* Signal Overview */}
            <Card>
              <CardHeader>
                <CardTitle>Signal Overview</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <div className="text-muted-foreground text-sm">Symbol</div>
                    <div className="text-lg font-semibold">{signal.symbol}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-sm">
                      Direction
                    </div>
                    <Badge className={getSignalColor(signal.signal_type)}>
                      {getSignalIcon(signal.signal_type)}
                      {signal.signal_type.toUpperCase()}
                    </Badge>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-sm">
                      Confidence
                    </div>
                    <div className="text-lg font-semibold">
                      {(signal.calibrated_confidence * 100).toFixed(1)}%
                    </div>
                  </div>
                  <div>
                    <div className="text-muted-foreground text-sm">
                      Time Horizon
                    </div>
                    <div className="text-lg font-semibold">
                      {signal.time_horizon}
                    </div>
                  </div>
                  {signal.target_price && (
                    <div>
                      <div className="text-muted-foreground text-sm">
                        Target Price
                      </div>
                      <div className="text-lg font-semibold">
                        ₹{signal.target_price.toFixed(2)}
                      </div>
                    </div>
                  )}
                  {signal.stop_loss && (
                    <div>
                      <div className="text-muted-foreground text-sm">
                        Stop Loss
                      </div>
                      <div className="text-lg font-semibold">
                        ₹{signal.stop_loss.toFixed(2)}
                      </div>
                    </div>
                  )}
                </div>
              </CardContent>
            </Card>

            {/* Reasoning */}
            <Card>
              <CardHeader>
                <CardTitle>Reasoning</CardTitle>
              </CardHeader>
              <CardContent>
                <p className="text-sm">{signal.reasoning}</p>
              </CardContent>
            </Card>

            {/* Contributing Factors */}
            <Card>
              <CardHeader>
                <CardTitle>Contributing Factors</CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {signal.contributing_factors.events.length > 0 && (
                  <div>
                    <h4 className="mb-2 font-semibold">Events</h4>
                    <div className="space-y-2">
                      {signal.contributing_factors.events.map((event, idx) => (
                        <div
                          key={event.event_id ?? `event-${idx}`}
                          className="rounded-lg border p-3"
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-sm font-medium">
                              {event.event_type}
                            </span>
                            <Badge variant="outline">
                              Impact: {(event.impact_score * 100).toFixed(0)}%
                            </Badge>
                          </div>
                          <p className="text-muted-foreground mt-1 text-sm">
                            {event.summary}
                          </p>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {signal.contributing_factors.ml_predictions.length > 0 && (
                  <div>
                    <h4 className="mb-2 font-semibold">ML Predictions</h4>
                    <div className="space-y-2">
                      {signal.contributing_factors.ml_predictions.map(
                        (pred, idx) => (
                          <div
                            key={pred.model_id ?? `pred-${idx}`}
                            className="rounded-lg border p-3"
                          >
                            <div className="flex items-center justify-between">
                              <span className="text-sm font-medium">
                                {pred.model_name}
                              </span>
                              <Badge variant="outline">
                                {(pred.confidence * 100).toFixed(1)}%
                              </Badge>
                            </div>
                            <p className="text-muted-foreground mt-1 text-sm">
                              {pred.prediction}
                            </p>
                          </div>
                        )
                      )}
                    </div>
                  </div>
                )}

                {signal.contributing_factors.technical.length > 0 && (
                  <div>
                    <h4 className="mb-2 font-semibold">Technical Indicators</h4>
                    <div className="grid grid-cols-2 gap-2">
                      {signal.contributing_factors.technical.map(
                        (tech, idx) => (
                          <div key={idx} className="rounded-lg border p-3">
                            <div className="text-sm font-medium">
                              {tech.indicator}
                            </div>
                            <div className="text-muted-foreground text-sm">
                              {tech.value.toFixed(2)} • {tech.signal}
                            </div>
                          </div>
                        )
                      )}
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>

            {/* Audit Trail */}
            {auditData && auditData.audit_log.length > 0 && (
              <Card>
                <CardHeader>
                  <CardTitle>Audit Trail</CardTitle>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    {auditData.audit_log.map((entry) => (
                      <div
                        key={entry.audit_id}
                        className="flex items-center justify-between rounded-lg border p-3"
                      >
                        <div>
                          <div className="text-sm font-medium">
                            {entry.symbol} • {entry.signal_type.toUpperCase()}
                          </div>
                          <div className="text-muted-foreground text-xs">
                            {safeFormat(entry.generated_at, "PPp")}
                          </div>
                        </div>
                        <div className="text-right">
                          <div className="text-sm font-medium">
                            {(entry.confidence * 100).toFixed(1)}%
                          </div>
                          {entry.outcome && (
                            <Badge variant="outline" className="mt-1">
                              {entry.outcome}
                            </Badge>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </CardContent>
              </Card>
            )}
            </div>
          </div>
        ) : null}
      </DialogContent>
    </Dialog>
  );
}
