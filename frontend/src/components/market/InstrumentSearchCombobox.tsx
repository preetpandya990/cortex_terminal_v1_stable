"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Loader2, Search } from "lucide-react";

import { upstoxAPI } from "@/lib/api";
import type { UpstoxInstrument, UpstoxInstrumentSearchResponse } from "@/types/upstox";

const SEARCH_DEBOUNCE_MS = 200;
const QUICK_LTP_MAX_RESULTS = 8;
const LTP_CACHE_TTL_MS = 5000;
const LTP_REQUEST_SPACING_MS = 250;

function useDebouncedValue<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value);

  useEffect(() => {
    const handle = window.setTimeout(() => setDebounced(value), delayMs);
    return () => window.clearTimeout(handle);
  }, [value, delayMs]);

  return debounced;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function normalizePrice(value: unknown): number | null {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

const INR_FORMAT = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

type InstrumentSearchComboboxProps = {
  onSelect: (instrument: UpstoxInstrument) => void;
  label?: string;
  placeholder?: string;
  helperText?: string;
  segment?: string;
  limit?: number;
  showQuickLtp?: boolean;
  initialQuery?: string;
  selectedInstrumentKey?: string | null;
  variant?: "default" | "dashboard";
  className?: string;
};

export function InstrumentSearchCombobox({
  onSelect,
  label = "Search Stock",
  placeholder = "Type symbol or company name",
  helperText = "Minimum 1 character.",
  segment = "NSE_EQ",
  limit = 12,
  showQuickLtp = true,
  initialQuery = "",
  selectedInstrumentKey = null,
  variant = "default",
  className = "",
}: InstrumentSearchComboboxProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const listboxId = useId();

  const [query, setQuery] = useState(initialQuery);
  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [highlightedIndex, setHighlightedIndex] = useState(-1);

  const [ltpByInstrument, setLtpByInstrument] = useState<
    Record<string, { price: number | null; loading: boolean; fetchedAt: number }>
  >({});

  const ltpCacheRef = useRef(new Map<string, { price: number | null; fetchedAt: number }>());
  const ltpInFlightRef = useRef(new Set<string>());
  const ltpQueueRef = useRef<string[]>([]);
  const isPumpingLtpQueueRef = useRef(false);
  const isMountedRef = useRef(true);

  const debouncedQuery = useDebouncedValue(query.trim(), SEARCH_DEBOUNCE_MS);

  const searchQuery = useQuery({
    queryKey: ["upstox", "instruments", debouncedQuery, segment, limit],
    queryFn: () => upstoxAPI.searchInstruments(debouncedQuery, { segment, limit }),
    enabled: debouncedQuery.length >= 1,
    staleTime: 30_000,
    gcTime: 3 * 60_000,
  });

  const results = useMemo(() => {
    return ((searchQuery.data as UpstoxInstrumentSearchResponse | undefined)?.results ?? []) as UpstoxInstrument[];
  }, [searchQuery.data]);

  // Keep input synced with selected instrument
  useEffect(() => {
    if (selectedInstrumentKey && results.length > 0) {
      const selectedInstrument = results.find(r => r.instrument_key === selectedInstrumentKey);
      if (selectedInstrument && query !== selectedInstrument.trading_symbol) {
        setQuery(selectedInstrument.trading_symbol);
      }
    }
  }, [selectedInstrumentKey, results, query]);

  const pumpLtpQueue = useCallback(async () => {
    if (isPumpingLtpQueueRef.current) return;
    isPumpingLtpQueueRef.current = true;

    while (ltpQueueRef.current.length > 0) {
      const instrumentKey = ltpQueueRef.current.shift();
      if (!instrumentKey) continue;

      const now = Date.now();
      const cached = ltpCacheRef.current.get(instrumentKey);
      if (cached && now - cached.fetchedAt <= LTP_CACHE_TTL_MS) {
        if (isMountedRef.current) {
          setLtpByInstrument((prev) => ({
            ...prev,
            [instrumentKey]: {
              price: cached.price,
              loading: false,
              fetchedAt: cached.fetchedAt,
            },
          }));
        }
        continue;
      }

      if (ltpInFlightRef.current.has(instrumentKey)) {
        continue;
      }

      ltpInFlightRef.current.add(instrumentKey);
      if (isMountedRef.current) {
        setLtpByInstrument((prev) => ({
          ...prev,
          [instrumentKey]: {
            price: prev[instrumentKey]?.price ?? null,
            loading: true,
            fetchedAt: prev[instrumentKey]?.fetchedAt ?? 0,
          },
        }));
      }

      try {
        const response = await upstoxAPI.getLtpQuote(instrumentKey);
        const price = normalizePrice(response?.last_price);
        const fetchedAt = Date.now();
        ltpCacheRef.current.set(instrumentKey, { price, fetchedAt });

        if (isMountedRef.current) {
          setLtpByInstrument((prev) => ({
            ...prev,
            [instrumentKey]: {
              price,
              loading: false,
              fetchedAt,
            },
          }));
        }
      } catch {
        if (isMountedRef.current) {
          setLtpByInstrument((prev) => ({
            ...prev,
            [instrumentKey]: {
              price: prev[instrumentKey]?.price ?? null,
              loading: false,
              fetchedAt: prev[instrumentKey]?.fetchedAt ?? 0,
            },
          }));
        }
      } finally {
        ltpInFlightRef.current.delete(instrumentKey);
      }

      await sleep(LTP_REQUEST_SPACING_MS);
    }

    isPumpingLtpQueueRef.current = false;
  }, []);

  const enqueueLtpLookup = useCallback(
    (instrumentKey: string) => {
      if (!showQuickLtp || !instrumentKey) return;

      const now = Date.now();
      const cached = ltpCacheRef.current.get(instrumentKey);
      if (cached && now - cached.fetchedAt <= LTP_CACHE_TTL_MS) {
        setLtpByInstrument((prev) => ({
          ...prev,
          [instrumentKey]: {
            price: cached.price,
            loading: false,
            fetchedAt: cached.fetchedAt,
          },
        }));
        return;
      }

      if (
        ltpInFlightRef.current.has(instrumentKey) ||
        ltpQueueRef.current.includes(instrumentKey)
      ) {
        return;
      }

      ltpQueueRef.current.push(instrumentKey);
      void pumpLtpQueue();
    },
    [pumpLtpQueue, showQuickLtp]
  );

  useEffect(() => {
    if (!isDropdownOpen || !showQuickLtp || results.length === 0) {
      return;
    }

    results
      .slice(0, QUICK_LTP_MAX_RESULTS)
      .forEach((instrument) => enqueueLtpLookup(instrument.instrument_key));
  }, [enqueueLtpLookup, isDropdownOpen, results, showQuickLtp]);

  useEffect(() => {
    isMountedRef.current = true;
    const inFlight = ltpInFlightRef.current;
    return () => {
      isMountedRef.current = false;
      ltpQueueRef.current = [];
      inFlight.clear();
    };
  }, []);

  useEffect(() => {
    if (debouncedQuery.length < 1) {
      setHighlightedIndex(-1);
      setIsDropdownOpen(false);
      return;
    }

    if (searchQuery.isSuccess) {
      setIsDropdownOpen(true);
      setHighlightedIndex(results.length ? 0 : -1);
    }
  }, [debouncedQuery.length, results.length, searchQuery.isSuccess]);

  useEffect(() => {
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (containerRef.current && !containerRef.current.contains(target)) {
        setIsDropdownOpen(false);
      }
    };

    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, []);

  const handleSelect = (instrument: UpstoxInstrument) => {
    setQuery(instrument.trading_symbol);
    setIsDropdownOpen(false);
    setHighlightedIndex(-1);
    onSelect(instrument);
  };

  const inputWrapperClass =
    variant === "dashboard"
      ? "rounded-xl border border-slate-300/90 bg-white shadow-sm"
      : "rounded-lg border border-slate-200 bg-white shadow-sm";

  const inputClass =
    variant === "dashboard"
      ? "h-11 w-full rounded-xl border-0 bg-transparent pl-11 pr-4 text-sm text-slate-900 outline-none"
      : "h-11 w-full rounded-lg border-0 bg-transparent pl-11 pr-4 text-sm text-slate-900 outline-none";

  return (
    <div className={`flex flex-col gap-2 ${className}`} ref={containerRef}>
      <label className="text-sm font-medium text-slate-600">{label}</label>
      <div className="relative">
        <div className={inputWrapperClass}>
          <Search className="pointer-events-none absolute left-3 top-3 h-5 w-5 text-slate-400" />
          <input
            value={query}
            role="combobox"
            aria-autocomplete="list"
            aria-expanded={isDropdownOpen}
            aria-controls={`instrument-listbox-${listboxId}`}
            aria-activedescendant={
              highlightedIndex >= 0
                ? `instrument-listbox-${listboxId}-option-${highlightedIndex}`
                : undefined
            }
            onChange={(event) => {
              setQuery(event.target.value);
              setIsDropdownOpen(event.target.value.trim().length >= 1);
            }}
            onFocus={() => {
              if (results.length > 0 || searchQuery.isLoading) {
                setIsDropdownOpen(true);
              }
            }}
            onKeyDown={(event) => {
              if (!isDropdownOpen && event.key === "ArrowDown" && results.length) {
                setIsDropdownOpen(true);
                setHighlightedIndex(0);
                return;
              }

              if (event.key === "ArrowDown") {
                event.preventDefault();
                setHighlightedIndex((prev) => {
                  const next = Math.min(prev + 1, results.length - 1);
                  return next < 0 ? 0 : next;
                });
              } else if (event.key === "ArrowUp") {
                event.preventDefault();
                setHighlightedIndex((prev) => Math.max(prev - 1, 0));
              } else if (event.key === "Enter") {
                event.preventDefault();
                if (highlightedIndex >= 0 && results[highlightedIndex]) {
                  handleSelect(results[highlightedIndex]);
                }
              } else if (event.key === "Escape") {
                setIsDropdownOpen(false);
              }
            }}
            placeholder={placeholder}
            className={inputClass}
          />
        </div>

        {isDropdownOpen && (
          <div className="absolute z-30 mt-2 w-full overflow-hidden rounded-xl border border-slate-200 bg-white shadow-lg">
            <div
              id={`instrument-listbox-${listboxId}`}
              role="listbox"
              className="max-h-72 overflow-y-auto"
            >
              {searchQuery.isLoading && (
                <div className="flex items-center gap-2 px-4 py-3 text-xs text-slate-500">
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Loading results...
                </div>
              )}

              {!searchQuery.isLoading && results.length === 0 && (
                <div className="px-4 py-3 text-xs text-slate-500">No results found.</div>
              )}

              {!searchQuery.isLoading &&
                results.map((instrument, index) => {
                  const isHighlighted = highlightedIndex === index;
                  const isSelected = selectedInstrumentKey === instrument.instrument_key;
                  const ltpState = ltpByInstrument[instrument.instrument_key];

                  return (
                    <button
                      key={instrument.instrument_key}
                      id={`instrument-listbox-${listboxId}-option-${index}`}
                      role="option"
                      aria-selected={isHighlighted}
                      onMouseDown={(event) => event.preventDefault()}
                      onClick={() => handleSelect(instrument)}
                      onMouseEnter={() => setHighlightedIndex(index)}
                      className={`flex w-full items-center justify-between gap-3 px-4 py-3 text-left text-sm transition ${
                        isHighlighted
                          ? "bg-slate-100 text-slate-900"
                          : "text-slate-700 hover:bg-slate-50"
                      } ${isSelected ? "ring-1 ring-inset ring-blue-500/40" : ""}`}
                    >
                      <div className="min-w-0">
                        <div className="truncate font-semibold">{instrument.trading_symbol}</div>
                        <div className="truncate text-xs text-slate-500">{instrument.name || "—"}</div>
                      </div>

                      <div className="ml-auto flex min-w-[110px] flex-col items-end text-xs">
                        <span className="text-slate-400">{instrument.exchange || instrument.segment || "—"}</span>
                        {showQuickLtp && (
                          <span className="font-semibold text-slate-700">
                            {ltpState?.loading ? (
                              <span className="inline-flex items-center gap-1 text-slate-500">
                                <Loader2 className="h-3 w-3 animate-spin" />
                                LTP
                              </span>
                            ) : ltpState?.price !== null && ltpState?.price !== undefined ? (
                              INR_FORMAT.format(ltpState.price)
                            ) : (
                              "—"
                            )}
                          </span>
                        )}
                      </div>
                    </button>
                  );
                })}
            </div>
          </div>
        )}
      </div>

      <span className="text-xs text-slate-400">{helperText}</span>
      {searchQuery.isError && (
        <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-600">
          Unable to search right now. Please retry.
        </div>
      )}
    </div>
  );
}
