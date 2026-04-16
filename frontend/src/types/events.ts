/**
 * Types for events
 */

export enum EventType {
  EARNINGS_ANNOUNCEMENT = "earnings_announcement",
  MERGER_ACQUISITION = "merger_acquisition",
  REGULATORY_CHANGE = "regulatory_change",
  MANAGEMENT_CHANGE = "management_change",
  PRODUCT_LAUNCH = "product_launch",
  LEGAL_ISSUE = "legal_issue",
  MARKET_RUMOR = "market_rumor",
  ANALYST_RATING = "analyst_rating",
  DIVIDEND_ANNOUNCEMENT = "dividend_announcement",
  STOCK_SPLIT = "stock_split",
  BUYBACK_ANNOUNCEMENT = "buyback_announcement",
  OTHER = "other",
}

export enum FakeNewsStatus {
  SUSPECTED = "suspected",
  CONFIRMED = "confirmed",
  CLEARED = "cleared",
}

export interface DetectionLayers {
  layer1: boolean;
  layer2: boolean;
  layer3: boolean;
  layer4: boolean;
}

export interface FakeNewsFlag {
  flag_status: FakeNewsStatus;
  detection_layers: DetectionLayers;
  reasoning?: string;
  flagged_at: string;
}

export interface ProcessedEvent {
  event_id: string;
  event_type: EventType;
  impact_score: number;
  affected_symbols: string[];
  sentiment_score: number;
  credibility_score: number;
  reasoning: string;
  processed_at: string;
  fake_news_flag?: FakeNewsFlag;
}

export interface EventDetail extends ProcessedEvent {
  source_type: string;
  source_name: string;
  title: string;
  content: string;
  language: string;
  translated_content?: string;
  published_at: string;
}

export interface ProcessedEventsResponse {
  events: ProcessedEvent[];
  total: number;
  page: number;
  limit: number;
}

export interface EventFilters {
  symbol?: string;
  event_type?: EventType;
  min_impact?: number;
  page?: number;
  limit?: number;
}
