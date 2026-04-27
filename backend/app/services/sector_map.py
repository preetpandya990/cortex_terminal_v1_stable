# PATH: backend/app/services/sector_map.py
"""
NSE sector classification.

Priority order:
  1. Exact trading_symbol match in _SYMBOL_SECTOR (covers index constituents)
  2. Keyword scan of instrument name (covers mid/small caps)
  3. None — unclassified
"""

_SYMBOL_SECTOR: dict[str, str] = {
    # ── Banking ─────────────────────────────────────────────────────────────
    "HDFCBANK": "Banking", "ICICIBANK": "Banking", "KOTAKBANK": "Banking",
    "AXISBANK": "Banking", "SBIN": "Banking", "INDUSINDBK": "Banking",
    "BANKBARODA": "Banking", "FEDERALBNK": "Banking", "BANDHANBNK": "Banking",
    "IDFCFIRSTB": "Banking", "PNB": "Banking", "AUBANK": "Banking",
    "CANBK": "Banking", "UNIONBANK": "Banking", "INDIANB": "Banking",
    "CENTRALBK": "Banking", "IOB": "Banking", "MAHABANK": "Banking",
    "UCOBANK": "Banking", "J&KBANK": "Banking", "KARURVYSYA": "Banking",
    "DCBBANK": "Banking", "RBLBANK": "Banking", "YESBANK": "Banking",
    "CSBBANK": "Banking", "SOUTHBANK": "Banking", "UJJIVANSFB": "Banking",
    "EQUITASBNK": "Banking", "SURYODAY": "Banking", "ESAFSFB": "Banking",
    "TMB": "Banking", "NAINITAL": "Banking",

    # ── Financial Services (NBFC / Insurance / AMC) ──────────────────────────
    "BAJFINANCE": "Financial Services", "BAJAJFINSV": "Financial Services",
    "HDFCLIFE": "Financial Services", "SBILIFE": "Financial Services",
    "ICICIPRULI": "Financial Services", "ICICIGI": "Financial Services",
    "SBICARD": "Financial Services", "MUTHOOTFIN": "Financial Services",
    "MANAPPURAM": "Financial Services", "CHOLAFIN": "Financial Services",
    "M&MFIN": "Financial Services", "SHRIRAMFIN": "Financial Services",
    "LICIHSGFIN": "Financial Services", "POONAWALLA": "Financial Services",
    "SUNDARMFIN": "Financial Services", "CREDITACC": "Financial Services",
    "SPANDANA": "Financial Services", "UJJIVAN": "Financial Services",
    "FIVESTAR": "Financial Services", "IIFLWAM": "Financial Services",
    "NUVOCO": "Financial Services", "STAR": "Financial Services",
    "STARHEALTH": "Financial Services", "GODIGIT": "Financial Services",
    "HDFCAMC": "Financial Services", "NIPPONLIFE": "Financial Services",
    "ABSLAMC": "Financial Services", "360ONE": "Financial Services",
    "ANGELONE": "Financial Services", "IIFL": "Financial Services",
    "MOTILALOFS": "Financial Services", "CHOICEIN": "Financial Services",

    # ── IT & Technology ──────────────────────────────────────────────────────
    "TCS": "IT & Technology", "INFY": "IT & Technology",
    "HCLTECH": "IT & Technology", "WIPRO": "IT & Technology",
    "TECHM": "IT & Technology", "MPHASIS": "IT & Technology",
    "LTTS": "IT & Technology", "PERSISTENT": "IT & Technology",
    "COFORGE": "IT & Technology", "LTIM": "IT & Technology",
    "OFSS": "IT & Technology", "HEXAWARE": "IT & Technology",
    "KPIT": "IT & Technology", "CYIENT": "IT & Technology",
    "ZENSAR": "IT & Technology", "BIRLASOFT": "IT & Technology",
    "MASTEK": "IT & Technology", "TATAELXSI": "IT & Technology",
    "TANLA": "IT & Technology", "INTELLECT": "IT & Technology",
    "NEWGEN": "IT & Technology", "NUCLEUS": "IT & Technology",
    "HAPPSTMNDS": "IT & Technology", "ROUTE": "IT & Technology",
    "NIIT": "IT & Technology", "RATEGAIN": "IT & Technology",
    "INDIAMART": "IT & Technology", "JUSTDIAL": "IT & Technology",
    "NAUKRI": "IT & Technology", "POLICYBZR": "IT & Technology",
    "ZOMATO": "IT & Technology", "SWIGGY": "IT & Technology",
    "PAYTM": "IT & Technology", "MAPMYINDIA": "IT & Technology",

    # ── Healthcare & Pharma ──────────────────────────────────────────────────
    "SUNPHARMA": "Healthcare & Pharma", "DRREDDY": "Healthcare & Pharma",
    "CIPLA": "Healthcare & Pharma", "DIVISLAB": "Healthcare & Pharma",
    "BIOCON": "Healthcare & Pharma", "TORNTPHARM": "Healthcare & Pharma",
    "LUPIN": "Healthcare & Pharma", "ABBOTINDIA": "Healthcare & Pharma",
    "ALKEM": "Healthcare & Pharma", "GRANULES": "Healthcare & Pharma",
    "GLAXO": "Healthcare & Pharma", "IPCALAB": "Healthcare & Pharma",
    "NATCOPHARM": "Healthcare & Pharma", "PFIZER": "Healthcare & Pharma",
    "SANOFI": "Healthcare & Pharma", "APOLLOHOSP": "Healthcare & Pharma",
    "FORTIS": "Healthcare & Pharma", "MAXHEALTH": "Healthcare & Pharma",
    "ERIS": "Healthcare & Pharma", "GLENMARK": "Healthcare & Pharma",
    "LAURUSLABS": "Healthcare & Pharma", "MEDANTA": "Healthcare & Pharma",
    "AJANTPHARM": "Healthcare & Pharma", "ASTRAZEN": "Healthcare & Pharma",
    "SUVEN": "Healthcare & Pharma", "SEQUENT": "Healthcare & Pharma",
    "SOLARA": "Healthcare & Pharma", "LAURUS": "Healthcare & Pharma",
    "METROPOLIS": "Healthcare & Pharma", "THYROCARE": "Healthcare & Pharma",
    "KRSNAA": "Healthcare & Pharma", "SUVENPHAR": "Healthcare & Pharma",

    # ── Automobiles ──────────────────────────────────────────────────────────
    "MARUTI": "Automobiles", "TATAMOTORS": "Automobiles",
    "M&M": "Automobiles", "BAJAJ-AUTO": "Automobiles",
    "HEROMOTOCO": "Automobiles", "EICHERMOT": "Automobiles",
    "TVSMOTOR": "Automobiles", "ASHOKLEY": "Automobiles",
    "MOTHERSON": "Automobiles", "BALKRISIND": "Automobiles",
    "EXIDEIND": "Automobiles", "MRF": "Automobiles",
    "APOLLOTYRE": "Automobiles", "CEATLTD": "Automobiles",
    "BOSCHLTD": "Automobiles", "ENDURANCE": "Automobiles",
    "SUNDRMFAST": "Automobiles", "CRAFTSMAN": "Automobiles",
    "SUPRAJIT": "Automobiles", "OLECTRA": "Automobiles",
    "MAHINDCIE": "Automobiles", "TIINDIA": "Automobiles",

    # ── FMCG ─────────────────────────────────────────────────────────────────
    "HINDUNILVR": "FMCG", "ITC": "FMCG", "NESTLEIND": "FMCG",
    "BRITANNIA": "FMCG", "DABUR": "FMCG", "MARICO": "FMCG",
    "GODREJCP": "FMCG", "COLPAL": "FMCG", "TATACONSUM": "FMCG",
    "EMAMILTD": "FMCG", "PGHH": "FMCG", "VBL": "FMCG",
    "VARUNBEV": "FMCG", "RADICO": "FMCG", "UBL": "FMCG",
    "MCDOWELL-N": "FMCG", "JUBLFOODS": "FMCG", "BIKAJI": "FMCG",
    "PATANJALI": "FMCG", "HATSUN": "FMCG",

    # ── Metals & Mining ──────────────────────────────────────────────────────
    "TATASTEEL": "Metals & Mining", "JSWSTEEL": "Metals & Mining",
    "HINDALCO": "Metals & Mining", "VEDL": "Metals & Mining",
    "NATIONALUM": "Metals & Mining", "SAIL": "Metals & Mining",
    "COALINDIA": "Metals & Mining", "NMDC": "Metals & Mining",
    "HINDCOPPER": "Metals & Mining", "WELCORP": "Metals & Mining",
    "APLAPOLLO": "Metals & Mining", "RATNAMANI": "Metals & Mining",
    "MOIL": "Metals & Mining", "JINDALSTEL": "Metals & Mining",
    "JSPL": "Metals & Mining", "TINPLATE": "Metals & Mining",

    # ── Oil & Gas ────────────────────────────────────────────────────────────
    "RELIANCE": "Oil & Gas", "ONGC": "Oil & Gas", "IOC": "Oil & Gas",
    "BPCL": "Oil & Gas", "GAIL": "Oil & Gas", "PETRONET": "Oil & Gas",
    "MGL": "Oil & Gas", "IGL": "Oil & Gas", "GSPL": "Oil & Gas",
    "GUJGASLTD": "Oil & Gas", "HPCL": "Oil & Gas", "OIL": "Oil & Gas",
    "CASTROLIND": "Oil & Gas",

    # ── Energy & Power ───────────────────────────────────────────────────────
    "NTPC": "Energy & Power", "POWERGRID": "Energy & Power",
    "TATAPOWER": "Energy & Power", "ADANIPOWER": "Energy & Power",
    "ADANIGREEN": "Energy & Power", "TORNTPOWER": "Energy & Power",
    "CESC": "Energy & Power", "NHPC": "Energy & Power",
    "SJVN": "Energy & Power", "RPOWER": "Energy & Power",
    "JSWENERGY": "Energy & Power", "INOXGREEN": "Energy & Power",
    "GREENKO": "Energy & Power", "SUZLON": "Energy & Power",

    # ── Cement & Infrastructure ──────────────────────────────────────────────
    "ULTRACEMCO": "Cement & Infra", "SHREECEM": "Cement & Infra",
    "AMBUJACEM": "Cement & Infra", "ACC": "Cement & Infra",
    "DALMIACMT": "Cement & Infra", "RAMCOCEM": "Cement & Infra",
    "HEIDELBERG": "Cement & Infra", "BIRLACORPN": "Cement & Infra",
    "LT": "Cement & Infra", "KEC": "Cement & Infra",
    "KALPATPOWR": "Cement & Infra", "IRCON": "Cement & Infra",
    "RITES": "Cement & Infra", "NBCC": "Cement & Infra",
    "NCC": "Cement & Infra", "PNCINFRA": "Cement & Infra",
    "HGINFRA": "Cement & Infra", "HCC": "Cement & Infra",
    "JKCEMENT": "Cement & Infra", "STARCEMENT": "Cement & Infra",

    # ── Realty ───────────────────────────────────────────────────────────────
    "DLF": "Realty", "GODREJPROP": "Realty", "PRESTIGE": "Realty",
    "OBEROIRLTY": "Realty", "PHOENIXLTD": "Realty", "BRIGADE": "Realty",
    "MACROTECH": "Realty", "LODHA": "Realty", "MAHLIFE": "Realty",
    "SOBHA": "Realty", "KOLTEPATIL": "Realty", "SUNTECK": "Realty",
    "NAVKARURB": "Realty", "KEYSTONE": "Realty",

    # ── Telecom ──────────────────────────────────────────────────────────────
    "BHARTIARTL": "Telecom", "IDEA": "Telecom", "TATACOMM": "Telecom",
    "RAILTEL": "Telecom", "HFCL": "Telecom", "STLTECH": "Telecom",
    "TEJASNET": "Telecom",

    # ── Chemicals ────────────────────────────────────────────────────────────
    "PIDILITIND": "Chemicals", "ASIANPAINT": "Chemicals",
    "BERGEPAINT": "Chemicals", "KANSAINER": "Chemicals",
    "AKZONOBEL": "Chemicals", "AARTI": "Chemicals",
    "AARTIIND": "Chemicals", "DEEPAKNTR": "Chemicals",
    "NAVINFLUOR": "Chemicals", "FINEORG": "Chemicals",
    "TATACHEM": "Chemicals", "GUJALKALI": "Chemicals",
    "VINATI": "Chemicals", "NOCIL": "Chemicals",
    "SUDARSCHEM": "Chemicals", "BASF": "Chemicals",
    "PCBL": "Chemicals", "BALCHEM": "Chemicals",

    # ── Textiles ─────────────────────────────────────────────────────────────
    "PAGEIND": "Textiles", "TRENT": "Textiles", "ABFRL": "Textiles",
    "RAYMOND": "Textiles", "VARDHACRLC": "Textiles",
    "WELSPUNLIV": "Textiles", "TRIDENT": "Textiles", "NDL": "Textiles",
    "GOKUTEX": "Textiles", "SPORTKING": "Textiles",

    # ── Hospitality ──────────────────────────────────────────────────────────
    "LEMONTREE": "Hospitality", "EIHOTEL": "Hospitality",
    "CHALET": "Hospitality", "TAJGVK": "Hospitality",
    "INDHOTEL": "Hospitality", "MAHHOLIDAY": "Hospitality",
    "ROYALORCH": "Hospitality",

    # ── Media & Entertainment ────────────────────────────────────────────────
    "ZEEL": "Media & Entertainment", "SUNTV": "Media & Entertainment",
    "NETWORK18": "Media & Entertainment", "TV18BRDCST": "Media & Entertainment",
    "RADIOCITY": "Media & Entertainment", "SAREGAMA": "Media & Entertainment",
    "PVRINOX": "Media & Entertainment", "INOXLEISUR": "Media & Entertainment",
    "BALAJITELE": "Media & Entertainment", "JAGRAN": "Media & Entertainment",
    "DBCORP": "Media & Entertainment",

    # ── Agriculture ──────────────────────────────────────────────────────────
    "UPL": "Agriculture", "PIIND": "Agriculture", "BAYER": "Agriculture",
    "RALLIS": "Agriculture", "COROMANDEL": "Agriculture",
    "GNFC": "Agriculture", "CHAMBAL": "Agriculture", "GSFC": "Agriculture",
    "GOKULAGRO": "Agriculture",

    # ── Logistics & Transport ────────────────────────────────────────────────
    "CONCOR": "Logistics", "BLUEDART": "Logistics", "GATI": "Logistics",
    "DELHIVERY": "Logistics", "MAHLOG": "Logistics",
    "TCI": "Logistics", "VTLLTD": "Logistics",
}

