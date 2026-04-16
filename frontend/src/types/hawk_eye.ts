/**
 * Cortex AI — HawkEye Radar Types
 * =================================
 * Typed contracts for the HawkEye multi-timeframe scanner.
 * Replaces `useState<any>(null)` and raw JSON.stringify display.
 */

export type SignalDirection = "buy" | "sell" | "neutral";
export type TimeframeStrength = "strong" | "moderate" | "weak";

export interface HawkEyeSignal {
  name: string;
  direction: SignalDirection;
  value: number;
  timeframe: string;
}

export interface HawkEyeResult {
  instrument_key: string;
  trading_symbol: string;
  exchange: string;
  composite_signal: SignalDirection;
  composite_score: number;
  timeframe_signals: HawkEyeSignal[];
  last_price: number;
  strength: TimeframeStrength;
  agreement_pct: number; // % of timeframes agreeing on direction
  scanned_at: string;
}

export interface HawkEyeResponse {
  results: HawkEyeResult[];
  total: number;
  timeframes_scanned: string[];
  scanned_at: string;
}