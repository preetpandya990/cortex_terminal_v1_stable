# Fundamentals Data — Research & Findings
> Date: 2026-05-18 | Company used for research: HINDUSTAN COPPER LTD (NSE_EQ|INE531E01026, ISIN: INE531E01026)

---

## 1. Data Source

All fundamentals data is sourced from **Upstox v2 Fundamentals API** (`https://api.upstox.com/v2/fundamentals/{isin}/`).

8 endpoints:
- `/profile`
- `/key-ratios`
- `/income-statement` — supports `?type=standalone|consolidated` and `?time_period=yearly|quarterly`
- `/balance-sheet` — supports `?type=standalone|consolidated`
- `/cash-flow` — supports `?type=standalone|consolidated`
- `/share-holdings`
- `/corporate-actions`
- `/competitors` — takes URL-encoded `NSE_EQ|ISIN`, NOT bare ISIN

---

## 2. Standalone vs Consolidated

**Standalone** = parent company financials only. Subsidiaries excluded; investment in them appears as a single line item.

**Consolidated** = parent + all subsidiaries treated as one entity. Intercompany transactions (sales, loans, dividends) are eliminated to avoid double-counting.

### Example (HINDCOPPER)
```
Hindustan Copper Ltd (Parent)
        └── Chhattisgarh Copper Limited (100% owned subsidiary)
```

| | Standalone | Consolidated |
|---|---|---|
| Revenue | Parent only | Parent + Sub − intercompany sales |
| Net Profit | Parent only | Parent + Sub − intercompany dividends |
| Total Assets | Parent only | Parent + Sub − intercompany balances |

### Impact on ratios
| Ratio | Standalone | Consolidated | Why |
|---|---|---|---|
| P/E | Higher | Lower | Consolidated EPS is larger |
| ROE | Lower | Higher | More profit in numerator |
| ROCE | Lower | Higher | More operating profit |
| EV/EBITDA | Higher | Lower | More EBITDA in consolidated |

**Decision: we use standalone data going forward.**

---

## 3. What Updates When

| Data | Update Frequency | Reason |
|---|---|---|
| Key Ratios (P/E, P/B, ROE, ROCE, EV/EBITDA) | **Daily** | Price-driven — numerator (MCap) changes every trading day |
| Sector benchmark values | **Daily** | Aggregate of all sector peers' prices |
| Income Statement | **Quarterly** | Companies report every 3 months; annual in May/June |
| Balance Sheet | **Quarterly/Annual** | Same as above |
| Cash Flow | **Quarterly/Annual** | Same as above |
| Share Holdings | **Quarterly** | SEBI mandates disclosure within 21 days of quarter end |
| Corporate Actions | **Event-driven** | Only on dividend/bonus/split announcement |
| Competitors MCap | **Daily** | Peer MCap = price × shares, changes daily |
| Profile/Sector | **Rarely** | Only if company changes sector or description |

---

## 4. HINDCOPPER — Live Standalone Data (as of 2026-05-18)

### Key Ratios
| Ratio | Company | Sector |
|---|---|---|
| P/E | 60.04 | 38.08 |
| P/B | 16.50 | 3.98 |
| ROA | 20.85% | 6.52% |
| ROE | 27.48% | 15.31% |
| ROCE | 34.56% | 12.74% |
| EV/EBITDA | 37.86 | 24.68 |

### Income Statement — Standalone Yearly (₹ Cr)
| Period | Revenue | Op. Profit | Net Profit |
|---|---|---|---|
| Mar 2023 | 1,773.20 | 395.66 | 295.31 |
| Mar 2024 | 1,771.84 | 410.43 | 295.41 |
| Mar 2025 | 2,149.29 | 633.51 | 468.53 |
| Mar 2026 | 3,149.67 | 1,232.73 | 920.67 |

