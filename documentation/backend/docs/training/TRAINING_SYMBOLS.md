# Training Symbols - 47 Stocks

Complete list of stocks used in ML model training (sorted by liquidity/volume):

**2026-07-14 cleanup**: removed 3 non-stock rows that had contaminated this
curated manifest — GOLDBEES/SILVERBEES (mutual-fund/ETF units, `INF`-prefixed
ISIN) and the row previously labeled "[ETF] Gold/Silver ETF"
(`INE775A08105`). See `app.services.instrument_classifier` and
`backend/scripts/cleanup_etf_instruments.py` for the automated filter this
manifest should now stay consistent with.

**Known gap, not silently papered over**: the removed `[ETF] Gold/Silver ETF`
row's ISIN (`INE775A08105`) is `INE`-prefixed, not `INF` — it does **not**
match the automated ETF-detection rule (AMFI/mutual-fund ISINs are
`INF`-prefixed) and its symbol is not in the curated REIT/InvIT registry
either. If this or a similarly-structured instrument (e.g. a Sovereign Gold
Bond or another RBI/Government debt instrument trading in the equity segment)
re-enters the live instrument universe, the automated filter will currently
misclassify it as `STOCK`. This needs a dedicated look at what asset class
`INE775A08105` actually is before it's safe to rely on the filter alone for
this instrument category — flagging rather than guessing.

**Only 47 rows remain** — replacing the 3 removed rows with the
next-highest-liquidity genuine stocks requires re-running the actual
liquidity-ranking pipeline (not available here); do not backfill rows #48-50
with placeholder or guessed symbols.

| #  | Symbol       | Name                      | Instrument Key       |
|----|--------------|------|--------------------|----------------------|
| 01 | IDEA         | VODAFONE IDEA LIMITED     | NSE_EQ\|INE669E01016 |
| 02 | YESBANK      | YES BANK LIMITED          | NSE_EQ\|INE528G01035 |
| 03 | SUZLON       | SUZLON ENERGY LIMITED     | NSE_EQ\|INE040H01021 |
| 04 | GTLINFRA     | GTL INFRA.LTD             | NSE_EQ\|INE221H01019 |
| 05 | JPPOWER      | JAIPRAKASH POWER VEN. LTD | NSE_EQ\|INE351F01018 |
| 06 | RPOWER       | RELIANCE POWER LTD.       | NSE_EQ\|INE614G01033 |
| 07 | ETERNAL      | ETERNAL LIMITED           | NSE_EQ\|INE758T01015 |
| 08 | IRFC         | INDIAN RAILWAY FIN CORP L | NSE_EQ\|INE053F01010 |
| 09 | PCJEWELLER   | PC JEWELLER LTD           | NSE_EQ\|INE785M01021 |
| 10 | EASEMYTRIP   | EASY TRIP PLANNERS LTD    | NSE_EQ\|INE07O001026 |
| 11 | RTNPOWER     | RATTANINDIA POWER LIMITED | NSE_EQ\|INE399K01017 |
| 12 | PNB          | PUNJAB NATIONAL BANK      | NSE_EQ\|INE160A01022 |
| 13 | ADANIPOWER   | ADANI POWER LTD           | NSE_EQ\|INE814H01029 |
| 14 | NHPC         | NHPC LTD                  | NSE_EQ\|INE848E01016 |
| 15 | TATASTEEL    | TATA STEEL LIMITED        | NSE_EQ\|INE081A01020 |
| 16 | IDFCFIRSTB   | IDFC FIRST BANK LIMITED   | NSE_EQ\|INE092T01019 |
| 17 | NMDC         | NMDC LTD.                 | NSE_EQ\|INE584A01023 |
| 18 | CANBK        | CANARA BANK               | NSE_EQ\|INE476A01022 |
| 19 | HDFCBANK     | HDFC BANK LTD             | NSE_EQ\|INE040A01034 |
| 20 | HCC          | HINDUSTAN CONSTRUCTION CO | NSE_EQ\|INE549A01026 |
| 21 | SOUTHBANK    | THE SOUTH INDIAN BANK LTD | NSE_EQ\|INE683A01023 |
| 22 | NBCC         | NBCC (INDIA) LIMITED      | NSE_EQ\|INE095N01031 |
| 23 | MAHABANK     | BANK OF MAHARASHTRA       | NSE_EQ\|INE457A01014 |
| 24 | SAIL         | STEEL AUTHORITY OF INDIA  | NSE_EQ\|INE114A01011 |
| 25 | IRB          | IRB INFRA DEV LTD.        | NSE_EQ\|INE821I01022 |
| 26 | KOTAKBANK    | KOTAK MAHINDRA BANK LTD   | NSE_EQ\|INE237A01036 |
| 27 | CCAVENUE     | AVENUESAI LIMITED         | NSE_EQ\|INE483S01020 |
| 28 | IFCI         | IFCI LTD                  | NSE_EQ\|INE039A01010 |
| 29 | BEL          | BHARAT ELECTRONICS LTD    | NSE_EQ\|INE263A01024 |
| 30 | ASHOKLEY     | ASHOK LEYLAND LTD         | NSE_EQ\|INE208A01029 |
| 31 | GMRAIRPORT   | GMR AIRPORTS LIMITED      | NSE_EQ\|INE776C01039 |
| 32 | BHEL         | BHEL                      | NSE_EQ\|INE257A01026 |
| 33 | DISHTV       | DISH TV INDIA LTD.        | NSE_EQ\|INE836F01026 |
| 34 | RAMASTEEL    | RAMA STEEL TUBES LIMITED  | NSE_EQ\|INE230R01035 |
| 35 | HFCL         | HFCL LIMITED              | NSE_EQ\|INE548A01028 |
| 36 | IOB          | INDIAN OVERSEAS BANK      | NSE_EQ\|INE565A01014 |
| 37 | RVNL         | RAIL VIKAS NIGAM LIMITED  | NSE_EQ\|INE415G01027 |
| 38 | IOC          | INDIAN OIL CORP LTD       | NSE_EQ\|INE242A01010 |
| 39 | SJVN         | SJVN LTD                  | NSE_EQ\|INE002L01015 |
| 40 | CUPID        | CUPID LIMITED             | NSE_EQ\|INE509F01029 |
| 41 | UNIONBANK    | UNION BANK OF INDIA       | NSE_EQ\|INE692A01016 |
| 42 | NATIONALUM   | NATIONAL ALUMINIUM CO LTD | NSE_EQ\|INE139A01034 |
| 43 | GAIL         | GAIL (INDIA) LTD          | NSE_EQ\|INE129A01019 |
| 44 | UCOBANK      | UCO BANK                  | NSE_EQ\|INE691A01018 |
| 45 | ALOKINDS     | ALOK INDUSTRIES LIMITED   | NSE_EQ\|INE270A01029 |
| 46 | BAJAJHIND    | BAJAJ HINDUSTHAN SUGAR LT | NSE_EQ\|INE306A01021 |
| 47 | ITC          | ITC LTD                   | NSE_EQ\|INE154A01025 |

---

## Selection Criteria
- Top 50 by average trading volume (currently 47 after the 2026-07-14 non-stock cleanup — see above)
- 3 years of historical data (1095 days)
- >95% data completeness
- Timeframe: 1D (daily candles)

## For Testing
Use first 5 symbols:
- IDEA (NSE_EQ|INE669E01016)
- YESBANK (NSE_EQ|INE528G01035)
- SUZLON (NSE_EQ|INE040H01021)
- GTLINFRA (NSE_EQ|INE221H01019)
- JPPOWER (NSE_EQ|INE351F01018)
