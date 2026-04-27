# Main Dashboard Context - Cortex Terminal V1

## Overview
The main dashboard (`/` - `frontend/src/app/page.tsx`) is the **landing page** and **navigation hub** for Cortex Terminal V1. It provides quick access to all major features and displays a live positions placeholder.

## Current Structure

### Main Dashboard Page (`/`)
**Location**: `frontend/src/app/page.tsx`

**Components**:
1. **Welcome Card** - Hero section with feature navigation
2. **Open Positions Placeholder** - Mock live P&L tracker (WebSocket-ready)

### Navigation Cards (4 Main Features)

#### 1. Cortex AI (`/cortex-ai`)
**Icon**: Brain  
**Description**: AI-powered signals, regime detection, and market intelligence

**Features**:
- Trading Signals Panel (buy/sell/hold with confidence scores)
- Market Regime Panel (bull/bear/sideways classification)
- Events Panel (high-impact news with fake news detection)
- ML Models Panel (model monitoring and drift detection)

**Tech**: Tabbed interface with real-time WebSocket updates

#### 2. Hawk Eye Radar (`/hawk-eye-radar`)
**Icon**: Activity  
**Description**: Multi-timeframe signal scanner and technical analysis

**Features**:
- Trade suggestions with multi-timeframe analysis
- Real-time WebSocket updates for new suggestions
- Instrument search and filtering
- Detail pane with technical indicators
- Stats dashboard (total suggestions, quality metrics)

**Tech**: WebSocket-first with polling fallback

#### 3. Market Scanner (`/scanner`)
**Icon**: TrendingUp  
**Description**: Scan markets for gainers, losers, and volume spikes

**Status**: Implemented

#### 4. Risk Management
**Icon**: AlertTriangle  
**Description**: Portfolio risk analysis and position sizing

**Status**: Coming Soon (placeholder card, opacity 60%)

## Open Positions Component

**Location**: `frontend/src/components/dashboard/OpenPositionsPlaceholder.tsx`

**Purpose**: Mock live positions tracker demonstrating WebSocket-ready architecture

**Features**:
- 8 mock positions (RELIANCE, HDFCBANK, TCS, INFY, etc.)
- Live P&L ticking (updates every 1 second)
- Long/Short position support
- Status indicators (Open/Partial)
- Summary metrics:
  - Open Count
  - Gross Exposure (INR)
  - Average P&L %

**Data Structure**:
```typescript
type Position = {
  id: string;
  symbol: string;
  qty: number;
  side: "Long" | "Short";
  status: "Open" | "Partial";
  buyPrice: number;
  currentPrice: number;
  targetPrice: number;
  pnlPct: number;
}
```

**Mock Logic**:
- P&L drifts randomly ±0.22% per tick
- Clamped between -4.5% and +5.5%
- Current price derived from buy price + P&L%
- Feed status: "disconnected" (placeholder for WebSocket)

## Layout & Navigation

**Location**: `frontend/src/app/layout.tsx`

**Header Components**:
- Logo/Brand: "Cortex Terminal V1"
- Navigation Links:
  - Dashboard (/)
  - Cortex AI (/cortex-ai)
  - Hawk-Eye-Radar (/hawk-eye-radar)
  - Scanner (/scanner)
- Auth Status (login/logout)
- Dev Login Button (development only)

**Styling**:
- Radial gradient background
- Backdrop blur header
- Max width: 7xl (1280px)
- Responsive padding

## Authentication Flow

**Context**: `frontend/src/contexts/AuthContext.tsx`

**States**:
- `isAuthenticated`: boolean
- `isLoading`: boolean
- `user`: User object or null

**Behavior**:
- Unauthenticated users see welcome message
- Open Positions only shown when authenticated
- Feature cards always visible (navigation)

## Pages Structure

```
frontend/src/app/
├── page.tsx                    # Main dashboard (landing page)
├── layout.tsx                  # Root layout with header/nav
├── providers.tsx               # React Query + Auth providers
├── cortex-ai/
│   └── page.tsx               # Cortex AI dashboard (tabbed)
├── hawk-eye-radar/
│   ├── page.tsx               # Hawk Eye main page
│   └── components/            # Trade suggestion components
├── scanner/
│   └── page.tsx               # Market scanner
├── cai-dashboard/
│   └── page.tsx               # CAI realtime demo (separate)
└── stocks/[symbol]/
    └── page.tsx               # Individual stock page
```

