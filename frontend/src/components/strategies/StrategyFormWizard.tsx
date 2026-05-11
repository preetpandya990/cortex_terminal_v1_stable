'use client';

/**
 * StrategyFormWizard — shared 6-step strategy creation form.
 *
 * Used by both the user strategy creation page (/strategies/new) and the
 * admin library creation page (/admin/strategies/new).  The caller supplies
 * an `onSubmit` handler so each context can call the appropriate API endpoint
 * and handle post-submit navigation independently.
 */

import { useState } from 'react';
import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronUp,
  Info,
  Plus,
  RotateCcw,
  Settings2,
  Shield,
  Target,
  TrendingUp,
  Workflow,
  X,
} from 'lucide-react';
import type { CreateStrategyRequest, StrategyDefinition } from '@/types/strategies';
import { cn } from '@/lib/utils';

// ─── Types & defaults ─────────────────────────────────────────────────────────

type Direction = 'BUY' | 'SELL';
type Regime =
  | 'bull_trending'
  | 'bear_trending'
  | 'sideways_range'
  | 'high_volatility'
  | 'low_liquidity'
  | 'trending_up';

export interface FormState {
  // Step 1: Metadata
  name: string;
  description: string;
  tags: string[];

  // Step 2: Signal Gates
  allowedSymbols: string[];
  blockedSymbols: string[];
  allowedDirections: Direction[];
  allowedRegimes: Regime[];
  allowedTimeframes: string[];

  // Step 3: Condition Tree (JSON editor)
  signalConditionJson: string;
  indicatorConditionJson: string;

  // Step 4: Risk (SL/TP)
  slType: 'fixed_pct' | 'atr_multiple' | 'price_level' | '';
  slValue: string;
  tpLevels: Array<{ pct: string; exitFraction: string }>;

  // Step 5: Position Sizing
  sizingMode: 'fixed_qty' | 'risk_pct' | 'kelly';
  fixedQty: string;
  riskPct: string;
  kellyFraction: string;
  maxPositionPct: string;

  // Step 6: Guardrails
  maxConcurrentActivations: string;
  minConfidenceScore: string;
  allowedSessions: string[];
  maxDailyLossPct: string;
}

export const DEFAULT_FORM: FormState = {
  name: '',
  description: '',
  tags: [],
  allowedSymbols: [],
  blockedSymbols: [],
  allowedDirections: ['BUY', 'SELL'],
  allowedRegimes: [],
  allowedTimeframes: [],
  signalConditionJson: '',
  indicatorConditionJson: '',
  slType: 'fixed_pct',
  slValue: '2',
  tpLevels: [
    { pct: '4', exitFraction: '0.5' },
    { pct: '8', exitFraction: '0.5' },
  ],
  sizingMode: 'risk_pct',
  fixedQty: '10',
  riskPct: '2',
  kellyFraction: '0.25',
  maxPositionPct: '10',
  maxConcurrentActivations: '3',
  minConfidenceScore: '60',
  allowedSessions: [],
  maxDailyLossPct: '',
};

// ─── Step config ──────────────────────────────────────────────────────────────

const STEPS = [
  { id: 1, label: 'Metadata', icon: Workflow },
  { id: 2, label: 'Signal Gates', icon: Shield },
  { id: 3, label: 'Conditions', icon: Settings2 },
  { id: 4, label: 'Risk', icon: AlertTriangle },
  { id: 5, label: 'Sizing', icon: TrendingUp },
  { id: 6, label: 'Guardrails', icon: Target },
] as const;

// ─── Field helpers ────────────────────────────────────────────────────────────

function Label({ children, hint }: { children: React.ReactNode; hint?: string }) {
  return (
    <label className="mb-1 block text-xs font-medium text-slate-700">
      {children}
      {hint && <span className="ml-1 font-normal text-slate-400">{hint}</span>}
    </label>
  );
}

