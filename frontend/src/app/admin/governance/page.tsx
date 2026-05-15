"use client";

import { MLModelsPanel } from "@/components/ai/MLModelsPanel";

export default function GovernancePage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">ML Governance</h1>
        <p className="mt-1 text-sm text-slate-500">
          Model registry, deployment lifecycle, drift monitoring, and state management.
        </p>
      </div>

      <MLModelsPanel isAdmin={true} />
    </div>
  );
}
