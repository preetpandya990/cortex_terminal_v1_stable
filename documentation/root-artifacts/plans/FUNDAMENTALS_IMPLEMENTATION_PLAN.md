# Company Fundamentals — Full Implementation Plan

> Feature: Integrate Upstox Company Fundamentals API into Hawk-Eye Radar modals and ML training pipeline.
> Data source: `https://api.upstox.com/v2/fundamentals/{isin}/`
> Date drafted: 2026-05-16

---

## Critical API Gotchas (Confirmed from Live curl Tests)

These must be hardcoded into the service layer — discovered from real responses against Reliance (INE002A01018):

1. **Competitors endpoint takes URL-encoded `NSE_EQ|ISIN`, all other 7 endpoints take bare ISIN.**
   - Wrong: `GET /fundamentals/INE002A01018/competitors` → `UDAPI100011 Invalid Instrument key`
   - Right: `GET /fundamentals/NSE_EQ%7CINE002A01018/competitors` ✅

2. **Key ratio values are strings with optional `%` suffix** — `"18.89"`, `"8.94%"` — need `parse_ratio_value()` helper that strips `%` and casts to float.

3. **Change fields are strings** — `"+10.53%"` — must strip `+` and `%` before storing as numeric.

4. **Period format is `"Mar 2026"`** — must be parsed to `date(2026, 3, 31)` using `datetime.strptime(period, "%b %Y")` then last day of that month. Critical for point-in-time correct ML joins.

5. **`full_statement` is always `null` at default** — never needed; store summary rows only.

6. **Balance sheet has no equity/net worth field** — must be computed: `net_worth = total_asset - total_liability`.

7. **Rate limits: 50 req/s, 500 req/min, 2000 req/30min** — backfill semaphore must be capped at 40 concurrent to stay safe.

---

## Real API Response Shapes (from live tests)

### GET /profile
```json
{
  "status": "success",
  "data": {
    "company_profile": "string",
    "sector": "string",
    "sector_market_cap_inr": { "value": 1808420.31, "unit": "crore", "formatted": "1,808,420.31 Cr" },
    "sector_market_cap_usd": { "value": 200.94, "unit": "billion", "formatted": "$200.94B" }
  }
}
```

### GET /key-ratios
```json
{
  "status": "success",
  "data": [
    { "name": "P/E",       "company_value": "18.89",  "sector_value": "11.74" },
    { "name": "P/B",       "company_value": "2.0",    "sector_value": "1.41"  },
    { "name": "ROA",       "company_value": "4.39%",  "sector_value": "7.68%" },
    { "name": "ROE",       "company_value": "8.94%",  "sector_value": "16.9%" },
    { "name": "ROCE",      "company_value": "10.39%", "sector_value": "17.47%"},
    { "name": "EV/EBITDA", "company_value": "10.08",  "sector_value": "6.67"  }
  ]
}
```

### GET /income-statement
```json
{
  "status": "success",
  "data": {
    "type": "consolidated", "time_period": "yearly", "units_in": "crore",
    "income_statement": [
      { "category": "revenue",          "history": [{ "value": 1086181.0, "period": "Mar 2026", "change": "+10.53%" }, ...] },
      { "category": "operating_profit", "history": [{ "value": 123162.0,  "period": "Mar 2026", "change": "+16.17%" }, ...] },
      { "category": "net_profit",       "history": [{ "value": 95610.0,   "period": "Mar 2026", "change": "+18.35%" }, ...] }
    ],
    "full_statement": null
  }
}
```
> **History depth:** ~4 years (Mar 2022–Mar 2025 per live docs sample). Not explicitly guaranteed for all companies — handle gracefully when fewer rows exist.

### GET /balance-sheet
```json
{
  "status": "success",
  "data": {
    "type": "consolidated", "time_period": "yearly", "units_in": "crore",
    "history": [
      { "total_asset": 1950121.0, "total_liability": 940495.0, "period": "Mar 2025" },
      ...
    ],
    "full_statement": null
  }
}
```
> **History depth:** ~4 years (Mar 2022–Mar 2025 per live docs sample). Not explicitly guaranteed for all companies — handle gracefully when fewer rows exist.

