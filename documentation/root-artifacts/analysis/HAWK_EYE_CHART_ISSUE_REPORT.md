# Hawk-Eye Radar Chart Issue - Context Report

**Date**: 2026-04-23  
**Issue**: Chart not displaying in DetailPane when "View Details" button is clicked  
**Status**: 🔍 INVESTIGATION COMPLETE

---

## Problem Summary

When clicking "View Details" on a trade suggestion card, the DetailPane opens but the **chart component is missing**. The panel shows other components but not the main CandlestickChart.

---

## Architecture Analysis

### Component Flow

```
HawkEyeRadarPage
  ├─ TradeSuggestionCard (shows suggestion)
  │   └─ "View Details" button → calls handleViewDetails(suggestionId)
  │
  ├─ handleViewDetails()
  │   └─ Sets modalSuggestion (opens SuggestionDetailModal)
  │   └─ Does NOT set detailSuggestion or selectedInstrument
  │
  ├─ DetailPane (floating panel with chart)
  │   └─ Only renders if detailInstrument exists
  │   └─ detailInstrument = selectedInstrument || detailSuggestion
  │
  └─ SuggestionDetailModal (different component)
      └─ Shows suggestion details in a modal
```

---

## Root Cause Identified

### Issue 1: Wrong Component Being Opened

**Current Behavior**:
```typescript
// In page.tsx line 100
const handleViewDetails = (suggestionId: string) => {
  const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
  if (suggestion) {
    setModalSuggestion(suggestion);  // ❌ Opens SuggestionDetailModal, NOT DetailPane
    const params = new URLSearchParams(searchParams.toString());
    params.set('suggestion_id', suggestionId);
    router.push(`?${params.toString()}`, { scroll: false });
  }
};
```

**What happens**:
1. User clicks "View Details" button
2. `handleViewDetails()` is called
3. Sets `modalSuggestion` state
4. Opens `SuggestionDetailModal` (a different modal component)
5. Does NOT set `detailSuggestion` or `selectedInstrument`
6. `DetailPane` never renders because `detailInstrument` is `null`

**Expected Behavior**:
- Should set `detailSuggestion` to open the DetailPane with chart
- DetailPane should render when `detailInstrument` is not null

### Issue 2: DetailPane Rendering Logic

```typescript
// In page.tsx line 131
const detailInstrument = selectedInstrument || (detailSuggestion ? {
  instrument_key: detailSuggestion.instrument_key,
  trading_symbol: detailSuggestion.trading_symbol || detailSuggestion.symbol,
  name: detailSuggestion.symbol,
  exchange: "NSE",
} as UpstoxInstrument : null);

// In page.tsx line 276
{detailInstrument && (
  <DetailPane
    instrument={detailInstrument}
    onClose={handleCloseDetail}
  />
)}
```

**Current State**:
- `detailInstrument` is only set when:
  1. `selectedInstrument` is set (via InstrumentSearchCombobox)
  2. OR `detailSuggestion` is set (currently never happens)
- Since `handleViewDetails` doesn't set `detailSuggestion`, `detailInstrument` remains `null`
- DetailPane never renders

---

## Component Inventory

### 1. DetailPane Component
**Location**: `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx`

**Purpose**: Floating panel with chart and analysis

**Features**:
- ✅ CandlestickChart component (fully implemented)
- ✅ Live tick streaming via WebSocket
- ✅ Historical + intraday candle loading
- ✅ AnalysisCardsSection (AI/ML analysis)
- ✅ Responsive design with overlay

**Props**:
```typescript
interface DetailPaneProps {
  instrument: UpstoxInstrument;
  onClose: () => void;
}
```

**Status**: ✅ Component is complete and functional

### 2. SuggestionDetailModal Component
**Location**: `frontend/src/app/hawk-eye-radar/components/SuggestionDetailModal.tsx`

**Purpose**: Modal showing suggestion details (NOT the chart panel)

**Current Usage**: This is what's currently being opened by "View Details" button

**Status**: ⚠️ Wrong component being used

### 3. CandlestickChart Component
**Location**: `frontend/src/components/charts/CandlestickChart.tsx`

**Purpose**: Renders candlestick chart using lightweight-charts library

**Features**:
- ✅ Candlestick rendering
- ✅ Live tick updates
- ✅ Responsive design
- ✅ Infinite scroll for historical data
- ✅ Loading states

**Status**: ✅ Component is complete and functional

---

## Data Flow Analysis

### Current Flow (Broken)
```
User clicks "View Details"
  ↓
handleViewDetails(suggestionId)
  ↓
setModalSuggestion(suggestion)
  ↓
SuggestionDetailModal opens (no chart)
  ↓
DetailPane never renders (detailInstrument is null)
```

### Expected Flow (Fixed)
```
User clicks "View Details"
  ↓
handleViewDetails(suggestionId)
  ↓
setDetailSuggestion(suggestion)  ← FIX: Set this instead
  ↓
detailInstrument is constructed from detailSuggestion
  ↓
DetailPane renders with chart
  ↓
Chart loads candles and displays
```

---

## TradeSuggestion Data Structure