## Component Hierarchy

```
RootLayout
├── Header
│   ├── Logo/Brand
│   ├── Navigation
│   │   ├── Dashboard Link
│   │   ├── Cortex AI Link
│   │   ├── Hawk-Eye-Radar Link
│   │   └── Scanner Link
│   └── Auth Section
│       ├── AuthStatus
│       └── DevLoginButton
└── Main Content
    └── Home (page.tsx)
        ├── Welcome Card
        │   └── Feature Navigation Grid
        │       ├── Cortex AI Card
        │       ├── Hawk Eye Radar Card
        │       ├── Market Scanner Card
        │       └── Risk Management Card (Coming Soon)
        └── OpenPositionsPlaceholder (if authenticated)
            ├── Summary Stats
            └── Positions Table
```

## Styling & Design System

**Framework**: Tailwind CSS + shadcn/ui

**Color Palette**:
- Primary: Slate (neutral)
- Success: Emerald (green)
- Danger: Rose/Red
- Warning: Amber/Yellow
- Info: Blue

**Typography**:
- Font: Geist Sans (primary), Geist Mono (code)
- Headings: font-semibold, tracking-tight
- Body: text-sm, text-slate-600

**Components** (shadcn/ui):
- Card, CardHeader, CardTitle, CardDescription, CardContent
- Button
- Badge
- Tabs, TabsList, TabsTrigger, TabsContent

## State Management

**Library**: TanStack React Query (v5)

**Providers**:
```typescript
// frontend/src/app/providers.tsx
<QueryClientProvider>
  <AuthProvider>
    {children}
  </AuthProvider>
</QueryClientProvider>
```

**Query Keys**:
- `["trade-suggestions", filters]` - Hawk Eye suggestions
- `["signals", filters]` - Cortex AI signals
- `["events", filters]` - Cortex AI events
- `["models", filters]` - ML models
- `["regime", "overview"]` - Market regime

## API Integration

**Base URL**: Configured via environment variables
- Development: `http://localhost:8000`
- Production: TBD

**API Client**: `frontend/src/lib/api-client.ts`

**Endpoints Used by Main Dashboard**:
- None directly (navigation only)
- Child pages make API calls

**WebSocket URLs**:
- Trade Suggestions: `ws://localhost:8000/api/v1/trade-suggestions/ws`
- Cortex AI Signals: `ws://localhost:8000/api/v1/fusion/ws/signals`

## Environment Variables

```bash
# Frontend (.env.local)
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000
```

## Responsive Design

**Breakpoints**:
- Mobile: < 640px (sm)
- Tablet: 640px - 1024px (md)
- Desktop: > 1024px (lg)

**Grid Layouts**:
- Feature cards: 1 col (mobile) → 2 cols (md) → 4 cols (lg)
- Summary stats: 1 col (mobile) → 3 cols (sm)

## Performance Optimizations

1. **Code Splitting**: Each page is a separate route (automatic with Next.js App Router)
2. **Client Components**: Marked with `'use client'` for interactivity
3. **Lazy Loading**: Components loaded on-demand
4. **Memoization**: `useMemo` for expensive calculations (positions summary)
5. **Debouncing**: Search inputs debounced (instrument search)

## Future Enhancements (Potential)

### 1. Real Open Positions Integration
**Current**: Mock data with random ticking  
**Future**: 
- Connect to broker API (Upstox/Zerodha)
- Real-time position updates via WebSocket
- P&L calculation from live market data
- Order placement integration

### 2. Dashboard Widgets
**Concept**: Customizable widget grid
- Drag-and-drop layout
- Widget library (charts, watchlists, news, etc.)
- User preferences saved to backend
- Responsive grid system

### 3. Quick Actions Panel
**Features**:
- Quick order entry
- Watchlist management
- Alert creation
- Hotkey support

### 4. Market Overview Widget
**Data**:
- Nifty 50 / Bank Nifty indices
- Top gainers/losers
- Market breadth (advance/decline)
- Sector performance heatmap

