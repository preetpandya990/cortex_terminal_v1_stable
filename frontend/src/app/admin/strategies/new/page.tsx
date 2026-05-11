'use client';

import { useRouter } from 'next/navigation';
import { BookOpen, Globe } from 'lucide-react';
import { adminStrategiesAPI } from '@/lib/api';
import { StrategyFormWizard } from '@/components/strategies/StrategyFormWizard';
import type { CreateStrategyRequest } from '@/types/strategies';

export default function AdminNewLibraryStrategyPage() {
  const router = useRouter();

  const handleSubmit = async (payload: CreateStrategyRequest) => {
    await adminStrategiesAPI.createLibraryStrategy(payload);
    router.push('/admin/strategies');
  };

  return (
    <div className="mx-auto max-w-2xl py-8">
      <StrategyFormWizard
        onSubmit={handleSubmit}
        onBack={() => router.push('/admin/strategies')}
        submitLabel="Publish to Library"
        headerSlot={
          <div className="space-y-1">
            <div className="flex items-center gap-2">
              <BookOpen className="h-5 w-5 text-blue-500" />
              <h1 className="text-xl font-semibold text-slate-900">New Library Strategy</h1>
            </div>
            <div className="flex items-center gap-1.5">
              <Globe className="h-3 w-3 text-blue-500" />
              <span className="text-xs text-blue-600 font-medium">
                Publishes immediately as shared · active
              </span>
            </div>
          </div>
        }
      />
    </div>
  );
}
