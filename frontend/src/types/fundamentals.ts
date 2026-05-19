/**
 * Company Fundamentals — TypeScript types
 * Mirror of backend/app/schemas/fundamentals.py
 * period_date fields are ISO date strings ("YYYY-MM-DD") serialised by FastAPI.
 */

export interface ProfileData {
  company_profile: string | null;
  sector: string | null;
  sector_mcap_inr: number | null;
  sector_mcap_usd: number | null;
  sector_mcap_usd_unit: string | null;
}

export interface KeyRatioItem {
  name: string;
  company_value: number | null;
  sector_value: number | null;
}

export interface PeriodValue {
  value: number;
  period: string;
  period_date: string;
  change_pct: number | null;
}

export interface CategoryHistory {
  category: string;
  history: PeriodValue[];
}

export interface BalanceSheetEntry {
  total_asset: number | null;
  total_liability: number | null;
  net_worth: number | null;
  period: string;
  period_date: string;
}

export interface ShareHoldingEntry {
  promoters_pct: number | null;
  fii_pct: number | null;
  other_dii_pct: number | null;
  mutual_funds_pct: number | null;
  retail_and_other_pct: number | null;
  period: string;
  period_date: string;
}

export interface IncomeStatementData {
  yearly:    CategoryHistory[];
  quarterly: CategoryHistory[];
}

export interface CorporateAction {
  action_type: string;
  expiry_date_str: string | null;
  expiry_date: string | null;
  amount: number | null;
  ratio: string | null;
  event_details: { name: string; value: string }[] | null;
}

export interface CompetitorEntry {
  instrument_key: string;
  trading_symbol: string | null;
  name: string | null;
  company_profile: string | null;
  sector: string | null;
  mcap_inr_crore: number | null;
  mcap_usd: number | null;
  mcap_usd_unit: string | null;
}

export interface FundamentalsFullResponse {
  available: boolean;
  instrument_key: string;
  isin: string | null;
  reason: string | null;
  profile: ProfileData | null;
  key_ratios: KeyRatioItem[] | null;
  income_statement: IncomeStatementData | null;
  balance_sheet: BalanceSheetEntry[] | null;
  cash_flow: CategoryHistory[] | null;
  share_holdings: ShareHoldingEntry[] | null;
  corporate_actions: CorporateAction[] | null;
  competitors: CompetitorEntry[] | null;
  fetched_at: string | null;
  section_fetched_at: Record<string, string | null> | null;
}