### 5. News Feed Integration
**Sources**:
- Economic Times
- Moneycontrol
- Twitter/X (market sentiment)
- Filtered by relevance and credibility

### 6. Performance Analytics
**Metrics**:
- Daily/Weekly/Monthly P&L
- Win rate
- Average gain/loss
- Sharpe ratio
- Max drawdown

### 7. Risk Management Dashboard
**Features**:
- Portfolio heat map
- Correlation matrix
- VaR (Value at Risk) calculation
- Position sizing recommendations
- Stop-loss suggestions

## Development Workflow

### Adding a New Feature Card

1. **Create the page**:
```bash
mkdir -p frontend/src/app/new-feature
touch frontend/src/app/new-feature/page.tsx
```

2. **Add navigation link** in `layout.tsx`:
```tsx
<Link href="/new-feature">New Feature</Link>
```

3. **Add card to main dashboard** in `page.tsx`:
```tsx
<Link href="/new-feature">
  <Card className="cursor-pointer hover:border-primary">
    <CardHeader>
      <div className="flex items-center gap-2">
        <Icon className="h-5 w-5 text-primary" />
        <CardTitle>New Feature</CardTitle>
      </div>
    </CardHeader>
    <CardContent>
      <p className="text-sm text-muted-foreground">
        Description of the feature
      </p>
    </CardContent>
  </Card>
</Link>
```

### Adding a Dashboard Widget

1. **Create component**:
```bash
touch frontend/src/components/dashboard/NewWidget.tsx
```

2. **Import in main page**:
```tsx
import { NewWidget } from '@/components/dashboard/NewWidget';
```

3. **Add to layout**:
```tsx
<div className="grid grid-cols-1 md:grid-cols-2 gap-4">
  <OpenPositionsPlaceholder />
  <NewWidget />
</div>
```

## Testing

### Unit Tests
```bash
cd frontend
npm test -- src/app/page.test.tsx
npm test -- src/components/dashboard/
```

### E2E Tests
```bash
npm run test:e2e -- tests/dashboard.spec.ts
```

### Test Coverage
- Main page rendering
- Navigation links
- Authentication flow
- Open positions calculations
- Responsive layout

## Monitoring & Analytics

### Metrics to Track
- Page load time
- Time to interactive (TTI)
- Navigation click rates
- Feature usage (which cards clicked most)
- Error rates by page

### Logging
```typescript
// Example: Track feature navigation
console.log('[Dashboard] User navigated to:', featureName);
```

## Accessibility

**ARIA Labels**:
- Navigation landmarks
- Button labels
- Status indicators

**Keyboard Navigation**:
- Tab order for cards
- Enter to navigate
- Escape to close modals

**Screen Reader Support**:
- Semantic HTML
- Alt text for icons
- Live regions for updates

## Known Issues & Limitations

1. **Open Positions**: Mock data only, not connected to real broker
2. **Risk Management**: Placeholder card, not implemented
3. **WebSocket Feed**: Shows "disconnected" status (not wired up)
4. **Mobile Layout**: Positions table requires horizontal scroll on small screens
5. **No Dashboard Customization**: Fixed layout, no user preferences

## Related Documentation

- [CAI Dashboard Context](./DASHBOARD_CONTEXT.md) - Cortex AI dashboard details
- [API Documentation](./documentation/api/API_ENDPOINTS_DOCUMENTATION.md)
- [Architecture](./documentation/architecture/ARCHITECTURE.md)
- [Frontend Components](./documentation/implementation/FRONTEND_COMPONENTS_SCAN.md)

## Quick Reference

### File Locations
```
Main Dashboard:     frontend/src/app/page.tsx
Layout:            frontend/src/app/layout.tsx
Open Positions:    frontend/src/components/dashboard/OpenPositionsPlaceholder.tsx
Auth Context:      frontend/src/contexts/AuthContext.tsx
API Client:        frontend/src/lib/api-client.ts
```

### Key Commands
```bash
# Run dev server
cd frontend && npm run dev

# Build for production
npm run build

# Run tests
npm test

# Lint
npm run lint
```

### Environment Setup
```bash
# Copy example env
cp .env.example .env.local

# Install dependencies
npm install

# Start backend (required for API calls)
cd ../backend && uvicorn app.main:app --reload
```