```typescript
interface TradeSuggestion {
  suggestion_id: string;
  symbol: string;              // e.g., "RELIANCE"
  instrument_key: string;      // e.g., "NSE_EQ|INE002A01018"
  trading_symbol: string | null; // e.g., "RELIANCE-EQ"
  consensus_score: number;
  confidence_level: ConfidenceLevel;
  signal_direction: SignalDirection;
  // ... other fields
}
```

**Available for Chart**:
- ✅ `instrument_key` - Required for API calls
- ✅ `trading_symbol` - Display name
- ✅ `symbol` - Fallback display name

**Conversion to UpstoxInstrument**:
```typescript
const detailInstrument = {
  instrument_key: suggestion.instrument_key,
  trading_symbol: suggestion.trading_symbol || suggestion.symbol,
  name: suggestion.symbol,
  exchange: "NSE",
} as UpstoxInstrument;
```

---

## Solution Required

### Fix 1: Update handleViewDetails Function

**Change**:
```typescript
// BEFORE (line 100)
const handleViewDetails = (suggestionId: string) => {
  const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
  if (suggestion) {
    setModalSuggestion(suggestion);  // ❌ Wrong state
    const params = new URLSearchParams(searchParams.toString());
    params.set('suggestion_id', suggestionId);
    router.push(`?${params.toString()}`, { scroll: false });
  }
};

// AFTER (proposed fix)
const handleViewDetails = (suggestionId: string) => {
  const suggestion = suggestionsData?.suggestions.find((s) => s.suggestion_id === suggestionId);
  if (suggestion) {
    setDetailSuggestion(suggestion);  // ✅ Correct state for DetailPane
    const params = new URLSearchParams(searchParams.toString());
    params.set('instrument_key', suggestion.instrument_key);  // ✅ Use instrument_key
    router.push(`?${params.toString()}`, { scroll: false });
  }
};
```

### Fix 2: Decide on Modal vs. DetailPane

**Option A: Use DetailPane Only** (Recommended)
- Remove SuggestionDetailModal usage
- Use DetailPane for all detail views
- DetailPane already has chart + analysis

**Option B: Keep Both**
- SuggestionDetailModal for quick info
- DetailPane for full chart view
- Add separate "View Chart" button

**Option C: Combine Both**
- Show SuggestionDetailModal first
- Add "View Chart" button in modal
- Opens DetailPane when clicked

---

## Files Involved

### Primary Files
1. `frontend/src/app/hawk-eye-radar/page.tsx` - Main page logic
2. `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx` - Chart panel
3. `frontend/src/app/hawk-eye-radar/components/SuggestionDetailModal.tsx` - Detail modal
4. `frontend/src/app/hawk-eye-radar/components/TradeSuggestionCard.tsx` - Card with button

### Supporting Files
1. `frontend/src/components/charts/CandlestickChart.tsx` - Chart component
2. `frontend/src/lib/chart-policy.ts` - Chart configuration
3. `frontend/src/lib/candle-transforms.ts` - Data transformation
4. `frontend/src/types/trade_suggestions.ts` - Type definitions
5. `frontend/src/types/upstox.ts` - Upstox types

---

## Testing Checklist

After fix is applied:

- [ ] Click "View Details" on a suggestion card
- [ ] DetailPane should open (floating overlay)
- [ ] Chart should be visible in the panel
- [ ] Chart should load candles for the instrument
- [ ] Live tick should update the last candle
- [ ] Analysis cards should show below chart
- [ ] Close button should work
- [ ] URL should update with instrument_key
- [ ] Refresh should restore the panel state

---

## Additional Observations

### DetailPane Features (Already Implemented)
1. ✅ **Chart Loading**: Fetches historical + intraday candles
2. ✅ **Live Updates**: WebSocket tick streaming
3. ✅ **Hybrid Mode**: Merges historical + intraday data
4. ✅ **Analysis Section**: Shows AI/ML analysis cards
5. ✅ **Responsive**: Full-screen overlay with proper styling
6. ✅ **Error Handling**: Loading and error states

### Chart Configuration
- **Default Range**: 30 days (from chart-policy.ts)
- **Candle Unit**: "minute" (1-minute candles)
- **Candle Interval**: 1
- **Height**: 460px
- **Live Merge**: Enabled when viewing today/yesterday

### API Endpoints Used
1. `GET /api/v1/upstox/candles/intraday` - Intraday candles
2. `GET /api/v1/upstox/candles/historical` - Historical candles
3. `WS /api/v1/upstox/ticks/ws` - Live tick stream

---

## Recommendation

**Immediate Fix**: Change `handleViewDetails` to set `detailSuggestion` instead of `modalSuggestion`

**Reasoning**:
1. DetailPane is fully implemented with chart
2. SuggestionDetailModal doesn't have chart
3. User expects to see chart when clicking "View Details"
4. Minimal code change required
5. Maintains existing functionality

**Impact**: Low risk, high value fix

---

**Status**: 🔍 READY FOR IMPLEMENTATION  
**Complexity**: Low (single function change)  
**Estimated Time**: 5 minutes
