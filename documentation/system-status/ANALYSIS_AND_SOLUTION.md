# Comprehensive Data Flow Analysis & Solution
## Cortex AI - Hawk Eye Radar TypeScript Issues

**Date:** April 7, 2026  
**Status:** Production-Ready Solution Design  
**Scope:** API → Types → Components → UI

---

## 1. CURRENT STATE ANALYSIS

### 1.1 Data Flow Architecture
```
Backend API (Upstox V3)
    ↓
Frontend API Layer (api.ts)
    ↓
Type Definitions (upstox.ts)
    ↓
React Query (useMutation)
    ↓
Page Component (page.tsx)
    ↓
Chart Component (CandlestickChart.tsx)
    ↓
Lightweight Charts Library v5.1.0
```

### 1.2 Identified Issues

#### **Issue #1: Missing Type Definition**
- **Location:** `frontend/src/types/upstox.ts`
- **Problem:** `UpstoxCandleResponse` type does not exist
- **Impact:** CandlestickChart.tsx expects this type but it's undefined
- **Current State:** Only `UpstoxCandle` type exists (tuple format)

#### **Issue #2: Type Mismatch in CandlestickChart**
- **Location:** `frontend/src/components/charts/CandlestickChart.tsx`
- **Problem:** Component expects `UpstoxCandleResponse[]` with properties:
  - `timestamp`
  - `open`, `high`, `low`, `close`
- **Reality:** Upstox API returns `UpstoxCandle[]` (tuple format):
  ```typescript
  [timestamp, open, high, low, close, volume, oi?]
  ```

#### **Issue #3: Props Interface Mismatch**
- **Location:** `frontend/src/app/hawk-eye-radar/page.tsx` line 588
- **Problem:** Page passes props that don't exist in CandlestickChart interface:
  ```typescript
  // Page passes:
  candles, liveTick, candleUnit, candleInterval, 
  canLoadMoreHistory, isLoadingMoreHistory, onLoadMoreHistory, height
  
  // Component expects:
  data, height
  ```

#### **Issue #4: useMutation Type Inference**
- **Location:** `frontend/src/app/hawk-eye-radar/page.tsx` line 120
- **Status:** ✅ PARTIALLY FIXED (type parameters added)
- **Remaining:** Promise.resolve needs explicit type assertion (line 144)

---

## 2. ROOT CAUSE ANALYSIS

### 2.1 Architecture Mismatch
The system has **two competing data models**:

**Model A: Upstox API Format (Current Backend)**
```typescript
type UpstoxCandle = [
  timestamp: string | number,
  open: number,
  high: number,
  low: number,
  close: number,
  volume: number,
  oi?: number
];
```

**Model B: Chart-Friendly Format (Expected by Component)**
```typescript
interface UpstoxCandleResponse {
  timestamp: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  oi?: number;
}
```

### 2.2 Missing Transformation Layer
There's no adapter/transformer between the API response format and the chart component's expected format.

---

## 3. PRODUCTION-READY SOLUTION

### 3.1 Type System Redesign

#### **Step 1: Define Complete Type Hierarchy**
```typescript
// frontend/src/types/upstox.ts

// Raw API response (tuple format from Upstox)
export type UpstoxCandle = [
  timestamp: string | number,
  open: number,
  high: number,
  low: number,
  close: number,
  volume: number,
  oi?: number
];

// Normalized format for application use
export interface UpstoxCandleData {
  timestamp: string | number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  oi?: number;
}

// API response wrapper
export interface UpstoxCandlesPayload {
  candles: UpstoxCandle[];
}

export interface UpstoxCandlesResponse {
  status: string;
  data: UpstoxCandlesPayload;
}
```

#### **Step 2: Create Transformation Utilities**
```typescript
// frontend/src/lib/candle-transforms.ts

import type { UpstoxCandle, UpstoxCandleData } from "@/types/upstox";
import type { CandlestickData, UTCTimestamp } from "lightweight-charts";

/**
 * Transforms Upstox tuple format to normalized object format
 */
export function normalizeUpstoxCandle(candle: UpstoxCandle): UpstoxCandleData {
  return {
    timestamp: candle[0],
    open: candle[1],
    high: candle[2],
    low: candle[3],
    close: candle[4],
    volume: candle[5],
    oi: candle[6],
  };
}

/**
 * Transforms normalized candle to lightweight-charts format
 */
export function toLightweightChartData(candle: UpstoxCandleData): CandlestickData {
  const timestamp = typeof candle.timestamp === 'string' 
    ? new Date(candle.timestamp).getTime() / 1000
    : candle.timestamp > 10_000_000_000 
      ? candle.timestamp / 1000 
      : candle.timestamp;

  return {
    time: timestamp as UTCTimestamp,
    open: candle.open,
    high: candle.high,
    low: candle.low,
    close: candle.close,
  };
}

/**
 * Batch transformation for performance
 */
export function transformCandlesForChart(candles: UpstoxCandle[]): CandlestickData[] {
  return candles
    .map(normalizeUpstoxCandle)
    .map(toLightweightChartData)
    .sort((a, b) => (a.time as number) - (b.time as number));
}
```

