'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { ArrowLeft, Loader2 } from 'lucide-react';
import { useAuth } from '@/contexts/AuthContext';
import { strategiesAPI } from '@/lib/api';
import {
  DEFAULT_FORM,
  StrategyFormWizard,
  type FormState,
} from '@/components/strategies/StrategyFormWizard';
import type { Strategy, UpdateStrategyRequest } from '@/types/strategies';

// Map a loaded Strategy back into the wizard's FormState
function strategyToFormState(s: Strategy): FormState {
  const def = s.definition;
  const sl = def.stop_loss;
  const tp = def.take_profit;
  const ps = def.position_sizing;
  const sg = def.signal_gates;

  const tpLevels =
    tp?.levels.map((l) => ({
      pct: String(l.pct),
      exitFraction: String(l.exit_fraction),
    })) ?? DEFAULT_FORM.tpLevels;

  return {
    name: s.name,
    description: s.description ?? '',
    tags: s.tags ?? [],

    allowedSymbols: sg.allowed_symbols ?? [],
    blockedSymbols: sg.blocked_symbols ?? [],
    allowedDirections: (sg.allowed_directions ?? ['BUY', 'SELL']) as FormState['allowedDirections'],
    allowedRegimes: (sg.allowed_regimes ?? []) as FormState['allowedRegimes'],
    allowedTimeframes: sg.allowed_timeframes ?? [],

    signalConditionJson: def.signal_condition
      ? JSON.stringify(def.signal_condition, null, 2)
      : '',
    indicatorConditionJson: def.indicator_condition
      ? JSON.stringify(def.indicator_condition, null, 2)
      : '',

    slType: sl?.type ?? 'fixed_pct',
    slValue: sl?.value != null ? String(sl.value) : '2',
    tpLevels,

    sizingMode: ps?.mode ?? 'risk_pct',
    fixedQty: ps && 'fixed_qty' in ps ? String(ps.fixed_qty) : DEFAULT_FORM.fixedQty,
    riskPct: ps && 'risk_pct' in ps ? String(ps.risk_pct) : DEFAULT_FORM.riskPct,
    kellyFraction: ps && 'kelly_fraction' in ps ? String(ps.kelly_fraction) : DEFAULT_FORM.kellyFraction,
    maxPositionPct: ps?.max_position_pct != null ? String(ps.max_position_pct) : DEFAULT_FORM.maxPositionPct,

    maxConcurrentActivations: String(def.guardrails?.max_concurrent_activations ?? 3),
    minConfidenceScore: String(def.guardrails?.min_confidence_score ?? 60),
    allowedSessions: def.guardrails?.allowed_sessions ?? [],
    maxDailyLossPct: def.guardrails?.max_daily_loss_pct != null
      ? String(def.guardrails.max_daily_loss_pct)
      : '',
  };
}

export default function EditStrategyPage() {
  const { isAuthenticated, isAuthReady, user } = useAuth();
  const router = useRouter();
  const params = useParams<{ id: string }>();
  const strategyId = params.id;

  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (isAuthReady && !isAuthenticated) router.replace('/login');
  }, [isAuthReady, isAuthenticated, router]);

  useEffect(() => {
    if (!isAuthenticated || !strategyId) return;
    strategiesAPI.get(strategyId)
      .then((s) => {
        // Only the owner can edit
        if (user && s.created_by !== (user as { id: number }).id) {
          router.replace(`/strategies/${strategyId}`);
          return;
        }
        setStrategy(s);
      })
      .catch(() => setError('Failed to load strategy.'))
      .finally(() => setLoading(false));
  }, [isAuthenticated, strategyId, user, router]);

  const handleSubmit = async (payload: UpdateStrategyRequest) => {
    await strategiesAPI.update(strategyId, payload);
    router.push(`/strategies/${strategyId}`);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
      </div>
    );
  }

  if (error || !strategy) {
    return (
      <div className="py-20 text-center">
        <p className="text-sm text-rose-600">{error ?? 'Strategy not found.'}</p>
        <Link href="/strategies" className="mt-4 inline-block text-xs text-slate-500 underline">
          ← Back to Strategies
        </Link>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl py-8">
      <Link
        href={`/strategies/${strategyId}`}
        className="mb-5 inline-flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
      >
        <ArrowLeft className="h-3.5 w-3.5" />
        Back to strategy
      </Link>

      <StrategyFormWizard
        initialForm={strategyToFormState(strategy)}
        onSubmit={handleSubmit}
        onBack={() => router.push(`/strategies/${strategyId}`)}
        submitLabel="Save changes"
      />
    </div>
  );
}