_KEYWORD_SECTORS: list[tuple[list[str], str]] = [
    (["BANK", "BANKING"], "Banking"),
    (["FINANCE", "FINANCIAL", "FINSERV", "FINCORP", "LENDING",
      "MICROFINANCE", "HOUSING FINANCE", "NBFC", "INSURANCE", "INSUR"], "Financial Services"),
    (["PHARMA", "PHARMACEUTICAL", "DRUG", "BIOTECH", "BIOLOG",
      "HOSPITAL", "HEALTHCARE", "HEALTH CARE", "MEDICAL",
      "DIAGNOSTIC", "PATHOLOG", "LIFE SCIENCE"], "Healthcare & Pharma"),
    (["INFOTECH", "INFORMATION TECH", "IT SOLUTION", "SOFTWARE",
      "TECHNOLOGY", "DIGITAL", "CYBER", "DATA TECH"], "IT & Technology"),
    (["AUTO", "AUTOMOBILE", "MOTOR", "VEHICLE", "TYRE", "TIRE"], "Automobiles"),
    (["FMCG", "CONSUMER GOODS", "BEVERAGES", "BEVERAGE",
      "DAIRY", "BAKERY", "EDIBLE OIL"], "FMCG"),
    (["STEEL", "METAL", "ALUMINIUM", "ALUMINUM", "COPPER",
      "MINING", "ZINC", "NICKEL", "COAL", "MINERAL"], "Metals & Mining"),
    (["PETROLEUM", "PETRO", "REFINER", "REFINERY", "LPG", "CNG"], "Oil & Gas"),
    (["POWER", "ELECTRIC", "SOLAR", "WIND ENERGY", "RENEWABLE ENERGY"], "Energy & Power"),
    (["CEMENT", "READY MIX", "CONCRETE"], "Cement & Infra"),
    (["INFRASTRUCTURE", "CONSTRUCTION", "ROADS", "HIGHWAY",
      "RAILWAY", "PORT", "AIRPORT", "EPC"], "Cement & Infra"),
    (["REAL ESTATE", "REALTY", "PROPERTY", "DEVELOPER", "BUILDERS"], "Realty"),
    (["TELECOM", "TELECOMMUNICATION", "BROADBAND", "FIBER OPTIC"], "Telecom"),
    (["CHEMICAL", "CHEM", "PAINT", "POLYMER", "PIGMENT"], "Chemicals"),
    (["TEXTILE", "COTTON", "FABRIC", "GARMENT", "APPAREL",
      "DENIM", "YARN", "LINEN", "WEAV"], "Textiles"),
    (["HOTEL", "RESORT", "HOSPITALITY", "TOURISM", "TRAVEL"], "Hospitality"),
    (["MEDIA", "BROADCAST", "ENTERTAINMENT", "FILM",
      "TELEVISION", "CINEMA", "PUBLISHING"], "Media & Entertainment"),
    (["AGRO", "AGRICULTURE", "SEEDS", "FERTILIZER",
      "PESTICIDE", "CROP", "AGROCHEMICAL"], "Agriculture"),
    (["LOGISTIC", "FREIGHT", "SHIPPING", "COURIER",
      "TRANSPORT", "WAREHOUSE", "SUPPLY CHAIN"], "Logistics"),
    (["RETAIL", "E-COMMERCE", "ECOMMERCE", "SHOPPING", "HYPERMARKET"], "Retail"),
    (["EDUCATION", "LEARNING", "SCHOOL", "COLLEGE", "EDTECH"], "Education"),
]


def get_sector(trading_symbol: str | None, name: str | None) -> str | None:
    if trading_symbol:
        sector = _SYMBOL_SECTOR.get(trading_symbol.upper())
        if sector:
            return sector

    if name:
        name_upper = name.upper()
        for keywords, sector in _KEYWORD_SECTORS:
            if any(kw in name_upper for kw in keywords):
                return sector

    return None
