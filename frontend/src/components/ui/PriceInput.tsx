"use client";

import * as React from "react";
import { cn } from "@/lib/utils";

// ─── NSE Equity Tick-Size Table ───────────────────────────────────────────────
// Effective 15 April 2025 per NSE circular (equity CM segment).
// Tick enforcement applies only to exchange-submitted prices (limit orders).
// Internal reference levels (SL / TP) are exempt — pass no tickSize for those.
const NSE_TICK_BANDS = [
  { maxPrice: 250,      tick: 0.01 },
  { maxPrice: 1_000,    tick: 0.05 },
  { maxPrice: 5_000,    tick: 0.10 },
  { maxPrice: 25_000,   tick: 0.25 },
  { maxPrice: Infinity, tick: 1.00 },
] as const;

/** Returns the NSE CM-segment tick size for the given instrument price. */
export function getNSETickSize(price: number): number {
  return NSE_TICK_BANDS.find((b) => price <= b.maxPrice)?.tick ?? 1.00;
}

// Integer-math alignment check — avoids float-modulo rounding errors.
// e.g. 147.35 % 0.05 in float-land ≠ 0 due to IEEE 754, but
//      Math.round(14735) % Math.round(5) === 0  ✓
function isTickAligned(value: number, tickSize: number): boolean {
  const dp    = (tickSize.toString().split(".")[1] ?? "").length;
  const scale = 10 ** dp;
  return Math.round(value * scale) % Math.round(tickSize * scale) === 0;
}

// Accepts any intermediate state of a valid INR price (up to 2 decimal places).
// Blocks non-numeric characters and more than 2dp at the keystroke level.
const PRICE_RE = /^\d*\.?\d{0,2}$/;

// ─── Types ────────────────────────────────────────────────────────────────────

type FocusVariant = "neutral" | "danger" | "success";

const FOCUS_CLASSES: Record<FocusVariant, string> = {
  neutral: "focus:border-blue-500    focus:ring-blue-500/20",
  danger:  "focus:border-rose-400    focus:ring-rose-400/20",
  success: "focus:border-emerald-400 focus:ring-emerald-400/20",
};

export interface PriceInputProps
  extends Omit<
    React.InputHTMLAttributes<HTMLInputElement>,
    "type" | "value" | "onChange" | "min" | "max" | "step"
  > {
  /** Controlled raw-string value (may be empty). */
  value: string;
  /** Called with the filtered string on every accepted keystroke. */
  onChange: (value: string) => void;
  /**
   * NSE tick size (₹) for this instrument. When provided, the component
   * validates tick alignment on blur and surfaces the two nearest valid
   * prices. Omit for SL/TP fields — they are internal reference levels
   * and are never submitted directly to the exchange.
   */
  tickSize?: number;
  /**
   * Parent-supplied error (business-logic rules: ordering constraints,
   * insufficient-funds warnings, etc.). Takes display precedence over any
   * local format error when non-empty.
   */
  error?: string;
  /**
   * Focus-ring accent colour. Use "danger" for stop-loss inputs,
   * "success" for take-profit inputs, and "neutral" (default) for
   * limit-price and generic price fields.
   */
  focusVariant?: FocusVariant;
}

// ─── Component ────────────────────────────────────────────────────────────────

export const PriceInput = React.forwardRef<HTMLInputElement, PriceInputProps>(
  function PriceInput(
    {
      value,
      onChange,
      tickSize,
      error,
      focusVariant = "neutral",
      disabled,
      className,
      id,
      ...rest
    },
    ref,
  ) {
    const [fmtError, setFmtError] = React.useState("");

    // Reset local format error whenever the field is externally cleared.
    React.useEffect(() => {
      if (value === "") setFmtError("");
    }, [value]);

    const handleChange = React.useCallback(
      (e: React.ChangeEvent<HTMLInputElement>) => {
        const raw = e.target.value;
        // Accept only characters that can form a valid INR price.
        if (raw === "" || PRICE_RE.test(raw)) {
          if (fmtError) setFmtError("");
          onChange(raw);
        }
      },
      [onChange, fmtError],
    );

    const handleBlur = React.useCallback(() => {
      if (!value) {
        setFmtError("");
        return;
      }

      // Normalise a trailing decimal separator ("147." → "147").
      if (value.endsWith(".")) {
        onChange(value.slice(0, -1));
        setFmtError("");
        return;
      }

      const num = parseFloat(value);
      if (isNaN(num) || num <= 0) {
        setFmtError("Enter a valid positive price.");
        return;
      }

      if (tickSize != null && !isTickAligned(num, tickSize)) {
        const dp    = (tickSize.toString().split(".")[1] ?? "").length;
        const scale = 10 ** dp;
        const ticks = Math.round(tickSize * scale);
        const lower = (Math.floor((num * scale) / ticks) * ticks) / scale;
        const upper = lower + tickSize;
        setFmtError(
          `Must be a ₹${tickSize.toFixed(dp)} multiple — nearest: ₹${lower.toFixed(dp)} or ₹${upper.toFixed(dp)}.`,
        );
        return;
      }

      setFmtError("");
    }, [value, tickSize, onChange]);

    // Parent error takes display precedence over local format error.
    const displayError = error || fmtError;
    const isInvalid    = Boolean(displayError);

    return (
      <>
        <input
          {...rest}
          ref={ref}
          id={id}
          type="text"
          inputMode="decimal"
          value={value}
          onChange={handleChange}
          onBlur={handleBlur}
          disabled={disabled}
          autoComplete="off"
          autoCorrect="off"
          spellCheck={false}
          aria-invalid={isInvalid || undefined}
          aria-describedby={isInvalid && id ? `${id}-err` : undefined}
          className={cn(
            "w-full rounded-xl border px-3 text-sm text-slate-900 placeholder-slate-400 transition",
            "focus:outline-none focus:ring-2",
            disabled && "cursor-not-allowed animate-pulse border-slate-200 bg-slate-50 text-slate-400",
            !disabled && "bg-white",
            !disabled && isInvalid && "border-rose-400 focus:border-rose-400 focus:ring-rose-400/20",
            !disabled && !isInvalid && FOCUS_CLASSES[focusVariant],
            className,
          )}
        />
        {displayError && !disabled && (
          <p
            id={id ? `${id}-err` : undefined}
            role="alert"
            className="mt-1 text-xs text-rose-600"
          >
            {displayError}
          </p>
        )}
      </>
    );
  },
);