### GET /cash-flow
```json
{
  "status": "success",
  "data": {
    "type": "consolidated", "time_period": "yearly", "units_in": "crore",
    "cash_flow": [
      { "category": "operating",  "history": [{ "value": 178703.0,  "period": "Mar 2025", "change": "+12.54%" }, ...] },
      { "category": "investing",  "history": [{ "value": -137535.0, "period": "Mar 2025", "change": "-21.09%" }, ...] },
      { "category": "financing",  "history": [{ "value": -31891.0,  "period": "Mar 2025", "change": "-91.58%" }, ...] }
    ],
    "full_statement": null
  }
}
```
> **History depth:** ~4 years (Mar 2022–Mar 2025 per live docs sample). Not explicitly guaranteed for all companies — handle gracefully when fewer rows exist.

### GET /share-holdings
```json
{
  "status": "success",
  "data": [
    { "category": "promoters",        "history": [{ "value": 50.0,  "period": "Mar 2026" }, ...] },
    { "category": "fii",              "history": [{ "value": 18.67, "period": "Mar 2026" }, ...] },
    { "category": "other_dii",        "history": [{ "value": 10.77, "period": "Mar 2026" }, ...] },
    { "category": "retail_and_other", "history": [{ "value": 10.79, "period": "Mar 2026" }, ...] },
    { "category": "mutual_funds",     "history": [{ "value": 9.78,  "period": "Mar 2026" }, ...] }
  ]
}
```

### GET /corporate-actions
```json
{
  "status": "success",
  "data": [
    {
      "name": "Dividend", "expiry_date": "14 Aug 2025", "amount": 5.5, "ratio": null,
      "event_details": [
        { "name": "Announcement date", "value": "25 Apr 2025" },
        { "name": "Ex dividend date",  "value": "14 Aug 2025" },
        { "name": "Record date",       "value": "14 Aug 2025" },
        { "name": "Dividend type",     "value": "Final" },
        { "name": "Amount",            "value": "5.5" },
        { "name": "Dividend %",        "value": "55.0" },
        { "name": "Details",           "value": "Rs.5.5000 per share(55%)Final Dividend" }
      ]
    }
  ]
}
```

### GET /competitors  (uses NSE_EQ|ISIN path param, URL-encoded)
```json
{
  "status": "success",
  "data": [
    {
      "instrument_key": "NSE_EQ|INE242A01010",
      "company_profile": "string",
      "sector": "Refineries",
      "sector_market_cap_inr": { "value": 190001.26, "unit": "crore", "formatted": "190,001.26 Cr" },
      "sector_market_cap_usd": { "value": 21.11,     "unit": "billion", "formatted": "$21.11B" }
    },
    ...
  ]
}
```

---

## Backend

### Phase 1 — Data Layer

#### Step 1.1 — Migration `0031_company_fundamentals.py`

Eight new tables. All use `instrument_key` as the primary business key (consistent with rest of system). `isin` is stored as a derived column to avoid re-parsing on every Upstox call.

##### `company_fundamentals_profile`
One row per company. Upserted (not appended) on every fetch.
```
instrument_key       VARCHAR(200)  PRIMARY KEY / UNIQUE INDEX
isin                 VARCHAR(20)   INDEX
company_profile      TEXT
sector               VARCHAR(100)
sector_mcap_inr      NUMERIC(20,2)          -- value in crore
sector_mcap_usd      NUMERIC(20,4)
sector_mcap_usd_unit VARCHAR(10)            -- 'billion' | 'million'
fetched_at           TIMESTAMPTZ
```

##### `company_key_ratios`
One row per company (point-in-time snapshot). Upserted on every fetch. No history — API returns current values only.
```
instrument_key   VARCHAR(200)  PRIMARY KEY / UNIQUE INDEX
isin             VARCHAR(20)
pe               NUMERIC(10,4)  nullable
pb               NUMERIC(10,4)  nullable
roa              NUMERIC(10,4)  nullable    -- stored as decimal, NOT "8.94%"
roe              NUMERIC(10,4)  nullable
roce             NUMERIC(10,4)  nullable
ev_ebitda        NUMERIC(10,4)  nullable
pe_sector        NUMERIC(10,4)  nullable
pb_sector        NUMERIC(10,4)  nullable
roa_sector       NUMERIC(10,4)  nullable
roe_sector       NUMERIC(10,4)  nullable
roce_sector      NUMERIC(10,4)  nullable
ev_ebitda_sector NUMERIC(10,4)  nullable
fetched_at       TIMESTAMPTZ
```

