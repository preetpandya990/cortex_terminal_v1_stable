"use client";

import { MLModelsPanel } from "@/components/ai/MLModelsPanel";
import { DeprecatedModelsPanel } from "@/components/ai/DeprecatedModelsPanel";

export default function GovernancePage() {
  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">ML Governance</h1>
        <p className="mt-1 text-sm text-slate-500">
          Model registry, deployment state transitions, drift monitoring, and retired model management.
        </p>
      </div>

      <MLModelsPanel isAdmin={true} />
      <DeprecatedModelsPanel isAdmin={true} />
    </div>
  );
}
