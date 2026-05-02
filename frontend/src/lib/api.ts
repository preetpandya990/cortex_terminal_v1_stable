import axios, { AxiosError, AxiosResponse } from 'axios';
import type {
  CachedPrediction,
  MLAlert,
  MLModel,
  MLPrediction,
  MLPredictionListResponse,
  MLRollingPredictionResponse,
  PredictResponse,
} from '@/types/ml';
import type { UpstoxCandlesResponse } from '@/types/upstox';
import type { SuggestionsListResponse, SuggestionFilters } from '@/types/trade_suggestions';
import type {
  CreatePortfolioRequest,
  UpdatePortfolioSettingsRequest,
  PlaceOrderRequest,
  ClosePositionRequest,
  Portfolio,
  PortfolioSummary,
  PaperOrder,
  PaperPositionDetail,
  OrdersListResponse,
  PositionsListResponse,
  OutcomesListResponse,
  OutcomeStatsResponse,
  PnlSnapshotsListResponse,
  QtySuggestionResponse,
  OrdersQueryParams,
  PositionsQueryParams,
  OutcomesQueryParams,
  PnlSnapshotsQueryParams,
} from '@/types/paper_trading';

// Watchlist types
export interface WatchlistItem {
  id: number;
  instrument_key: string;
  trading_symbol: string;
  name: string | null;
  exchange: string | null;
  position: number;
  created_at: string;
  updated_at: string;
}

export interface WatchlistItemCreate {
  instrument_key: string;
  trading_symbol: string;
  name?: string | null;
  exchange?: string | null;
}

export interface WatchlistReorderRequest {
  item_id: number;
  new_position: number;
}

// Normalise the API base URL so it always ends with /api/v1.
// NEXT_PUBLIC_API_URL may be set as either the bare origin ("http://localhost:8000")
// or the full prefixed URL ("http://localhost:8000/api/v1") — both work correctly.
const _apiOrigin = (process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000').replace(/\/$/, '');
export const API_BASE_URL = _apiOrigin.endsWith('/api/v1') ? _apiOrigin : `${_apiOrigin}/api/v1`;
export const WS_BASE_URL = API_BASE_URL.replace(/^http/i, 'ws');

export const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
});

// Function to set auth token (called from AuthContext)
export function setAuthToken(token: string | null) {
  if (token) {
    api.defaults.headers.common['Authorization'] = `Bearer ${token}`;
  } else {
    delete api.defaults.headers.common['Authorization'];
  }
}