### 3.2 Enhanced CandlestickChart Component

#### **Design Principles:**
1. **Single Responsibility:** Chart rendering only
2. **Type Safety:** Strict TypeScript with no `any` casts
3. **Performance:** Memoization and efficient updates
4. **Extensibility:** Support for live updates and historical loading
5. **Accessibility:** ARIA labels and keyboard navigation

#### **New Props Interface:**
```typescript
// frontend/src/components/charts/CandlestickChart.tsx

import type { UpstoxCandle, UpstoxLtpTick } from "@/types/upstox";
import type { CandleUnit } from "@/lib/chart-policy";

export interface CandlestickChartProps {
  // Core data
  candles: UpstoxCandle[];
  
  // Live updates
  liveTick?: UpstoxLtpTick | null;
  
  // Chart configuration
  candleUnit: CandleUnit;
  candleInterval: number;
  height?: number;
  
  // Historical data loading
  canLoadMoreHistory?: boolean;
  isLoadingMoreHistory?: boolean;
  onLoadMoreHistory?: () => Promise<void>;
  
  // Styling
  className?: string;
}
```

### 3.3 Component Implementation Strategy

#### **Features:**
- ✅ Accepts raw `UpstoxCandle[]` (tuple format)
- ✅ Internal transformation to chart format
- ✅ Live tick updates without full re-render
- ✅ Infinite scroll for historical data
- ✅ Responsive sizing
- ✅ Loading states
- ✅ Error boundaries

---

## 4. IMPLEMENTATION PLAN

### Phase 1: Type System (15 min)
1. Add `UpstoxCandleData` interface to `upstox.ts`
2. Create `candle-transforms.ts` utility file
3. Add comprehensive JSDoc comments

### Phase 2: CandlestickChart Refactor (20 min)
1. Update props interface
2. Integrate transformation utilities
3. Add live tick update logic
4. Implement historical loading UI
5. Add error handling

### Phase 3: Page Integration (10 min)
1. Update hawk-eye-radar page.tsx
2. Remove type assertions (no longer needed)
3. Test data flow end-to-end

### Phase 4: Testing & Validation (15 min)
1. Run TypeScript compiler
2. Test with real Upstox data
3. Verify live updates
4. Check historical loading
5. Performance profiling

**Total Estimated Time:** 60 minutes

---

## 5. BENEFITS OF THIS APPROACH

### 5.1 Type Safety
- ✅ Zero `any` types
- ✅ Compile-time error detection
- ✅ IntelliSense support throughout

### 5.2 Maintainability
- ✅ Clear separation of concerns
- ✅ Single source of truth for transformations
- ✅ Easy to test in isolation

### 5.3 Performance
- ✅ Efficient batch transformations
- ✅ Memoized chart updates
- ✅ Minimal re-renders

### 5.4 Scalability
- ✅ Easy to add new chart types
- ✅ Reusable transformation utilities
- ✅ Extensible props interface

### 5.5 Developer Experience
- ✅ Self-documenting code
- ✅ Clear error messages
- ✅ Predictable behavior

---

## 6. ALTERNATIVE APPROACHES CONSIDERED

### Option A: Keep Tuple Format Everywhere
**Pros:** No transformation overhead  
**Cons:** Poor DX, hard to maintain, error-prone

### Option B: Transform at API Layer
**Pros:** Components always get clean data  
**Cons:** Couples API to UI, harder to cache

### Option C: Current Approach (Recommended)
**Pros:** Best of both worlds - raw data cached, transformed on demand  
**Cons:** Slight complexity in transformation layer

---

## 7. NEXT STEPS

**Ready to proceed with implementation?**

I will:
1. Create the transformation utilities
2. Refactor CandlestickChart component
3. Update the hawk-eye-radar page
4. Fix all TypeScript errors
5. Ensure production-ready quality

**Estimated completion:** 60 minutes  
**Risk level:** Low (well-tested pattern)  
**Breaking changes:** None (internal refactor only)

---

**Approval needed to proceed with implementation.**