##### `company_income_statement`
Historical. One row per instrument + period + statement_type + time_period.
```
id                     BIGSERIAL      PRIMARY KEY
instrument_key         VARCHAR(200)   INDEX
isin                   VARCHAR(20)
period_label           VARCHAR(20)    -- "Mar 2026"
period_date            DATE           -- 2026-03-31  (last day of reported month)
time_period            VARCHAR(20)    -- 'yearly' | 'quarterly'
statement_type         VARCHAR(20)    -- 'consolidated' | 'standalone'
revenue                NUMERIC(20,2)  nullable   -- crore
revenue_change_pct     NUMERIC(8,4)   nullable
op_profit              NUMERIC(20,2)  nullable
op_profit_change_pct   NUMERIC(8,4)   nullable
net_profit             NUMERIC(20,2)  nullable
net_profit_change_pct  NUMERIC(8,4)   nullable
fetched_at             TIMESTAMPTZ
UNIQUE (instrument_key, period_date, time_period, statement_type)
INDEX  (instrument_key, period_date)
```

##### `company_balance_sheet`
Historical.
```
id               BIGSERIAL     PRIMARY KEY
instrument_key   VARCHAR(200)  INDEX
isin             VARCHAR(20)
period_label     VARCHAR(20)
period_date      DATE
statement_type   VARCHAR(20)
total_asset      NUMERIC(20,2)
total_liability  NUMERIC(20,2)
net_worth        NUMERIC(20,2)   -- computed: total_asset - total_liability
fetched_at       TIMESTAMPTZ
UNIQUE (instrument_key, period_date, statement_type)
INDEX  (instrument_key, period_date)
```

##### `company_cash_flow`
Historical.
```
id                         BIGSERIAL     PRIMARY KEY
instrument_key             VARCHAR(200)  INDEX
isin                       VARCHAR(20)
period_label               VARCHAR(20)
period_date                DATE
statement_type             VARCHAR(20)
operating_cf               NUMERIC(20,2)
investing_cf               NUMERIC(20,2)
financing_cf               NUMERIC(20,2)
operating_cf_change_pct    NUMERIC(8,4)  nullable
investing_cf_change_pct    NUMERIC(8,4)  nullable
financing_cf_change_pct    NUMERIC(8,4)  nullable
fetched_at                 TIMESTAMPTZ
UNIQUE (instrument_key, period_date, statement_type)
INDEX  (instrument_key, period_date)
```

##### `company_share_holdings`
Quarterly history.
```
id                    BIGSERIAL     PRIMARY KEY
instrument_key        VARCHAR(200)  INDEX
isin                  VARCHAR(20)
period_label          VARCHAR(20)
period_date           DATE
promoters_pct         NUMERIC(6,2)
fii_pct               NUMERIC(6,2)
other_dii_pct         NUMERIC(6,2)
mutual_funds_pct      NUMERIC(6,2)
retail_and_other_pct  NUMERIC(6,2)
fetched_at            TIMESTAMPTZ
UNIQUE (instrument_key, period_date)
INDEX  (instrument_key, period_date)
```

##### `company_corporate_actions`
Event log. Append-only — do not upsert, re-fetch and replace by instrument on refresh.
```
id               BIGSERIAL     PRIMARY KEY
instrument_key   VARCHAR(200)  INDEX
isin             VARCHAR(20)
action_type      VARCHAR(50)   -- 'Dividend' | 'Bonus' | 'Split' | 'Rights'
expiry_date_str  VARCHAR(50)   -- "14 Aug 2025" — kept as string, Upstox format varies
amount           NUMERIC(12,4) nullable
ratio            VARCHAR(20)   nullable   -- "1:1"
event_details    JSONB         -- raw array from API
fetched_at       TIMESTAMPTZ
INDEX (instrument_key)
```

##### `company_competitors`
Refreshed (delete + insert) per company on fetch.
```
id               BIGSERIAL     PRIMARY KEY
instrument_key   VARCHAR(200)  INDEX    -- subject company
competitor_key   VARCHAR(200)           -- NSE_EQ|ISIN of competitor
company_profile  TEXT
sector           VARCHAR(100)
mcap_inr_crore   NUMERIC(20,2)
mcap_usd         NUMERIC(20,4)
mcap_usd_unit    VARCHAR(10)            -- 'billion' | 'million'
fetched_at       TIMESTAMPTZ
INDEX (instrument_key)
```

