'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { Workflow } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { strategiesAPI } from '@/lib/api';
import { StrategyFormWizard } from '@/components/strategies/StrategyFormWizard';
import type { CreateStrategyRequest } from '@/types/strategies';

export default function NewStrategyPage() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  const handleSubmit = async (payload: CreateStrategyRequest) => {
    const strategy = await strategiesAPI.create(payload);
    router.push(`/strategies/${strategy.id}`);
  };

  return (
    <div className="mx-auto max-w-2xl py-8">
      <h1 className="mb-6 text-xl font-semibold text-slate-900">New Strategy</h1>
      <StrategyFormWizard
        onSubmit={handleSubmit}
        onBack={() => router.back()}
        submitLabel="Create strategy"
        headerSlot={
          <div className="flex items-center gap-2">
            <Workflow className="h-4 w-4 text-slate-400" />
            <span className="text-xl font-semibold text-slate-900">New Strategy</span>
          </div>
        }
      />
    </div>
  );
}