// Attach a per-request ID and timing metadata for tracing.
function generateRequestId(): string {
  return `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

api.interceptors.request.use(
  (config) => {
    const requestId = generateRequestId();
    config.headers['X-Request-ID'] = requestId;
    (config as any).metadata = { startTime: Date.now(), requestId };
    return config;
  },
  (error) => Promise.reject(error),
);

api.interceptors.response.use(
  // Success — pass through without logging (use network tab for that).
  (response) => response,

  // Error — suppress expected conditions; log only genuinely unexpected ones.
  (error) => {
    const status = error.response?.status as number | undefined;

    const isSuppressed =
      // Request aborted: component unmounted or page navigated away.
      error.code === 'ERR_CANCELED' ||
      error.message === 'canceled' ||
      // Backend unreachable: React Query handles retry; callers render error state.
      error.code === 'ERR_NETWORK' ||
      error.message === 'Network Error' ||
      // Auth errors: handled by the token-refresh flow in AuthContext.
      status === 401 ||
      // Not Found: valid domain response (e.g. no portfolio yet) — callers handle via error state.
      status === 404;

    if (!isSuppressed) {
      const payload = error.response?.data as { detail?: string; message?: string } | undefined;
      const detail = payload?.detail ?? payload?.message ?? error.message ?? 'unknown error';
      // Use a flat string — object args are dropped as {} in the Next.js error overlay
      // when any property is undefined (JSON.stringify omits undefined values).
      console.error(
        `[API Error] ${error.config?.method?.toUpperCase() ?? '?'} ${error.config?.url ?? '?'} → HTTP ${status ?? '?'} | ${detail}`,
      );
    }

    return Promise.reject(error);
  },
);

interface APIErrorPayload {
  detail?: string;
  message?: string;
}

export class APIRequestError extends Error {
  statusCode?: number;
  errorCode?: string;

  constructor(message: string, options?: { statusCode?: number; errorCode?: string }) {
    super(message);
    this.name = 'APIRequestError';
    this.statusCode = options?.statusCode;
    this.errorCode = options?.errorCode;
  }
}

/**
 * Returns true when `error` is an HTTP 429 Too Many Requests response.
 * Covers both AxiosError instances and plain error objects with a response shape.
 */
export function isRateLimitError(error: unknown): boolean {
  if (error instanceof AxiosError) return error.response?.status === 429;
  return (error as { response?: { status?: number } } | null)?.response?.status === 429;
}

/**
 * Extracts the Retry-After delay from a 429 error response header.
 * Falls back to `defaultMs` when the header is absent or non-numeric.
 */
export function getRetryAfterMs(error: unknown, defaultMs = 2_000): number {
  const header =
    error instanceof AxiosError
      ? error.response?.headers?.['retry-after']
      : (error as { response?: { headers?: Record<string, string> } } | null)
          ?.response?.headers?.['retry-after'];
  if (!header) return defaultMs;
  const seconds = parseInt(String(header), 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds * 1_000 : defaultMs;
}

/**
 * Returns true when `error` represents a transient network failure (backend
 * unreachable, request aborted, or connection reset).
 *
 * Use this in catch blocks to avoid polluting the console with expected errors
 * during development or when the backend is temporarily down.
 * React Query surfaces these in component error state; no logging needed.
 */
export function isNetworkError(error: unknown): boolean {
  // APIRequestError thrown by requestData()
  if (error instanceof APIRequestError) {
    return (
      error.errorCode === 'ERR_NETWORK' ||
      error.errorCode === 'ERR_CANCELED' ||
      error.message === 'Network Error' ||
      error.message === 'canceled'
    );
  }
  // Raw AxiosError from direct api.get() / api.post() calls
  const e = error as { code?: string; message?: string } | null;
  return (
    e?.code === 'ERR_NETWORK' ||
    e?.code === 'ERR_CANCELED' ||
    e?.message === 'Network Error' ||
    e?.message === 'canceled'
  );
}

function toAPIError(error: unknown, fallbackMessage: string): Error {
  if (error instanceof AxiosError) {
    const payload = error.response?.data as APIErrorPayload | undefined;
    const detail = payload?.detail ?? payload?.message;
    return new APIRequestError(detail || error.message || fallbackMessage, {
      statusCode: error.response?.status,
      errorCode: error.code,
    });
  }

  if (error instanceof Error) {
    return error;
  }

  return new APIRequestError(fallbackMessage);
}

async function requestData<T>(request: Promise<AxiosResponse<T>>, fallbackMessage: string): Promise<T> {
  try {
    const response = await request;
    return response.data;
  } catch (error) {
    throw toAPIError(error, fallbackMessage);
  }
}

// Market Data API
export const marketDataAPI = {
  getStockData: async (symbol: string, params?: {
    timeframe?: string;
    start_date?: string;
    end_date?: string;
    limit?: number;
  }) => {
    const response = await api.get(`/market/stocks/${symbol}/data`, { params });
    return response.data;
  },

  getStockIndicators: async (symbol: string, params?: {
    timeframe?: string;
    limit?: number;
  }) => {
    const response = await api.get(`/market/stocks/${symbol}/indicators`, { params });
    return response.data;
  },

  refreshStockData: async (symbol: string) => {
    const response = await api.post(`/market/stocks/${symbol}/refresh`);
    return response.data;
  },

  getLivePrice: async (instrumentKey: string, signal?: AbortSignal) => {
    const response = await api.get(`/market-data/live/${instrumentKey}`, { signal });
    return response.data;
  },

  getOhlcv: async (instrumentKey: string, params: {
    timeframe?: string;
    limit?: number;
    signal?: AbortSignal;
  }) => {
    const { signal, ...queryParams } = params;
    const response = await api.get('/upstox/candles/intraday', {
      params: { instrument_key: instrumentKey, unit: 'minute', interval: 1, ...queryParams },
      signal,
    });
    return response.data?.data ?? response.data;
  },
};

// Upstox Market Quotes API (proxied via backend)
export const upstoxAPI = {
  searchInstruments: async (query: string, params?: { limit?: number; segment?: string }) => {
    const response = await api.get('/market-data/instruments/search', {
      params: { q: query, ...params },
    });
    return response.data;
  },

  getOhlcQuote: async (instrumentKey: string, interval?: string) => {
    const params: Record<string, string> = { instrument_key: instrumentKey };
    if (interval) {
      params.interval = interval;
    }
    const response = await api.get('/upstox/ohlc', { params });
    return response.data;
  },

  getLtpQuote: async (instrumentKey: string) => {
    const response = await api.get(`/market-data/live/${instrumentKey}`);
    return response.data;
  },

  getHistoricalCandles: async (
    instrumentKey: string,
    params: { unit: string; interval: number; to_date: string; from_date?: string }
  ): Promise<UpstoxCandlesResponse> => {
    const response = await api.get('/upstox/candles/historical', {
      params: { instrument_key: instrumentKey, ...params },
    });
    return response.data;
  },

  getIntradayCandles: async (
    instrumentKey: string,
    params: { unit: string; interval: number }
  ): Promise<UpstoxCandlesResponse> => {
    const response = await api.get('/upstox/candles/intraday', {
      params: { instrument_key: instrumentKey, ...params },
    });
    return response.data;
  },
};

// ML Predictions API
export const mlAPI = {
  getModels: async (activeOnly = true): Promise<MLModel[]> => {
    return requestData(
      api.get<MLModel[]>('/ml/models', { params: { active_only: activeOnly } }),
      'Failed to fetch ML models'
    );
  },

  getModel: async (modelId: number): Promise<MLModel> => {
    return requestData(
      api.get<MLModel>(`/ml/models/${modelId}`),
      'Failed to fetch ML model'
    );
  },

  getPredictions: async (
    symbol: string,
    params?: { horizon?: string; limit?: number }
  ): Promise<MLPredictionListResponse> => {
    return requestData(
      api.get<MLPredictionListResponse>(`/ml/predictions/${symbol}`, { params }),
      'Failed to fetch predictions'
    );
  },

  getLatestPrediction: async (
    symbol: string,
    horizon?: string
  ): Promise<MLPrediction | null> => {
    return requestData(
      api.get<MLPrediction | null>(`/ml/predictions/${symbol}/latest`, { params: { horizon } }),
      'Failed to fetch latest prediction'
    );
  },

  runPredictions: async (symbols: string[], horizons?: string[]): Promise<PredictResponse> => {
    return requestData(
      api.post<PredictResponse>('/ml/predict', { symbols, horizons }),
      'Failed to run predictions'
    );
  },

  getRealtimePrediction: async (symbol: string, horizon: string): Promise<CachedPrediction> => {
    return requestData<CachedPrediction>(
      api.get<CachedPrediction>(`/ml/predictions/${symbol}/cached`, { params: { horizon } }),
      'Failed to fetch realtime prediction'
    );
  },

  getRollingPredictions: async (
    symbol: string,
    params: { horizon: string; start?: string; end?: string; limit?: number; desc?: boolean }
  ): Promise<MLRollingPredictionResponse> => {
    return requestData(
      api.get<MLRollingPredictionResponse>(`/ml/rolling/${symbol}`, { params }),
      'Failed to fetch rolling predictions'
    );
  },

  getAlerts: async (limit: number = 20): Promise<MLAlert[]> => {
    return requestData(
      api.get<MLAlert[]>('/ml/alerts', { params: { limit } }),
      'Failed to fetch ML alerts'
    );
  },

  triggerTraining: async () => {
    return requestData(api.post('/ml/train'), 'Failed to trigger training');
  },
};

// AI Fusion API (Trading Signals)
export const fusionAPI = {
  getSignals: async (params?: {
    symbol?: string;
    signal_type?: string;
    min_confidence?: number;
    time_horizon?: string;
    page?: number;
    limit?: number;
  }) => {
    return requestData(
      api.get('/fusion/signals', { params }),
      'Failed to fetch trading signals'
    );
  },

  getSignal: async (signalId: string) => {
    return requestData(
      api.get(`/fusion/signals/${signalId}`),
      'Failed to fetch signal'
    );
  },

  getSignalAudit: async (signalId: string) => {
    return requestData(
      api.get(`/fusion/signals/${signalId}/audit`),
      'Failed to fetch signal audit'
    );
  },

  generateSignal: async (symbol: string, params?: { force?: boolean }) => {
    return requestData(
      api.post(`/fusion/signals/generate`, { symbol, ...params }),
      'Failed to generate signal'
    );
  },
};

// AI Intelligence API (Events & Classification)
export const intelligenceAPI = {
  getEvents: async (params?: {
    symbol?: string;
    event_type?: string;
    min_impact?: number;
    page?: number;
    limit?: number;
  }) => {
    return requestData(
      api.get('/intelligence/events', { params }),
      'Failed to fetch events'
    );
  },

  getRawEvents: async (params?: {
    source_type?: string;
    page?: number;
    limit?: number;
  }) => {
    return requestData(
      api.get('/intelligence/events/raw', { params }),
      'Failed to fetch raw events'
    );
  },

  classifyEvent: async (eventId: number) => {
    return requestData(
      api.post(`/intelligence/events/${eventId}/classify`),
      'Failed to classify event'
    );
  },

  checkFakeNews: async (eventId: number) => {
    return requestData(
      api.post(`/intelligence/events/${eventId}/fake-news-check`),
      'Failed to check fake news'
    );
  },
};

// AI Strategy API (Regime Detection)
export const strategyAPI = {
  getRegimes: async (params?: {
    symbol?: string;
    regime_type?: string;
    page?: number;
    limit?: number;
  }) => {
    return requestData(
      api.get('/strategy/regimes', { params }),
      'Failed to fetch regimes'
    );
  },

  getLatestRegime: async (symbol?: string) => {
    return requestData(
      api.get('/strategy/regimes/latest', { params: { symbol } }),
      'Failed to fetch latest regime'
    );
  },

  detectRegime: async (symbol: string) => {
    return requestData(
      api.post('/strategy/regimes/detect', { symbol }),
      'Failed to detect regime'
    );
  },
};

// AI Governance API (Model Registry)
export const governanceAPI = {
  getModels: async (params?: {
    deployment_state?: string;
    page?: number;
    limit?: number;
  }) => {
    return requestData(
      api.get('/governance/models', { params }),
      'Failed to fetch models'
    );
  },

  getModel: async (modelId: number) => {
    return requestData(
      api.get(`/governance/models/${modelId}`),
      'Failed to fetch model'
    );
  },

  promoteModel: async (modelId: number, targetState: string) => {
    return requestData(
      api.post(`/governance/models/${modelId}/promote`, { target_state: targetState }),
      'Failed to promote model'
    );
  },

  evaluateModel: async (modelId: number) => {
    return requestData(
      api.post(`/governance/models/${modelId}/evaluate`),
      'Failed to evaluate model'
    );
  },
};

// AI Safety API (Kill Switch)
export const safetyAPI = {
  getKillSwitchStatus: async () => {
    return requestData(
      api.get('/safety/kill-switch/status'),
      'Failed to fetch kill switch status'
    );
  },

  activateKillSwitch: async (scope: string, symbol?: string, reason?: string) => {
    return requestData(
      api.post('/safety/kill-switch/activate', { scope, symbol, reason }),
      'Failed to activate kill switch'
    );
  },

  deactivateKillSwitch: async (scope: string, symbol?: string) => {
    return requestData(
      api.post('/safety/kill-switch/deactivate', { scope, symbol }),
      'Failed to deactivate kill switch'
    );
  },

  getSafetyTriggers: async (params?: { limit?: number }) => {
    return requestData(
      api.get('/safety/triggers', { params }),
      'Failed to fetch safety triggers'
    );
  },
};

// AI Ingestion API (RSS Sources)

// Trade Suggestions API
export const tradeSuggestionsAPI = {
  getSuggestions: async (filters?: SuggestionFilters): Promise<SuggestionsListResponse> => {
    return requestData(
      api.get('/trade-suggestions', { params: filters }),
      'Failed to fetch trade suggestions'
    );
  },
};
export const ingestionAPI = {
  getSources: async () => {
    return requestData(
      api.get('/ingestion/sources'),
      'Failed to fetch RSS sources'
    );
  },

  addSource: async (name: string, url: string, sourceType: string = 'rss') => {
    return requestData(
      api.post('/ingestion/sources', { name, url, source_type: sourceType }),
      'Failed to add RSS source'
    );
  },

  deleteSource: async (sourceId: number) => {
    return requestData(
      api.delete(`/ingestion/sources/${sourceId}`),
      'Failed to delete RSS source'
    );
  },
};

// CAI Dashboard API (Stats & Metrics)
export const caiAPI = {
  getStats: async () => {
    return requestData(
      api.get('/cai/stats'),
      'Failed to fetch CAI stats'
    );
  },

  getSystemHealth: async () => {
    return requestData(
      api.get('/cai/health'),
      'Failed to fetch system health'
    );
  },

  getAIAnalysis: async (symbol: string) => {
    const response = await api.get(`/analysis/ai/${symbol}`);
    return response.data;
  },

  getMLAnalysis: async (symbol: string) => {
    const response = await api.get(`/analysis/ml/${symbol}`);
    return response.data;
  },

  getVerdictAnalysis: async (symbol: string) => {
    const response = await api.get(`/analysis/verdict/${symbol}`);
    return response.data;
  },
};

// Watchlist API
// Paper Trading API
export const paperTradingAPI = {
  getMyPortfolio: async (): Promise<PortfolioSummary> => {
    return requestData(
      api.get<PortfolioSummary>('/paper-trading/portfolios/me'),
      'Failed to fetch portfolio'
    );
  },

  createPortfolio: async (payload: CreatePortfolioRequest): Promise<Portfolio> => {
    return requestData(
      api.post<Portfolio>('/paper-trading/portfolios', payload),
      'Failed to create portfolio'
    );
  },

  updatePortfolioSettings: async (payload: UpdatePortfolioSettingsRequest): Promise<Portfolio> => {
    return requestData(
      api.patch<Portfolio>('/paper-trading/portfolios/me', payload),
      'Failed to update portfolio settings'
    );
  },

  placeOrder: async (payload: PlaceOrderRequest): Promise<PaperOrder> => {
    return requestData(
      api.post<PaperOrder>('/paper-trading/orders', payload),
      'Failed to place order'
    );
  },

  getOrders: async (params?: OrdersQueryParams): Promise<OrdersListResponse> => {
    return requestData(
      api.get<OrdersListResponse>('/paper-trading/orders', { params }),
      'Failed to fetch orders'
    );
  },

  cancelOrder: async (orderId: string): Promise<void> => {
    return requestData(
      api.delete(`/paper-trading/orders/${orderId}`),
      'Failed to cancel order'
    );
  },

  getPositions: async (params?: PositionsQueryParams): Promise<PositionsListResponse> => {
    return requestData(
      api.get<PositionsListResponse>('/paper-trading/positions', { params }),
      'Failed to fetch positions'
    );
  },

  getPositionDetail: async (positionId: string): Promise<PaperPositionDetail> => {
    return requestData(
      api.get<PaperPositionDetail>(`/paper-trading/positions/${positionId}`),
      'Failed to fetch position detail'
    );
  },

  closePosition: async (positionId: string, payload: ClosePositionRequest): Promise<PaperOrder> => {
    return requestData(
      api.post<PaperOrder>(`/paper-trading/positions/${positionId}/close`, payload),
      'Failed to close position'
    );
  },

  getQtySuggestion: async (suggestionId: string): Promise<QtySuggestionResponse> => {
    return requestData(
      api.get<QtySuggestionResponse>('/paper-trading/qty-suggestion', {
        params: { suggestion_id: suggestionId },
      }),
      'Failed to fetch quantity suggestion'
    );
  },

  getOutcomes: async (params?: OutcomesQueryParams): Promise<OutcomesListResponse> => {
    return requestData(
      api.get<OutcomesListResponse>('/paper-trading/outcomes', { params }),
      'Failed to fetch outcomes'
    );
  },

  getOutcomeStats: async (): Promise<OutcomeStatsResponse> => {
    return requestData(
      api.get<OutcomeStatsResponse>('/paper-trading/outcomes/stats'),
      'Failed to fetch outcome stats'
    );
  },

  getPnlSnapshots: async (params?: PnlSnapshotsQueryParams): Promise<PnlSnapshotsListResponse> => {
    return requestData(
      api.get<PnlSnapshotsListResponse>('/paper-trading/pnl/snapshots', { params }),
      'Failed to fetch P&L snapshots'
    );
  },
};

export const watchlistAPI = {
  getWatchlist: async (): Promise<WatchlistItem[]> => {
    return requestData(
      api.get<WatchlistItem[]>('/watchlist'),
      'Failed to fetch watchlist'
    );
  },

  addToWatchlist: async (item: WatchlistItemCreate): Promise<WatchlistItem> => {
    return requestData(
      api.post<WatchlistItem>('/watchlist', item),
      'Failed to add to watchlist'
    );
  },

  removeFromWatchlist: async (itemId: number): Promise<void> => {
    return requestData(
      api.delete(`/watchlist/${itemId}`),
      'Failed to remove from watchlist'
    );
  },

  reorderWatchlist: async (reorder: WatchlistReorderRequest): Promise<void> => {
    return requestData(
      api.put('/watchlist/reorder', reorder),
      'Failed to reorder watchlist'
    );
  },

  checkInWatchlist: async (instrumentKey: string): Promise<{ in_watchlist: boolean; item_id: number | null }> => {
    return requestData(
      api.get(`/watchlist/check/${encodeURIComponent(instrumentKey)}`),
      'Failed to check watchlist status'
    );
  },
};