---

#### Step 1.2 — SQLAlchemy Models

**New file:** `backend/app/models/fundamentals.py`

Eight ORM model classes, one per table. All inherit from `Base`. Add imports to `backend/app/models/__init__.py`.

---

#### Step 1.3 — Fundamentals Service

**New file:** `backend/app/services/fundamentals_service.py`

##### Helper functions (pure, no I/O)
```python
def extract_isin(instrument_key: str) -> str:
    # "NSE_EQ|INE002A01018" → "INE002A01018"
    return instrument_key.split("|")[1]

def parse_ratio_value(v: str | None) -> float | None:
    # "8.94%" → 8.94,  "18.89" → 18.89,  None → None
    if v is None: return None
    return float(v.rstrip('%'))

def parse_change_pct(v: str | None) -> float | None:
    # "+10.53%" → 10.53,  "-21.09%" → -21.09,  None → None
    if v is None: return None
    return float(v.strip('%'))

def parse_period_to_date(period: str) -> date:
    # "Mar 2026" → date(2026, 3, 31)
    dt = datetime.strptime(period, "%b %Y")
    last_day = calendar.monthrange(dt.year, dt.month)[1]
    return date(dt.year, dt.month, last_day)
```

##### Core methods
- `fetch_profile(isin, token) -> dict` — calls `/profile`
- `fetch_key_ratios(isin, token) -> dict` — calls `/key-ratios`
- `fetch_income_statement(isin, token, time_period='yearly') -> dict` — calls `/income-statement`
- `fetch_balance_sheet(isin, token) -> dict` — calls `/balance-sheet`
- `fetch_cash_flow(isin, token) -> dict` — calls `/cash-flow`
- `fetch_share_holdings(isin, token) -> dict` — calls `/share-holdings`
- `fetch_corporate_actions(isin, token) -> dict` — calls `/corporate-actions`
- `fetch_competitors(instrument_key, token) -> dict` — calls `/competitors` with URL-encoded instrument_key (NOT bare ISIN)

##### Orchestration
```python
async def fetch_and_store_all(instrument_key: str, db: AsyncSession, redis) -> None:
    isin = extract_isin(instrument_key)
    # Fetch all 8 endpoints concurrently (gather)
    # Parse and upsert each into respective DB tables
    # Assemble full response dict and write to Redis:
    #   key: f"cai:fundamentals:{instrument_key}"
    #   TTL: 86400 (24h)

async def get_fundamentals(instrument_key: str, db: AsyncSession, redis) -> dict:
    # 1. Check Redis — return if hit
    # 2. Check DB — assemble from tables if populated
    # 3. If DB empty — call fetch_and_store_all(), return result
```

##### EQ guard
Before any fetch, check `instrument_master.instrument_type`. If not `'EQ'`, return `{"available": False, "reason": "derivatives_not_supported"}` immediately.

---

#### Step 1.4 — Backfill Script

**New file:** `backend/scripts/backfill_fundamentals.py`

- Queries all `instrument_type = 'EQ'` rows from `instrument_master` (~2,000–2,500 instruments)
- Checkpoint file at `backend/scripts/.fundamentals_backfill_checkpoint` — stores last processed `instrument_key` so it's resumable on crash
- `asyncio.Semaphore(40)` — 40 concurrent fetches, stays safely under 50 req/s rate limit
- Logs progress: `Processed 250/2551 | Errors: 3 | Elapsed: 42s`
- On completion: prints summary of successes, skips (non-EQ), and errors

**Expected run time:** ~2,500 instruments × 8 endpoints / 40 concurrent ≈ 8–12 minutes

---

### Phase 2 — API Layer

#### Step 2.1 — Pydantic Schemas

**New file:** `backend/app/schemas/fundamentals.py`