### Income Statement — Standalone Quarterly (₹ Cr)
| Period | Revenue | Op. Profit | Net Profit |
|---|---|---|---|
| Jun 2025 | 526.65 | 179.36 | 134.28 |
| Sep 2025 | 728.95 | 248.63 | 186.02 |
| Dec 2025 | 705.31 | 212.53 | 156.31 |
| Mar 2026 | 1,188.76 | 592.21 | 444.06 |

### Balance Sheet — Standalone (₹ Cr)
| Period | Total Assets | Total Liabilities | Net Worth |
|---|---|---|---|
| Mar 2022 | 2,954.53 | 1,043.27 | 1,911.26 |
| Mar 2023 | 2,985.14 | 903.09 | 2,082.05 |
| Mar 2024 | 3,270.02 | 984.93 | 2,285.09 |
| Mar 2025 | 3,504.17 | 839.87 | 2,664.30 |

### Cash Flow — Standalone (₹ Cr)
| Period | Operating | Investing | Financing |
|---|---|---|---|
| Mar 2022 | 1,052.36 | -404.01 | -251.12 |
| Mar 2023 | 673.58 | -337.30 | -339.45 |
| Mar 2024 | 341.22 | -524.89 | -38.64 |
| Mar 2025 | 544.31 | -402.36 | -152.28 |

### Share Holdings (%)
| Period | Promoter | FII | Other DII | MF | Retail |
|---|---|---|---|---|---|
| Jun 2025 | 66.14 | 3.71 | 5.51 | 2.74 | 21.90 |
| Sep 2025 | 66.14 | 5.06 | 5.51 | 0.49 | 22.79 |
| Dec 2025 | 66.14 | 6.56 | 4.84 | 0.73 | 21.73 |
| Mar 2026 | 66.14 | 6.34 | 4.50 | 0.89 | 22.13 |

### Corporate Actions
| Type | Ex-Date | Amount | Subtype |
|---|---|---|---|
| Dividend | 13 Feb 2026 | ₹1.00/share (20%) | Interim |
| Dividend | 18 Sep 2025 | ₹1.46/share (29.2%) | Final |

### Competitors (10 peers)
| Company | Instrument Key | MCap (₹ Cr) | MCap (USD) |
|---|---|---|---|
| HINDUSTAN ZINC | NSE_EQ\|INE267A01025 | 2,69,342 | $29.93B |
| HINDALCO | NSE_EQ\|INE038A01020 | 2,39,835 | $26.65B |
| VEDANTA | NSE_EQ\|INE205A01025 | 1,29,472 | $14.39B |
| NATIONAL ALUMINIUM | NSE_EQ\|INE139A01034 | 74,126 | $8.24B |
| JAIN RESOURCE RECYCLING | NSE_EQ\|INE0YD401026 | 19,530 | $2.17B |
| GRAVITA INDIA | NSE_EQ\|INE024L01027 | 12,459 | $1.38B |
| PRECISION WIRES | NSE_EQ\|INE372C01037 | 6,879 | $764M |
| PONDY OXIDES | NSE_EQ\|INE063E01053 | 4,709 | $523M |
| KSH INTERNATIONAL | NSE_EQ\|INE987S01020 | 4,300 | $477M |
| RAM RATNA WIRES | NSE_EQ\|INE207E01023 | 3,923 | $435M |

---

## 5. Current DB State & Action Items

- **DB only has consolidated rows** — standalone was never fetched during backfill
- Sector benchmark values in DB are stale (fetched 2026-05-17); live values differ slightly
- **Action required:** update `fundamentals_service.py` to fetch `?type=standalone` and store with `statement_type = 'standalone'`; re-run backfill for standalone data

---

## 6. Notes

- Broker data (external) noted for reference — **excluded from all system calculations until explicitly instructed**
- Key ratios from Upstox are pre-computed by them (not by us) — we are consumers only
- Sector benchmark values have no per-field `updated_at` on the API — only our `fetched_at` timestamp is available
