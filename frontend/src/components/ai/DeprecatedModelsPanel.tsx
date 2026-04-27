"use client";

/**
 * Deprecated / Retired Models Panel
 *
 * Shows models in shadow state — demoted from live/paper due to drift,
 * performance degradation, or manual retirement.
 * Collapsed by default; admins can restore a model to paper for re-evaluation.
 */
import * as React from "react";
import { format } from "date-fns";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useModels, useUpdateModelState } from "@/hooks/useModels";
import { ModelState, type MLModel } from "@/types/models";
import { ChevronDown, ChevronRight, RefreshCw, RotateCcw } from "lucide-react";

interface DeprecatedModelsPanelProps {
  className?: string;
  isAdmin?: boolean;
}

export function DeprecatedModelsPanel({
  className,
  isAdmin = false,
}: DeprecatedModelsPanelProps) {
  const [expanded, setExpanded] = React.useState(false);
  const [restoreTarget, setRestoreTarget] = React.useState<MLModel | null>(null);

  const { data, isLoading, refetch, isRefetching } = useModels({
    state: ModelState.SHADOW,
    limit: 50,
  });

  const updateState = useUpdateModelState();
  const count = data?.models.length ?? 0;

  const confirmRestore = async () => {
    if (!restoreTarget) return;
    await updateState.mutateAsync({
      modelId: restoreTarget.model_id,
      data: {
        new_state: ModelState.PAPER,
        reason: "Manually restored from shadow to paper for re-evaluation",
      },
    });
    setRestoreTarget(null);
  };

  return (
    <>
      <Card className={`border-dashed border-muted-foreground/30 ${className ?? ""}`}>
        {/* ── Header / toggle ── */}
        <CardHeader className="pb-3">
          <div className="flex items-center justify-between">
            {/* Toggle — plain div, not a button, to avoid nested-button violation */}
            <div
              role="button"
              tabIndex={0}
              className="flex flex-1 cursor-pointer items-center gap-2 text-left"
              onClick={() => setExpanded((v) => !v)}
              onKeyDown={(e) => e.key === "Enter" && setExpanded((v) => !v)}
              aria-expanded={expanded}
            >
              {expanded ? (
                <ChevronDown className="size-4 text-muted-foreground" />
              ) : (
                <ChevronRight className="size-4 text-muted-foreground" />
              )}
              <CardTitle className="text-base font-medium text-muted-foreground">
                Deprecated / Retired Models
              </CardTitle>
              {count > 0 && (
                <Badge
                  variant="outline"
                  className="border-muted-foreground/30 text-xs text-muted-foreground"
                >
                  {count}
                </Badge>
              )}
            </div>

            {expanded && (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => refetch()}
                disabled={isRefetching}
                className="text-muted-foreground"
              >
                <RefreshCw className={`size-3.5 ${isRefetching ? "animate-spin" : ""}`} />
              </Button>
            )}
          </div>
        </CardHeader>

        {/* ── Body (shown only when expanded) ── */}
        {expanded && (
          <CardContent className="pt-0">
            {isLoading ? (
              <div className="flex items-center justify-center py-6">
                <RefreshCw className="size-5 animate-spin text-muted-foreground" />
              </div>
            ) : count === 0 ? (
              <p className="py-4 text-center text-sm text-muted-foreground">
                No deprecated models.
              </p>
            ) : (
              <div className="rounded-md border border-dashed border-muted-foreground/20">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="text-muted-foreground">Model</TableHead>
                      <TableHead className="text-muted-foreground">Type</TableHead>
                      <TableHead className="text-muted-foreground">Version</TableHead>
                      <TableHead className="text-muted-foreground">Accuracy</TableHead>
                      <TableHead className="text-muted-foreground">Last Updated</TableHead>
                      {isAdmin && (
                        <TableHead className="text-muted-foreground">Actions</TableHead>
                      )}
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data!.models.map((model) => (
                      <TableRow key={model.model_id} className="opacity-60 hover:opacity-90">
                        <TableCell className="font-medium text-muted-foreground">
                          {model.model_name}
                        </TableCell>
                        <TableCell>
                          <span className="text-xs text-muted-foreground">
                            {model.model_type}
                          </span>
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant="outline"
                            className="border-muted-foreground/30 text-muted-foreground"
                          >
                            {model.version}
                          </Badge>
                        </TableCell>
                        <TableCell>
                          {model.accuracy_metrics?.accuracy != null ? (
                            <span className="text-sm text-muted-foreground">
                              {(model.accuracy_metrics.accuracy * 100).toFixed(1)}%
                            </span>
                          ) : (
                            <span className="text-xs text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell>
                          <span className="text-xs text-muted-foreground">
                            {model.registered_at
                              ? format(new Date(model.registered_at), "MMM d, yyyy")
                              : "—"}
                          </span>
                        </TableCell>
                        {isAdmin && (
                          <TableCell>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="h-7 gap-1.5 text-xs text-muted-foreground hover:text-foreground"
                              onClick={() => setRestoreTarget(model)}
                              title="Restore to Paper for re-evaluation"
                            >
                              <RotateCcw className="size-3" />
                              Restore
                            </Button>
                          </TableCell>
                        )}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </CardContent>
        )}
      </Card>

      {/* ── Restore confirmation dialog ── */}
      <Dialog
        open={!!restoreTarget}
        onOpenChange={(o) => !o && setRestoreTarget(null)}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Restore Model to Paper</DialogTitle>
            <DialogDescription>
              <strong>{restoreTarget?.model_name}</strong> will be moved from{" "}
              <strong>shadow</strong> to <strong>paper</strong> for re-evaluation
              before it can go live again. This does not automatically serve
              predictions.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setRestoreTarget(null)}>
              Cancel
            </Button>
            <Button onClick={confirmRestore} disabled={updateState.isPending}>
              {updateState.isPending ? (
                <>
                  <RefreshCw className="mr-2 size-4 animate-spin" />
                  Restoring…
                </>
              ) : (
                "Restore to Paper"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