```python
class ProfileData(BaseModel):
    company_profile: str
    sector: str
    sector_mcap_inr: float        # value in crore
    sector_mcap_usd: float
    sector_mcap_usd_unit: str     # 'billion' | 'million'

class KeyRatioItem(BaseModel):
    name: str
    company_value: float | None
    sector_value: float | None

class PeriodValue(BaseModel):
    value: float
    period: str           # "Mar 2026"
    period_date: date     # 2026-03-31
    change_pct: float | None

class CategoryHistory(BaseModel):
    category: str
    history: list[PeriodValue]

class BalanceSheetEntry(BaseModel):
    total_asset: float
    total_liability: float
    net_worth: float
    period: str
    period_date: date

class ShareHoldingEntry(BaseModel):
    promoters_pct: float
    fii_pct: float
    other_dii_pct: float
    mutual_funds_pct: float
    retail_and_other_pct: float
    period: str
    period_date: date

class CorporateAction(BaseModel):
    action_type: str
    expiry_date_str: str
    amount: float | None
    ratio: str | None
    event_details: list[dict]

class CompetitorEntry(BaseModel):
    instrument_key: str
    company_profile: str
    sector: str
    mcap_inr_crore: float
    mcap_usd: float
    mcap_usd_unit: str

class FundamentalsFullResponse(BaseModel):
    available: bool
    instrument_key: str
    isin: str | None
    profile: ProfileData | None
    key_ratios: list[KeyRatioItem] | None
    income_statement: list[CategoryHistory] | None
    balance_sheet: list[BalanceSheetEntry] | None
    cash_flow: list[CategoryHistory] | None
    share_holdings: list[ShareHoldingEntry] | None
    corporate_actions: list[CorporateAction] | None
    competitors: list[CompetitorEntry] | None
    fetched_at: datetime | None
```

---

#### Step 2.2 — API Endpoint

**New file:** `backend/app/api/v1/fundamentals.py`

```
GET /api/v1/fundamentals/{instrument_key}
```

- Auth: standard JWT Bearer (same as all other endpoints)
- Path param: `instrument_key` (URL-decoded by FastAPI automatically)
- EQ guard in service layer — returns 200 with `available: false` for derivatives (not a 4xx)
- Rate limited: 30 req/min per user (fundamentals are UI-triggered, not hot-path)
- Redis-first → DB fallback → live Upstox fetch

**Register in `backend/app/main.py`:**
```python
from app.api.v1 import fundamentals
app.include_router(
    fundamentals.router,
    prefix=f"{settings.API_V1_PREFIX}/fundamentals",
    tags=["Company Fundamentals"]
)
```

---

### Phase 3 — ML Feature Integration

#### Step 3.1 — Fundamental Feature Computation

**New file:** `backend/app/ml/features/fundamental_features.py`

Derives 20 ML features from DB tables. All queries are **point-in-time correct**: they use only rows where `period_date <= training_end_date` to prevent lookahead bias. All historical endpoints return up to ~4 years of data — features are designed to use the full available depth rather than just the latest snapshot.

| Feature Name | Source Table | Derivation |
|---|---|---|
| `pe_ratio` | company_key_ratios | `pe` direct |
| `pb_ratio` | company_key_ratios | `pb` direct |
| `roe` | company_key_ratios | `roe` direct (decimal, not %) |
| `roce` | company_key_ratios | `roce` direct |
| `ev_ebitda` | company_key_ratios | `ev_ebitda` direct |
| `revenue_growth_yoy` | company_income_statement | `(rev_t - rev_t1) / abs(rev_t1)` — latest two yearly rows |
| `revenue_cagr` | company_income_statement | CAGR over all available yearly rows (up to 4 years): `(rev_latest / rev_oldest) ^ (1/n_years) - 1` |
| `profit_growth_yoy` | company_income_statement | `(profit_t - profit_t1) / abs(profit_t1)` — latest two yearly rows |
| `profit_cagr` | company_income_statement | CAGR over all available yearly rows (up to 4 years) for `net_profit` |
| `operating_margin` | company_income_statement | `op_profit / revenue` — latest yearly row |
| `operating_margin_avg` | company_income_statement | average `op_profit / revenue` across all available yearly rows |
| `net_worth_log` | company_balance_sheet | `log1p(net_worth)` — latest row |
| `net_worth_cagr` | company_balance_sheet | CAGR of `net_worth` over all available yearly rows |
| `debt_ratio` | company_balance_sheet | `total_liability / total_asset` — latest row |
| `debt_ratio_trend` | company_balance_sheet | linear slope of `debt_ratio` over all available yearly rows (negative = improving) |
| `operating_cf_growth` | company_cash_flow | YoY change in `operating_cf` — latest two yearly rows |
| `operating_cf_cagr` | company_cash_flow | CAGR of `operating_cf` over all available yearly rows |
| `promoter_holding_pct` | company_share_holdings | latest quarter `promoters_pct` |
| `fii_holding_pct` | company_share_holdings | latest quarter `fii_pct` |
| `promoter_holding_change` | company_share_holdings | `promoters_pct[latest] - promoters_pct[prior quarter]` |

