"use client";

import { Loader2, AlertCircle } from "lucide-react";
import { useOutcomeStats } from "@/hooks/usePaperTrading";
import { MLFeedbackStats } from "@/components/admin/MLFeedbackStats";
import { AuditOutcomesTable } from "@/components/admin/AuditOutcomesTable";

export default function AuditPage() {
  const {
    data: stats,
    isLoading,
    isRefetching,
    isError,
    refetch,
  } = useOutcomeStats();

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Trade Audit</h1>
        <p className="mt-1 text-sm text-slate-500">
          Platform-wide closed trade outcomes and ML signal accuracy — used for model fine-tuning.
        </p>
      </div>

      {/* Stats panel */}
      {isLoading && (
        <div className="flex items-center gap-3 text-slate-400">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm">Loading ML feedback stats…</span>
        </div>
      )}

      {isError && (
        <div className="flex items-center gap-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
          <AlertCircle className="h-4 w-4 flex-shrink-0" />
          Failed to load ML feedback stats. You may not have admin access.
        </div>
      )}

      {stats && (
        <MLFeedbackStats
          stats={stats}
          onRefresh={() => refetch()}
          isRefreshing={isRefetching}
        />
      )}

      {/* Outcomes table */}
      <AuditOutcomesTable />
    </div>
  );
}