function Input({
  value,
  onChange,
  placeholder,
  type = 'text',
  min,
  max,
  step,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  min?: string;
  max?: string;
  step?: string;
}) {
  return (
    <input
      type={type}
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full rounded-md border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
    />
  );
}

function Textarea({
  value,
  onChange,
  placeholder,
  rows = 3,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  rows?: number;
}) {
  return (
    <textarea
      rows={rows}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className="w-full resize-none rounded-md border border-slate-200 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
    />
  );
}

function ToggleChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
        active
          ? 'border-slate-900 bg-slate-900 text-white'
          : 'border-slate-200 text-slate-600 hover:border-slate-400',
      )}
    >
      {label}
    </button>
  );
}

function ChipInput({
  values,
  onChange,
  placeholder,
}: {
  values: string[];
  onChange: (v: string[]) => void;
  placeholder: string;
}) {
  const [input, setInput] = useState('');
  const add = () => {
    const v = input.trim().toUpperCase();
    if (v && !values.includes(v)) onChange([...values, v]);
    setInput('');
  };
  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add();
            }
          }}
          placeholder={placeholder}
          className="flex-1 rounded-md border border-slate-200 px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
        />
        <button
          type="button"
          onClick={add}
          className="rounded-md border border-slate-200 px-2 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
      </div>
      {values.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {values.map((v) => (
            <span
              key={v}
              className="inline-flex items-center gap-1 rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-700"
            >
              {v}
              <button
                onClick={() => onChange(values.filter((x) => x !== v))}
                className="text-slate-400 hover:text-slate-700"
              >
                <X className="h-3 w-3" />
              </button>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Step components ──────────────────────────────────────────────────────────

function Step1({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  const [tagInput, setTagInput] = useState('');
  const addTag = () => {
    const t = tagInput.trim().toLowerCase().replace(/\s+/g, '-');
    if (t && !s.tags.includes(t) && s.tags.length < 10) update({ tags: [...s.tags, t] });
    setTagInput('');
  };
  return (
    <div className="space-y-4">
      <div>
        <Label>Strategy name *</Label>
        <Input
          value={s.name}
          onChange={(v) => update({ name: v })}
          placeholder="e.g. RSI Oversold Swing"
        />
      </div>
      <div>
        <Label hint="(optional)">Description</Label>
        <Textarea
          value={s.description}
          onChange={(v) => update({ description: v })}
          placeholder="Describe what this strategy looks for and when it triggers…"
        />
      </div>
      <div>
        <Label hint="(up to 10, press Enter)">Tags</Label>
        <div className="flex gap-2">
          <input
            value={tagInput}
            onChange={(e) => setTagInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault();
                addTag();
              }
            }}
            placeholder="e.g. swing, rsi, large-cap"
            className="flex-1 rounded-md border border-slate-200 px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-slate-400"
          />
          <button
            type="button"
            onClick={addTag}
            className="rounded-md border border-slate-200 px-2 py-1.5 text-xs text-slate-600 hover:bg-slate-50"
          >
            <Plus className="h-3.5 w-3.5" />
          </button>
        </div>
        {s.tags.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {s.tags.map((t) => (
              <span
                key={t}
                className="inline-flex items-center gap-1 rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
              >
                #{t}
                <button
                  onClick={() => update({ tags: s.tags.filter((x) => x !== t) })}
                  className="text-slate-400 hover:text-slate-700"
                >
                  <X className="h-3 w-3" />
                </button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Step2({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  const regimes: Regime[] = [
    'bull_trending',
    'bear_trending',
    'sideways_range',
    'high_volatility',
    'low_liquidity',
    'trending_up',
  ];
  const timeframes = ['1D', '1hour', '15minute', '5minute'];

  const toggleArr = <T extends string>(arr: T[], val: T): T[] =>
    arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val];

  return (
    <div className="space-y-5">
      <div>
        <Label hint="(empty = all symbols)">Allowed symbols</Label>
        <ChipInput
          values={s.allowedSymbols}
          onChange={(v) => update({ allowedSymbols: v })}
          placeholder="e.g. RELIANCE, TCS"
        />
      </div>
      <div>
        <Label hint="(overrides allowed list)">Blocked symbols</Label>
        <ChipInput
          values={s.blockedSymbols}
          onChange={(v) => update({ blockedSymbols: v })}
          placeholder="e.g. ADANI GROUP"
        />
      </div>
      <div>
        <Label>Signal directions</Label>
        <div className="mt-1 flex gap-2">
          {(['BUY', 'SELL'] as Direction[]).map((d) => (
            <ToggleChip
              key={d}
              label={d}
              active={s.allowedDirections.includes(d)}
              onClick={() =>
                update({ allowedDirections: toggleArr(s.allowedDirections, d) })
              }
            />
          ))}
        </div>
      </div>
      <div>
        <Label hint="(empty = all regimes)">Allowed market regimes</Label>
        <div className="mt-1 flex flex-wrap gap-2">
          {regimes.map((r) => (
            <ToggleChip
              key={r}
              label={r.replace(/_/g, ' ')}
              active={s.allowedRegimes.includes(r)}
              onClick={() =>
                update({ allowedRegimes: toggleArr(s.allowedRegimes, r) })
              }
            />
          ))}
        </div>
      </div>
      <div>
        <Label hint="(empty = all timeframes)">Allowed timeframes</Label>
        <div className="mt-1 flex flex-wrap gap-2">
          {timeframes.map((tf) => (
            <ToggleChip
              key={tf}
              label={tf}
              active={s.allowedTimeframes.includes(tf)}
              onClick={() =>
                update({ allowedTimeframes: toggleArr(s.allowedTimeframes, tf) })
              }
            />
          ))}
        </div>
      </div>
    </div>
  );
}

const CONDITION_HINT = `Optional. Leave blank to skip.
Example (AND — RSI < 40 and action == BUY):
{
  "type": "group",
  "operator": "AND",
  "children": [
    {"type": "leaf", "field": "rsi_14", "op": "<", "value": 40},
    {"type": "leaf", "field": "action",  "op": "==","value": "BUY"}
  ]
}`;

function Step3({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  const validate = (json: string): boolean => {
    if (!json.trim()) return true;
    try {
      JSON.parse(json);
      return true;
    } catch {
      return false;
    }
  };
  const sigErr = s.signalConditionJson && !validate(s.signalConditionJson);
  const indErr = s.indicatorConditionJson && !validate(s.indicatorConditionJson);

  return (
    <div className="space-y-5">
      <div className="rounded-lg border border-blue-100 bg-blue-50 px-4 py-3 text-xs text-blue-800">
        <div className="flex gap-2">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <p>
            Conditions are evaluated against the signal's market context fields:
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">rsi_14</code>
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">ema_20</code>
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">ema_50</code>
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">confidence_score</code>
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">action</code>
            <code className="mx-1 rounded bg-blue-100 px-1 font-mono">ltp</code>
            and more. Leave blank to skip.
          </p>
        </div>
      </div>
      <div>
        <Label hint="(optional JSON condition tree)">Signal condition</Label>
        <Textarea
          rows={8}
          value={s.signalConditionJson}
          onChange={(v) => update({ signalConditionJson: v })}
          placeholder={CONDITION_HINT}
        />
        {sigErr && <p className="mt-1 text-xs text-rose-600">Invalid JSON</p>}
      </div>
      <div>
        <Label hint="(optional JSON condition tree)">Indicator condition</Label>
        <Textarea
          rows={6}
          value={s.indicatorConditionJson}
          onChange={(v) => update({ indicatorConditionJson: v })}
          placeholder="Optional — evaluated against the same market context"
        />
        {indErr && <p className="mt-1 text-xs text-rose-600">Invalid JSON</p>}
      </div>
    </div>
  );
}

function Step4({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  const addTp = () =>
    update({ tpLevels: [...s.tpLevels, { pct: '', exitFraction: '1' }] });
  const removeTp = (i: number) =>
    update({ tpLevels: s.tpLevels.filter((_, idx) => idx !== i) });
  const updateTp = (i: number, field: 'pct' | 'exitFraction', val: string) => {
    const lvls = [...s.tpLevels];
    lvls[i] = { ...lvls[i], [field]: val };
    update({ tpLevels: lvls });
  };

  return (
    <div className="space-y-5">
      <div>
        <Label>Stop-loss type</Label>
        <div className="mt-1 flex gap-2">
          {(['fixed_pct', 'atr_multiple', 'price_level'] as const).map((t) => (
            <ToggleChip
              key={t}
              label={t.replace(/_/g, ' ')}
              active={s.slType === t}
              onClick={() => update({ slType: t })}
            />
          ))}
        </div>
      </div>
      {s.slType && (
        <div>
          <Label>
            {s.slType === 'fixed_pct'
              ? 'SL % from entry'
              : s.slType === 'atr_multiple'
              ? 'ATR multiplier'
              : 'SL price level (₹)'}
          </Label>
          <Input
            type="number"
            min="0.1"
            step="0.1"
            value={s.slValue}
            onChange={(v) => update({ slValue: v })}
            placeholder={
              s.slType === 'fixed_pct'
                ? 'e.g. 2 (= 2%)'
                : s.slType === 'atr_multiple'
                ? 'e.g. 1.5'
                : 'e.g. 2850'
            }
          />
        </div>
      )}

      <div className="border-t border-slate-100 pt-4">
        <div className="mb-2 flex items-center justify-between">
          <Label>Take-profit levels</Label>
          {s.tpLevels.length < 3 && (
            <button
              onClick={addTp}
              className="flex items-center gap-1 text-xs text-slate-500 hover:text-slate-900"
            >
              <Plus className="h-3 w-3" /> Add level
            </button>
          )}
        </div>
        <div className="space-y-2">
          {s.tpLevels.map((tp, i) => (
            <div key={i} className="flex items-center gap-2">
              <span className="w-6 shrink-0 text-xs text-slate-400">TP{i + 1}</span>
              <div className="flex-1">
                <Input
                  type="number"
                  min="0.1"
                  step="0.5"
                  value={tp.pct}
                  onChange={(v) => updateTp(i, 'pct', v)}
                  placeholder="% move (e.g. 4)"
                />
              </div>
              <div className="w-28">
                <Input
                  type="number"
                  min="0.01"
                  max="1"
                  step="0.01"
                  value={tp.exitFraction}
                  onChange={(v) => updateTp(i, 'exitFraction', v)}
                  placeholder="exit fraction"
                />
              </div>
              <button
                onClick={() => removeTp(i)}
                className="text-slate-400 hover:text-rose-600"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
          {s.tpLevels.length === 0 && (
            <p className="text-xs text-slate-400">
              No TP levels — position held until manual exit or SL.
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function Step5({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  return (
    <div className="space-y-5">
      <div>
        <Label>Sizing mode</Label>
        <div className="mt-1 flex gap-2">
          {(['fixed_qty', 'risk_pct', 'kelly'] as const).map((m) => (
            <ToggleChip
              key={m}
              label={m === 'risk_pct' ? '% risk' : m === 'fixed_qty' ? 'Fixed qty' : 'Kelly'}
              active={s.sizingMode === m}
              onClick={() => update({ sizingMode: m })}
            />
          ))}
        </div>
      </div>
      {s.sizingMode === 'fixed_qty' && (
        <div>
          <Label>Fixed quantity (shares)</Label>
          <Input
            type="number"
            min="1"
            step="1"
            value={s.fixedQty}
            onChange={(v) => update({ fixedQty: v })}
          />
        </div>
      )}
      {s.sizingMode === 'risk_pct' && (
        <div>
          <Label hint="(% of portfolio to risk per trade)">Risk per trade</Label>
          <Input
            type="number"
            min="0.1"
            max="5"
            step="0.1"
            value={s.riskPct}
            onChange={(v) => update({ riskPct: v })}
            placeholder="e.g. 2 (= 2%)"
          />
        </div>
      )}
      {s.sizingMode === 'kelly' && (
        <div>
          <Label hint="(fraction of full Kelly — 0.25 = quarter Kelly)">Kelly fraction</Label>
          <Input
            type="number"
            min="0.05"
            max="1"
            step="0.05"
            value={s.kellyFraction}
            onChange={(v) => update({ kellyFraction: v })}
            placeholder="e.g. 0.25"
          />
        </div>
      )}
      <div>
        <Label hint="(hard cap)">Max position size (% of portfolio)</Label>
        <Input
          type="number"
          min="1"
          max="25"
          step="1"
          value={s.maxPositionPct}
          onChange={(v) => update({ maxPositionPct: v })}
          placeholder="e.g. 10"
        />
      </div>
    </div>
  );
}

function Step6({ s, update }: { s: FormState; update: (p: Partial<FormState>) => void }) {
  return (
    <div className="space-y-5">
      <div>
        <Label hint="(concurrent activations for this strategy)">
          Max concurrent activations
        </Label>
        <Input
          type="number"
          min="1"
          max="20"
          step="1"
          value={s.maxConcurrentActivations}
          onChange={(v) => update({ maxConcurrentActivations: v })}
        />
      </div>
      <div>
        <Label hint="(0–100)">Minimum signal confidence score</Label>
        <Input
          type="number"
          min="0"
          max="100"
          step="1"
          value={s.minConfidenceScore}
          onChange={(v) => update({ minConfidenceScore: v })}
        />
      </div>
      <div>
        <Label hint="(empty = any session)">Allowed trading sessions</Label>
        <div className="mt-1 flex flex-wrap gap-2">
          {(['morning', 'midday', 'afternoon'] as const).map((sess) => (
            <ToggleChip
              key={sess}
              label={sess}
              active={s.allowedSessions.includes(sess)}
              onClick={() => {
                const cur = s.allowedSessions;
                update({
                  allowedSessions: cur.includes(sess)
                    ? cur.filter((x) => x !== sess)
                    : [...cur, sess],
                });
              }}
            />
          ))}
        </div>
      </div>
      <div>
        <Label hint="(strategy auto-pauses when daily P&L < -this%)">
          Max daily loss % (optional)
        </Label>
        <Input
          type="number"
          min="0.1"
          max="10"
          step="0.1"
          value={s.maxDailyLossPct}
          onChange={(v) => update({ maxDailyLossPct: v })}
          placeholder="e.g. 3"
        />
      </div>
    </div>
  );
}

// ─── Build payload ────────────────────────────────────────────────────────────

export function buildPayload(s: FormState): CreateStrategyRequest {
  const parseJson = (raw: string) => {
    if (!raw.trim()) return undefined;
    try {
      return JSON.parse(raw);
    } catch {
      return undefined;
    }
  };

  const slCfg =
    s.slType && s.slValue
      ? { type: s.slType, value: parseFloat(s.slValue), atr_period: 14, trail: false }
      : undefined;

  const tpLevels = s.tpLevels
    .filter((tp) => tp.pct && tp.exitFraction)
    .map((tp) => ({
      pct: parseFloat(tp.pct),
      exit_fraction: parseFloat(tp.exitFraction),
    }));

  const { sizingMode, fixedQty, riskPct, kellyFraction, maxPositionPct } = s;
  const sizingBase = { mode: sizingMode, max_position_pct: parseFloat(maxPositionPct) || 10 };
  const position_sizing: StrategyDefinition['position_sizing'] =
    sizingMode === 'fixed_qty'
      ? { ...sizingBase, fixed_qty: parseInt(fixedQty) || 10 }
      : sizingMode === 'risk_pct'
      ? { ...sizingBase, risk_pct: parseFloat(riskPct) || 2 }
      : { ...sizingBase, kelly_fraction: parseFloat(kellyFraction) || 0.25 };

  const definition: StrategyDefinition = {
    signal_gates: {
      allowed_symbols: s.allowedSymbols,
      blocked_symbols: s.blockedSymbols,
      allowed_directions: s.allowedDirections.length
        ? s.allowedDirections
        : ['BUY', 'SELL'],
      allowed_regimes: s.allowedRegimes,
      allowed_timeframes: s.allowedTimeframes,
      min_market_cap_cr: null,
    },
    signal_condition: parseJson(s.signalConditionJson) ?? null,
    indicator_condition: parseJson(s.indicatorConditionJson) ?? null,
    stop_loss: slCfg ?? null,
    take_profit: tpLevels.length > 0 ? { levels: tpLevels } : null,
    position_sizing,
    guardrails: {
      max_concurrent_activations: parseInt(s.maxConcurrentActivations) || 3,
      min_confidence_score: parseFloat(s.minConfidenceScore) || 60,
      allowed_sessions: s.allowedSessions,
      require_volume_confirm: false,
      max_daily_loss_pct: s.maxDailyLossPct ? parseFloat(s.maxDailyLossPct) : null,
    },
  };

  return {
    name: s.name,
    description: s.description || null,
    tags: s.tags,
    definition,
  };
}

// ─── Validation ───────────────────────────────────────────────────────────────

export function validateStep(step: number, s: FormState): string | null {
  if (step === 1) {
    if (!s.name.trim()) return 'Strategy name is required';
    if (s.name.trim().length < 3) return 'Name must be at least 3 characters';
  }
  if (step === 2) {
    if (s.allowedDirections.length === 0)
      return 'Select at least one signal direction (BUY or SELL)';
  }
  if (step === 3) {
    const check = (json: string) => {
      try {
        JSON.parse(json);
      } catch {
        return false;
      }
      return true;
    };
    if (s.signalConditionJson.trim() && !check(s.signalConditionJson))
      return 'Signal condition JSON is invalid';
    if (s.indicatorConditionJson.trim() && !check(s.indicatorConditionJson))
      return 'Indicator condition JSON is invalid';
  }
  if (step === 4) {
    if (s.slType && !s.slValue) return 'Enter a stop-loss value';
  }
  return null;
}

// ─── Wizard Component ─────────────────────────────────────────────────────────

export interface StrategyFormWizardProps {
  /** Called with the validated payload on final step submit. */
  onSubmit: (payload: CreateStrategyRequest) => Promise<void>;
  /** Called when the back button on step 1 is clicked. */
  onBack: () => void;
  /** Optional label override for the final submit button. */
  submitLabel?: string;
  /** Optional banner shown below the title (e.g. a "Library" scope badge). */
  headerSlot?: React.ReactNode;
  /** Pre-populate the form for edit mode. */
  initialForm?: Partial<FormState>;
}

export function StrategyFormWizard({
  onSubmit,
  onBack,
  submitLabel = 'Create strategy',
  headerSlot,
  initialForm,
}: StrategyFormWizardProps) {
  const [step, setStep] = useState(1);
  const [form, setForm] = useState<FormState>({ ...DEFAULT_FORM, ...initialForm });
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');

  const update = (patch: Partial<FormState>) => setForm((f) => ({ ...f, ...patch }));
  const stepError = validateStep(step, form);

  const next = () => {
    if (!stepError) setStep((s) => Math.min(s + 1, STEPS.length));
  };
  const prev = () => {
    if (step === 1) {
      onBack();
    } else {
      setStep((s) => Math.max(s - 1, 1));
    }
  };

  const submit = async () => {
    for (let i = 1; i <= STEPS.length; i++) {
      const err = validateStep(i, form);
      if (err) {
        setStep(i);
        setSubmitError(err);
        return;
      }
    }
    setSubmitting(true);
    setSubmitError('');
    try {
      await onSubmit(buildPayload(form));
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '';
      setSubmitError(msg || 'Failed to create strategy. Please try again.');
      setSubmitting(false);
    }
  };

  const stepComponents: React.ReactNode[] = [
    <Step1 key={1} s={form} update={update} />,
    <Step2 key={2} s={form} update={update} />,
    <Step3 key={3} s={form} update={update} />,
    <Step4 key={4} s={form} update={update} />,
    <Step5 key={5} s={form} update={update} />,
    <Step6 key={6} s={form} update={update} />,
  ];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <button
            onClick={prev}
            className="mb-2 flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
          >
            <ArrowLeft className="h-3.5 w-3.5" /> Back
          </button>
          {headerSlot}
        </div>
        <button
          onClick={() => setForm(DEFAULT_FORM)}
          className="flex items-center gap-1 text-xs text-slate-400 hover:text-slate-700"
        >
          <RotateCcw className="h-3.5 w-3.5" /> Reset
        </button>
      </div>

      {/* Step progress */}
      <div className="relative flex items-start">
        {STEPS.map((s, idx) => {
          const Icon = s.icon;
          const done = step > s.id;
          const active = step === s.id;
          return (
            <div key={s.id} className="flex flex-1 flex-col items-center">
              <div className="relative flex w-full items-center justify-center">
                {idx > 0 && (
                  <div
                    className={cn(
                      'absolute right-1/2 top-4 h-px w-full -translate-y-1/2',
                      step > s.id ? 'bg-emerald-400' : 'bg-slate-200',
                    )}
                  />
                )}
                <button
                  onClick={() => step > s.id && setStep(s.id)}
                  disabled={step <= s.id}
                  className={cn(
                    'relative z-10 flex h-8 w-8 items-center justify-center rounded-full border-2 text-xs font-semibold transition-colors',
                    done
                      ? 'border-emerald-500 bg-emerald-500 text-white'
                      : active
                      ? 'border-slate-900 bg-slate-900 text-white'
                      : 'border-slate-200 bg-white text-slate-400',
                  )}
                >
                  {done ? <Check className="h-3.5 w-3.5" /> : <Icon className="h-3.5 w-3.5" />}
                </button>
              </div>
              <span
                className={cn(
                  'mt-1 text-[10px] font-medium',
                  active ? 'text-slate-900' : 'text-slate-400',
                )}
              >
                {s.label}
              </span>
            </div>
          );
        })}
      </div>

      {/* Step card */}
      <div className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <h2 className="mb-5 text-sm font-semibold text-slate-900">
          {STEPS[step - 1].label}
        </h2>
        {stepComponents[step - 1]}
      </div>

      {/* Errors */}
      {submitError && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-4 py-2.5 text-xs text-rose-700">
          {submitError}
        </p>
      )}
      {stepError && (
        <p className="flex items-center gap-1.5 text-xs text-amber-700">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" /> {stepError}
        </p>
      )}

      {/* Navigation */}
      <div className="flex items-center justify-between">
        <button
          onClick={prev}
          className="flex items-center gap-1.5 rounded-md border border-slate-200 px-4 py-2 text-xs font-medium text-slate-600 hover:bg-slate-50"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          {step === 1 ? 'Cancel' : 'Previous'}
        </button>

        {step < STEPS.length ? (
          <button
            onClick={next}
            disabled={!!stepError}
            className="flex items-center gap-1.5 rounded-md bg-slate-900 px-4 py-2 text-xs font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            Next <ArrowRight className="h-3.5 w-3.5" />
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={submitting || !!stepError}
            className="flex items-center gap-1.5 rounded-md bg-emerald-600 px-5 py-2 text-xs font-semibold text-white hover:bg-emerald-700 disabled:opacity-50"
          >
            {submitting ? (
              'Saving…'
            ) : (
              <>
                <Check className="h-3.5 w-3.5" /> {submitLabel}
              </>
            )}
          </button>
        )}
      </div>
    </div>
  );
}