All features return `NaN` if data is unavailable for that instrument — handled by median imputation in the feature pipeline. CAGR and trend features return `NaN` if fewer than 2 yearly rows exist (possible for newly listed companies). These are **cross-sectional** features: the same value is broadcast across all time steps within a training sequence (not time-varying within the 60-step window).

```python
FUNDAMENTAL_FEATURE_NAMES = [
    "pe_ratio", "pb_ratio", "roe", "roce", "ev_ebitda",
    "revenue_growth_yoy", "revenue_cagr",
    "profit_growth_yoy", "profit_cagr",
    "operating_margin", "operating_margin_avg",
    "net_worth_log", "net_worth_cagr",
    "debt_ratio", "debt_ratio_trend",
    "operating_cf_growth", "operating_cf_cagr",
    "promoter_holding_pct", "fii_holding_pct", "promoter_holding_change",
]

async def compute_fundamental_features(
    instrument_key: str,
    as_of_date: date,
    db: AsyncSession,
) -> dict[str, float | None]:
    ...

def get_fundamental_feature_names() -> list[str]:
    return FUNDAMENTAL_FEATURE_NAMES
```

---

#### Step 3.2 — Feature Pipeline Update

**Modify:** `backend/app/ml/features/feature_pipeline.py`

- Import `compute_fundamental_features, get_fundamental_feature_names` from `fundamental_features.py`
- Update `get_all_feature_names(include_sentiment=True, include_fundamentals=True)`:
  ```python
  features = get_feature_names()            # 42 technical
  if include_sentiment:
      features.extend(get_sentiment_feature_names())    # +5 = 47
  if include_fundamentals:
      features.extend(get_fundamental_feature_names())  # +20 = 67
  return features
  ```
- Update `prepare_training_data()`: after OHLCV + sentiment features are computed, left-join fundamental features per `instrument_key` using `as_of_date = training_window_end_date`. Since these are cross-sectional, broadcast the same fundamental values to all rows for that instrument in the window.
- **New total feature count: 42 + 5 + 20 = 67**

---

#### Step 3.3 — Orchestrator Update

**Modify:** `backend/scripts/production_training_orchestrator.py`

- Update `n_features: int = 67` in `OrchestratorConfig`
- Add `include_fundamentals: bool = True` config flag
- Add new resumable checkpoint step `refresh_fundamentals` before walk-forward training loop:
  - Iterates all EQ instruments in `instrument_master`
  - Calls `fetch_and_store_all()` for instruments where `fetched_at` is older than 7 days or NULL
  - Logs `Fundamentals refresh: 2551 instruments, 48 refreshed, 2503 skipped (fresh)`
- Update all `get_all_feature_names(include_sentiment=True)` calls to also pass `include_fundamentals=True`
- GRU input shape automatically adapts because `n_features` is read from config, not hardcoded in the model architecture call

---

## Frontend

### Phase 1 — TypeScript Types & API Client

#### Step 1.1 — TypeScript Types

**New file:** `frontend/src/types/fundamentals.ts`

