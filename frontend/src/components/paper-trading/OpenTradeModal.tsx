"use client";

import { useEffect, useState } from "react";
import {
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  Loader2,
  Sparkles,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import { usePortfolioSummary, usePlaceOrder, usePriceTargets } from "@/hooks/usePaperTrading";
import { useToast } from "@/components/ui/toast";
import { PriceInput, getNSETickSize } from "@/components/ui/PriceInput";
import type {
  OrderType,
  PlaceOrderRequest,
  ProductType,
  TransactionType,
} from "@/types/paper_trading";
import type { TradeSuggestion } from "@/types/trade_suggestions";

// ─── Types ────────────────────────────────────────────────────────────────────

interface InstrumentRef {
  name: string;
  trading_symbol: string;
  exchange?: string;
  instrument_key: string;
}

interface OpenTradeModalProps {
  instrument: InstrumentRef;
  /** Present when opened from a Hawk-Eye signal — unlocks full audit trail. */
  suggestion?: TradeSuggestion;
  /** Current live price from market feed. */
  livePrice?: number | null;
  onClose: () => void;
  onPlaced?: () => void;
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

const INR = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

function fmt(v: number | null | undefined): string {
  if (v == null) return "—";
  return INR.format(v);
}

// ─── Component ────────────────────────────────────────────────────────────────

export function OpenTradeModal({
  instrument,
  suggestion,
  livePrice,
  onClose,
  onPlaced,
}: OpenTradeModalProps) {
  const toast = useToast();
  const { mutateAsync: placeOrder, isPending, error: mutationError } = usePlaceOrder();

  const { data: portfolio, isLoading: portfolioLoading, error: portfolioError } =
    usePortfolioSummary();

  // ── Form state ────────────────────────────────────────────────────────────
  const suggestedSide: TransactionType =
    suggestion?.signal_direction === "SELL" ? "SELL" : "BUY";

  const [side, setSide]             = useState<TransactionType>(suggestedSide);
  const [orderType, setOrderType]   = useState<OrderType>("MARKET");
  const [productType]               = useState<ProductType>("CNC");
  const [quantity, setQuantity]     = useState<string>("1");
  const [limitPrice, setLimitPrice] = useState<string>(
    livePrice != null ? livePrice.toFixed(2) : "",
  );
  const [stopLoss,    setStopLoss]    = useState<string>(
    suggestion?.stop_loss != null ? String(suggestion.stop_loss) : "",
  );
  const [takeProfit1, setTakeProfit1] = useState<string>(
    suggestion?.take_profit_1 != null ? String(suggestion.take_profit_1) : "",
  );
  const [takeProfit2, setTakeProfit2] = useState<string>(
    suggestion?.take_profit_2 != null ? String(suggestion.take_profit_2) : "",
  );
  const [takeProfit3, setTakeProfit3] = useState<string>(
    suggestion?.take_profit_3 != null ? String(suggestion.take_profit_3) : "",
  );
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  // True when SL/TP fields were populated by the ML model and not yet edited.
  const [isAiSuggested, setIsAiSuggested] = useState(false);

  // ── Derived values ────────────────────────────────────────────────────────
  const refPrice =
    orderType === "LIMIT"
      ? parseFloat(limitPrice) || 0
      : (livePrice ?? suggestion?.entry_price ?? 0);

  const qty           = parseInt(quantity, 10) || 0;
  const estimatedCost = refPrice * qty;
  const cash          = portfolio?.current_cash ?? 0;
  const cashAfter     = side === "BUY" ? cash - estimatedCost : cash + estimatedCost;
  const insufficientFunds = side === "BUY" && estimatedCost > cash && qty > 0;

  // ── ML price targets (manual trades only) ────────────────────────────────
  const { data: priceTargets, isLoading: priceTargetsLoading } = usePriceTargets(
    !suggestion ? instrument.trading_symbol : null,
    side,
    refPrice,
    !suggestion && refPrice > 0,
  );

  // Keep limitPrice in sync when live price arrives after mount.
  useEffect(() => {
    if (livePrice != null && limitPrice === "") {
      setLimitPrice(livePrice.toFixed(2));
    }
  }, [livePrice, limitPrice]);

  // Sync SL/TP from suggestion if it arrives after mount (async Hawk-Eye load).
  useEffect(() => {
    if (!suggestion) return;
    if (suggestion.stop_loss    != null) setStopLoss(String(suggestion.stop_loss));
    if (suggestion.take_profit_1 != null) setTakeProfit1(String(suggestion.take_profit_1));
    if (suggestion.take_profit_2 != null) setTakeProfit2(String(suggestion.take_profit_2));
    if (suggestion.take_profit_3 != null) setTakeProfit3(String(suggestion.take_profit_3));
  }, [suggestion]);

  // Auto-fill SL/TP from ML price targets for manual trades.
  useEffect(() => {
    if (!priceTargets) return;
    setStopLoss(String(priceTargets.stop_loss));
    setTakeProfit1(String(priceTargets.take_profit_1));
    setTakeProfit2(String(priceTargets.take_profit_2));
    setTakeProfit3(String(priceTargets.take_profit_3));
    setIsAiSuggested(true);
  }, [priceTargets]);

  // ── Validation ────────────────────────────────────────────────────────────
  function validate(): boolean {
    const errors: Record<string, string> = {};

    const q = parseInt(quantity, 10);
    if (!q || q < 1) errors.quantity = "Enter a valid quantity (≥ 1).";

    if (orderType === "LIMIT") {
      const p = parseFloat(limitPrice);
      if (isNaN(p) || p <= 0) errors.limitPrice = "Enter a valid limit price.";
    }

    if (insufficientFunds) {
      errors.funds = `Insufficient cash. Need ${fmt(estimatedCost)}, have ${fmt(cash)}.`;
    }

    // Defence-in-depth: gate submission on invalid values even if PriceInput
    // blur validation was never triggered (e.g. programmatic fills).
    const sl  = stopLoss    !== "" ? parseFloat(stopLoss)    : null;
    const tp1 = takeProfit1 !== "" ? parseFloat(takeProfit1) : null;
    const tp2 = takeProfit2 !== "" ? parseFloat(takeProfit2) : null;
    const tp3 = takeProfit3 !== "" ? parseFloat(takeProfit3) : null;

    if (sl  != null && (isNaN(sl)  || sl  <= 0)) errors.stopLoss    = "Stop loss must be a positive price.";
    if (tp1 != null && (isNaN(tp1) || tp1 <= 0)) errors.takeProfit1 = "Target 1 must be a positive price.";
    if (tp2 != null && (isNaN(tp2) || tp2 <= 0)) errors.takeProfit2 = "Target 2 must be a positive price.";
    if (tp3 != null && (isNaN(tp3) || tp3 <= 0)) errors.takeProfit3 = "Target 3 must be a positive price.";

    // Ordering constraints (only checked when positivity passes).
    if (!errors.takeProfit2 && tp2 != null && tp1 == null) errors.takeProfit2 = "Set Target 1 before Target 2.";
    if (!errors.takeProfit3 && tp3 != null && tp2 == null) errors.takeProfit3 = "Set Target 2 before Target 3.";

    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  // ── Submit ────────────────────────────────────────────────────────────────
  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!validate()) return;

    const sl  = stopLoss    !== "" ? parseFloat(stopLoss)    : undefined;
    const tp1 = takeProfit1 !== "" ? parseFloat(takeProfit1) : undefined;
    const tp2 = takeProfit2 !== "" ? parseFloat(takeProfit2) : undefined;
    const tp3 = takeProfit3 !== "" ? parseFloat(takeProfit3) : undefined;

    const riskFields = {
      ...(sl  != null && { stop_loss:    sl  }),
      ...(tp1 != null && { take_profit_1: tp1 }),
      ...(tp2 != null && { take_profit_2: tp2 }),
      ...(tp3 != null && { take_profit_3: tp3 }),
    };

    const payload: PlaceOrderRequest = suggestion
      ? {
          suggestion_id: suggestion.suggestion_id,
          transaction_type: side,
          product_type: productType,
          order_type: orderType,
          quantity: parseInt(quantity, 10),
          ...(orderType === "LIMIT" && { price: parseFloat(limitPrice) }),
          ...riskFields,
        }
      : {
          symbol:          instrument.trading_symbol,
          instrument_key:  instrument.instrument_key,
          entry_price:     refPrice || undefined,
          transaction_type: side,
          product_type:    productType,
          order_type:      orderType,
          quantity:        parseInt(quantity, 10),
          ...(orderType === "LIMIT" && { price: parseFloat(limitPrice) }),
          ...riskFields,
        };

    try {
      await placeOrder(payload);
      toast.success(
        `${side === "BUY" ? "Buy" : "Sell"} order placed`,
        `${qty} × ${instrument.trading_symbol} · ${orderType}`,
      );
      onPlaced?.();
      onClose();
    } catch {
      // error surfaced via mutationError below
    }
  }

  // ── Derived UI state ──────────────────────────────────────────────────────
  const serverError = mutationError
    ? ((mutationError as { message?: string })?.message ?? "Failed to place order.")
    : null;

  const noPortfolio =
    !portfolioLoading &&
    (portfolioError != null || portfolio == null);

  const limitPriceTickSize = refPrice > 0 ? getNSETickSize(refPrice) : undefined;
  const riskFieldsDisabled = priceTargetsLoading && !suggestion;

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-md rounded-2xl border border-slate-200 bg-white shadow-2xl">

        {/* ── Header ─────────────────────────────────────────────────────── */}
        <div className="flex items-center justify-between border-b border-slate-100 px-6 py-4">
          <div className="flex items-center gap-3">
            <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-slate-100">
              {side === "BUY"
                ? <TrendingUp  className="h-4 w-4 text-emerald-600" />
                : <TrendingDown className="h-4 w-4 text-rose-600" />}
            </span>
            <div>
              <h2 className="text-base font-semibold text-slate-900">
                Paper Trade — {instrument.trading_symbol}
              </h2>
              <p className="text-xs text-slate-400">
                {instrument.name}
                {instrument.exchange && (
                  <span className="ml-1.5 rounded bg-slate-100 px-1 py-0.5 text-[10px] font-medium text-slate-500">
                    {instrument.exchange}
                  </span>
                )}
                {!suggestion && (
                  <span className="ml-2 rounded border border-amber-200 bg-amber-50 px-1.5 py-0.5 text-[10px] font-medium text-amber-600">
                    Manual
                  </span>
                )}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="rounded-lg p-1.5 text-slate-400 transition-colors hover:bg-slate-100 hover:text-slate-600"
            aria-label="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        {/* ── No portfolio guard ────────────────────────────────────────── */}
        {noPortfolio && (
          <div className="px-6 py-8 text-center">
            <p className="text-sm font-medium text-slate-700">No paper portfolio found.</p>
            <p className="mt-1 text-xs text-slate-400">
              Create a portfolio from the dashboard first.
            </p>
            <button
              onClick={onClose}
              className="mt-4 rounded-xl border border-slate-200 px-4 py-2 text-xs font-semibold text-slate-600 transition hover:bg-slate-50"
            >
              Dismiss
            </button>
          </div>
        )}

        {/* ── Form ─────────────────────────────────────────────────────────── */}
        {!noPortfolio && (
          <form onSubmit={handleSubmit} className="space-y-4 px-6 py-5">

            {/* Signal context banner */}
            {suggestion && (
              <div className="flex items-center gap-3 rounded-xl border border-slate-100 bg-slate-50 px-3 py-2.5">
                <div className="min-w-0 flex-1">
                  <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Signal</p>
                  <p className="truncate text-sm font-medium text-slate-800">
                    {suggestion.signal_direction} · {suggestion.confidence_level} confidence
                    {suggestion.entry_price != null && (
                      <span className="ml-2 text-slate-500">@ {fmt(suggestion.entry_price)}</span>
                    )}
                  </p>
                </div>
                {suggestion.stop_loss != null && (
                  <div className="shrink-0 text-right">
                    <p className="text-[10px] font-semibold uppercase text-slate-400">SL</p>
                    <p className="text-xs font-semibold text-rose-600">{fmt(suggestion.stop_loss)}</p>
                  </div>
                )}
                {suggestion.take_profit_1 != null && (
                  <div className="shrink-0 text-right">
                    <p className="text-[10px] font-semibold uppercase text-slate-400">TP1</p>
                    <p className="text-xs font-semibold text-emerald-600">{fmt(suggestion.take_profit_1)}</p>
                  </div>
                )}
              </div>
            )}

            {/* Server error */}
            {serverError && (
              <div className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-sm text-rose-700">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                {serverError}
              </div>
            )}

            {/* Live price strip */}
            <div className="flex items-center justify-between rounded-lg border border-slate-200 bg-slate-50 px-3 py-2.5">
              <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                {livePrice != null ? "Live Price" : "Ref. Price"}
              </span>
              <span className="text-sm font-bold text-slate-900">
                {livePrice != null
                  ? fmt(livePrice)
                  : suggestion?.entry_price != null
                  ? fmt(suggestion.entry_price)
                  : "—"}
              </span>
            </div>

            {/* BUY / SELL toggle */}
            <div className="space-y-1.5">
              <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
                Direction
              </label>
              <div className="flex gap-2">
                {(["BUY", "SELL"] as TransactionType[]).map((dir) => (
                  <button
                    key={dir}
                    type="button"
                    onClick={() => setSide(dir)}
                    className={`flex flex-1 items-center justify-center gap-1.5 rounded-xl border py-2.5 text-sm font-semibold transition ${
                      side === dir
                        ? dir === "BUY"
                          ? "border-emerald-500 bg-emerald-600 text-white shadow-sm"
                          : "border-rose-500 bg-rose-600 text-white shadow-sm"
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {dir === "BUY"
                      ? <ArrowUpRight   className="h-4 w-4" />
                      : <ArrowDownRight className="h-4 w-4" />}
                    {dir}
                  </button>
                ))}
              </div>
            </div>

            {/* Order type */}
            <div className="space-y-1.5">
              <label className="block text-xs font-semibold uppercase tracking-wide text-slate-500">
                Order Type
              </label>
              <div className="flex gap-2">
                {(["MARKET", "LIMIT"] as OrderType[]).map((type) => (
                  <button
                    key={type}
                    type="button"
                    onClick={() => setOrderType(type)}
                    className={`flex-1 rounded-xl border py-2 text-xs font-semibold transition ${
                      orderType === type
                        ? "border-blue-500 bg-blue-600 text-white"
                        : "border-slate-200 bg-white text-slate-600 hover:bg-slate-50"
                    }`}
                  >
                    {type}
                  </button>
                ))}
              </div>
            </div>

            {/* Limit price (conditional) */}
            {orderType === "LIMIT" && (
              <div className="space-y-1.5">
                <label
                  htmlFor="modal-limit-price"
                  className="block text-xs font-semibold uppercase tracking-wide text-slate-500"
                >
                  Limit Price (₹)
                </label>
                <PriceInput
                  id="modal-limit-price"
                  value={limitPrice}
                  onChange={setLimitPrice}
                  tickSize={limitPriceTickSize}
                  error={fieldErrors.limitPrice}
                  focusVariant="neutral"
                  className="py-2.5"
                  placeholder="0.00"
                />
              </div>
            )}

            {/* Quantity */}
            <div className="space-y-1.5">
              <label
                htmlFor="modal-quantity"
                className="block text-xs font-semibold uppercase tracking-wide text-slate-500"
              >
                Quantity (Shares)
              </label>
              <input
                id="modal-quantity"
                type="number"
                value={quantity}
                onChange={(e) => setQuantity(e.target.value)}
                min={1}
                className="w-full rounded-xl border border-slate-300 bg-white px-3 py-2.5 text-sm text-slate-900 placeholder-slate-400 transition focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                placeholder="1"
              />
              {fieldErrors.quantity && (
                <p className="text-xs text-rose-600">{fieldErrors.quantity}</p>
              )}
            </div>

            {/* Risk Management */}
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <p className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                  Risk Management
                  <span className="ml-1 font-normal normal-case text-slate-400">(optional)</span>
                </p>
                {riskFieldsDisabled && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-500">
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    Computing…
                  </span>
                )}
                {isAiSuggested && !riskFieldsDisabled && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-indigo-200 bg-indigo-50 px-2 py-0.5 text-[10px] font-semibold text-indigo-600">
                    <Sparkles className="h-2.5 w-2.5" />
                    ML Suggested
                  </span>
                )}
              </div>

              {/* Stop Loss */}
              <div className="space-y-1.5">
                <label
                  htmlFor="modal-sl"
                  className="block text-xs font-medium text-slate-500"
                >
                  Stop Loss (₹)
                </label>
                <PriceInput
                  id="modal-sl"
                  value={stopLoss}
                  onChange={(v) => { setStopLoss(v); setIsAiSuggested(false); }}
                  error={fieldErrors.stopLoss}
                  focusVariant="danger"
                  disabled={riskFieldsDisabled}
                  className="py-2.5"
                  placeholder={riskFieldsDisabled ? "Calculating…" : "Auto-close when price falls below"}
                />
              </div>

              {/* Take-profit targets */}
              <div className="grid grid-cols-3 gap-2">
                {([
                  { label: "Target 1", id: "modal-tp1", value: takeProfit1, set: setTakeProfit1, errKey: "takeProfit1" },
                  { label: "Target 2", id: "modal-tp2", value: takeProfit2, set: setTakeProfit2, errKey: "takeProfit2" },
                  { label: "Target 3", id: "modal-tp3", value: takeProfit3, set: setTakeProfit3, errKey: "takeProfit3" },
                ]).map(({ label, id, value, set, errKey }) => (
                  <div key={id} className="space-y-1.5">
                    <label htmlFor={id} className="block text-xs font-medium text-slate-500">
                      {label} (₹)
                    </label>
                    <PriceInput
                      id={id}
                      value={value}
                      onChange={(v) => { set(v); setIsAiSuggested(false); }}
                      error={fieldErrors[errKey]}
                      focusVariant="success"
                      disabled={riskFieldsDisabled}
                      className="py-2"
                      placeholder={riskFieldsDisabled ? "…" : "—"}
                    />
                  </div>
                ))}
              </div>
            </div>

            {/* Cost summary */}
            {qty > 0 && refPrice > 0 && (
              <div
                className={`rounded-xl border px-3 py-3 ${
                  insufficientFunds ? "border-rose-200 bg-rose-50" : "border-slate-100 bg-slate-50"
                }`}
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-semibold uppercase tracking-wide text-slate-400">
                    {side === "BUY" ? "Estimated Cost" : "Estimated Proceeds"}
                  </span>
                  <span className="text-sm font-bold text-slate-900">{fmt(estimatedCost)}</span>
                </div>
                <div className="mt-1.5 flex items-center justify-between">
                  <span className="text-[11px] text-slate-400">Cash after trade</span>
                  <span className={`text-xs font-semibold ${cashAfter < 0 ? "text-rose-600" : "text-slate-600"}`}>
                    {fmt(cashAfter)}
                  </span>
                </div>
                {portfolioLoading && (
                  <p className="mt-1 text-[11px] text-slate-400">Loading portfolio…</p>
                )}
                {fieldErrors.funds && (
                  <p className="mt-1.5 text-xs font-medium text-rose-600">{fieldErrors.funds}</p>
                )}
              </div>
            )}

            {/* Actions */}
            <div className="flex gap-3 pt-1">
              <button
                type="button"
                onClick={onClose}
                className="flex-1 rounded-xl border border-slate-200 bg-white px-4 py-2.5 text-sm font-semibold text-slate-700 transition hover:bg-slate-50"
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={isPending || portfolioLoading}
                className={`inline-flex flex-1 items-center justify-center gap-2 rounded-xl px-4 py-2.5 text-sm font-semibold text-white transition disabled:cursor-not-allowed disabled:opacity-60 ${
                  side === "BUY" ? "bg-emerald-600 hover:bg-emerald-700" : "bg-rose-600 hover:bg-rose-700"
                }`}
              >
                {isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                {isPending
                  ? "Placing…"
                  : `${side === "BUY" ? "Buy" : "Sell"} ${qty > 0 ? qty : ""} · ${orderType}`}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