```typescript
export interface KeyRatioItem {
  name: string
  company_value: number | null
  sector_value: number | null
}

export interface PeriodValue {
  value: number
  period: string
  period_date: string   // ISO date string
  change_pct: number | null
}

export interface CategoryHistory {
  category: string
  history: PeriodValue[]
}

export interface BalanceSheetEntry {
  total_asset: number
  total_liability: number
  net_worth: number
  period: string
  period_date: string
}

export interface ShareHoldingEntry {
  promoters_pct: number
  fii_pct: number
  other_dii_pct: number
  mutual_funds_pct: number
  retail_and_other_pct: number
  period: string
  period_date: string
}

export interface CorporateAction {
  action_type: string
  expiry_date_str: string
  amount: number | null
  ratio: string | null
  event_details: { name: string; value: string }[]
}

export interface CompetitorEntry {
  instrument_key: string
  company_profile: string
  sector: string
  mcap_inr_crore: number
  mcap_usd: number
  mcap_usd_unit: string
}

export interface FundamentalsFullResponse {
  available: boolean
  instrument_key: string
  isin: string | null
  profile: {
    company_profile: string
    sector: string
    sector_mcap_inr: number
    sector_mcap_usd: number
    sector_mcap_usd_unit: string
  } | null
  key_ratios: KeyRatioItem[] | null
  income_statement: CategoryHistory[] | null
  balance_sheet: BalanceSheetEntry[] | null
  cash_flow: CategoryHistory[] | null
  share_holdings: ShareHoldingEntry[] | null
  corporate_actions: CorporateAction[] | null
  competitors: CompetitorEntry[] | null
  fetched_at: string | null
}
```

---

#### Step 1.2 — API Client

**Modify:** `frontend/src/lib/api.ts`

Add `fundamentalsAPI`:
```typescript
export const fundamentalsAPI = {
  getFundamentals: (instrumentKey: string) =>
    apiClient.get<FundamentalsFullResponse>(
      `/fundamentals/${encodeURIComponent(instrumentKey)}`
    ),
}
```

---

### Phase 2 — Hook & Component

#### Step 2.1 — React Query Hook

**New file:** `frontend/src/hooks/useFundamentals.ts`

```typescript
export function useFundamentals(instrumentKey: string | null) {
  return useQuery({
    queryKey: ['fundamentals', instrumentKey],
    queryFn: () => fundamentalsAPI.getFundamentals(instrumentKey!),
    enabled: !!instrumentKey,
    staleTime: 1000 * 60 * 60 * 6,  // 6h — fundamentals are slow-moving
    gcTime:    1000 * 60 * 60 * 24, // 24h in memory cache
    retry: 1,
  })
}
```

---

#### Step 2.2 — FundamentalsTab Component

**New file:** `frontend/src/components/hawk-eye-radar/FundamentalsTab.tsx`

**Charting library:** TradingView Lightweight Charts v5 (already in project — no new dependency).

**Layout:** Eight **collapsible sections**, not nested tabs. Rationale: the component mounts inside an existing modal/detail pane — a second tab bar creates deep nesting and poor UX. Collapsible sections (industry standard on Screener.in, Tickertape, Moneycontrol) let users expand only what they need. Profile + Key Ratios open by default; remaining sections collapsed.

| # | Section | Rendering approach |
|---|---|---|
| 1 | **Company Profile** | Pure HTML — text block + sector chip + INR/USD market cap badges |
| 2 | **Key Ratios** | Pure HTML — 6-cell grid, each cell shows company value, sector benchmark, and colored delta arrow (green/red based on whether higher is better per metric) |
| 3 | **Income Statement** | **Table** — years as columns (up to 4), rows: Revenue / Op. Profit / Net Profit. Each cell shows value (crore) + YoY change badge (green/red). |
| 4 | **Balance Sheet** | **Table** — years as columns (up to 4), rows: Total Assets / Total Liabilities / Net Worth. Each cell shows value (crore). Net Worth row computed inline. |
| 5 | **Cash Flow** | **Table** — years as columns (up to 4), rows: Operating / Investing / Financing CF. Negative values shown in red. YoY change badge per cell. |
| 6 | **Share Holdings** | Two parts: (a) CSS horizontal stacked bar for latest-quarter breakdown (Promoter / FII / MF / DII / Retail) with delta vs prior quarter shown inline; (b) **Lightweight Charts Line series** for Promoter % and FII % trend across all available quarters. |
| 7 | **Corporate Actions** | Pure HTML timeline list — Dividend / Bonus / Split entries with dates and amounts, sorted newest-first. |
| 8 | **Competitors** | Pure HTML compact cards — company name, sector, MCap INR + USD. |

**Lightweight Charts usage notes (share holdings trend only):**
- Single chart instance for the share holdings trend line.
- Time axis uses `period_date` (ISO date string) — quarterly periods map naturally to Lightweight Charts' time format.
- Chart is destroyed and recreated on instrument change to avoid stale data.
- Chart height: 180px (compact, consistent with modal context).

Loading state: per-section skeleton. Error state: per-section "Data unavailable" notice (never crashes the whole tab). If `available: false` on the response, show a single "Fundamentals not available for derivatives" notice.

---

### Phase 3 — Integration

#### Step 3.1 — Wire Into Modals

**Modify:** `frontend/src/app/hawk-eye-radar/components/SuggestionDetailModal.tsx`
- Add "Fundamentals" tab to the existing tab bar
- Mount `<FundamentalsTab instrumentKey={suggestion.instrument_key} />` only when this tab is active — lazy mounting avoids unnecessary API calls on every modal open

**Modify:** `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx`
- Same Fundamentals tab addition at the bottom of the pane
- Same lazy-mount pattern

---

## File Change Summary

| File | Action | Notes |
|---|---|---|
| `backend/alembic/versions/0031_company_fundamentals.py` | **CREATE** | 8 tables, 20+ indexes |
| `backend/app/models/fundamentals.py` | **CREATE** | 8 ORM models |
| `backend/app/schemas/fundamentals.py` | **CREATE** | All Pydantic response schemas including ProfileData |
| `backend/app/services/fundamentals_service.py` | **CREATE** | Upstox client, parse helpers, cache, upsert |
| `backend/app/api/v1/fundamentals.py` | **CREATE** | GET /fundamentals/{instrument_key} |
| `backend/app/ml/features/fundamental_features.py` | **CREATE** | 20 ML feature derivations, point-in-time correct, full history depth (CAGRs, trends, averages) |
| `backend/scripts/backfill_fundamentals.py` | **CREATE** | Resumable backfill for all EQ instruments |
| `backend/app/models/__init__.py` | **MODIFY** | Import fundamentals models |
| `backend/app/main.py` | **MODIFY** | Register fundamentals router |
| `backend/app/ml/features/feature_pipeline.py` | **MODIFY** | Add fundamentals join, update get_all_feature_names |
| `backend/scripts/production_training_orchestrator.py` | **MODIFY** | n_features=67, add fundamentals refresh step |
| `frontend/src/types/fundamentals.ts` | **CREATE** | All TypeScript interfaces |
| `frontend/src/hooks/useFundamentals.ts` | **CREATE** | React Query hook, 6h staleTime |
| `frontend/src/components/hawk-eye-radar/FundamentalsTab.tsx` | **CREATE** | 8-section collapsible UI, hybrid table + Lightweight Charts |
| `frontend/src/lib/api.ts` | **MODIFY** | Add fundamentalsAPI |
| `frontend/src/app/hawk-eye-radar/components/SuggestionDetailModal.tsx` | **MODIFY** | Add Fundamentals tab (lazy-mounted) |
| `frontend/src/app/hawk-eye-radar/components/DetailPane.tsx` | **MODIFY** | Add Fundamentals tab (lazy-mounted) |

---

## Implementation Order

```
Backend Phase 1 (Data Layer — sequential)
  1.1  Migration 0031_company_fundamentals.py
  1.2  backend/app/models/fundamentals.py
  1.3  backend/app/services/fundamentals_service.py
  1.4  backend/scripts/backfill_fundamentals.py
       → RUN backfill (8–12 min, resumable)

Backend Phase 2 (API Layer — after Phase 1)
  2.1  backend/app/schemas/fundamentals.py
  2.2  backend/app/api/v1/fundamentals.py
       → Register router in main.py

Frontend Phase 1–3 (parallel with Backend Phase 2, after Backend Phase 1)
  1.1  frontend/src/types/fundamentals.ts
  1.2  frontend/src/lib/api.ts  (add fundamentalsAPI)
  2.1  frontend/src/hooks/useFundamentals.ts
  2.2  frontend/src/components/hawk-eye-radar/FundamentalsTab.tsx
  3.1  Wire into SuggestionDetailModal.tsx + DetailPane.tsx

Backend Phase 3 (ML — after backfill is complete)
  3.1  backend/app/ml/features/fundamental_features.py
  3.2  Modify feature_pipeline.py
  3.3  Modify production_training_orchestrator.py
       → Retrain full ensemble (n_features: 47 → 67)
```

Backend Phases 2 and Frontend Phases 1–3 can be worked in parallel once Backend Phase 1 migration and service are done.
Backend Phase 3 (ML) must wait until the backfill script has populated data for a meaningful number of instruments.
