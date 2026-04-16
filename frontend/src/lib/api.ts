import axios, { AxiosError, AxiosResponse } from 'axios';
import { RunScanResponse, ScanResults, ScannerContext, ScanType } from '@/types/market';
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

export const API_BASE_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000/api/v1';
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

// Generate unique request ID for tracking
function generateRequestId(): string {
  return `req_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

// Request interceptor for logging
api.interceptors.request.use(
  (config) => {
    const requestId = generateRequestId();
    config.headers['X-Request-ID'] = requestId;

    // Log outgoing request (excluding sensitive data)
    const logData = {
      requestId,
      method: config.method?.toUpperCase(),
      url: config.url,
      baseURL: config.baseURL,
      fullURL: `${config.baseURL}${config.url}`,
      params: config.params,
      timestamp: new Date().toISOString(),
    };

    console.log('[API Request]', logData);

    // Store request start time for response time calculation
    (config as any).metadata = { startTime: Date.now(), requestId };

    return config;
  },
  (error) => {
    console.error('[API Request Error]', {
      message: error.message,
      timestamp: new Date().toISOString(),
    });
    return Promise.reject(error);
  }
);

// Response interceptor for logging
api.interceptors.response.use(
  (response) => {
    const metadata = (response.config as any).metadata;
    const responseTime = metadata ? Date.now() - metadata.startTime : 0;

    // Log successful response
    const logData = {
      requestId: metadata?.requestId,
      method: response.config.method?.toUpperCase(),
      url: response.config.url,
      status: response.status,
      statusText: response.statusText,
      responseTime: `${responseTime}ms`,
      timestamp: new Date().toISOString(),
    };

    console.log('[API Response]', logData);

    return response;
  },
  (error) => {
    const metadata = (error.config as any)?.metadata;
    const responseTime = metadata ? Date.now() - metadata.startTime : 0;

    // Determine error type
    let errorType = 'Unknown Error';
    let errorDetails: any = {
      requestId: metadata?.requestId,
      method: error.config?.method?.toUpperCase(),
      url: error.config?.url,
      responseTime: `${responseTime}ms`,
      timestamp: new Date().toISOString(),
    };

    if (error.code === 'ERR_NETWORK' || error.message === 'Network Error') {
      errorType = 'Network Error';
      errorDetails.message = 'Cannot connect to server. Please check if the backend is running.';
    } else if (error.response) {
      // Server responded with error status
      errorType = 'HTTP Error';
      errorDetails.status = error.response.status;
      errorDetails.statusText = error.response.statusText;
      errorDetails.data = error.response.data;
    } else if (error.request) {
      // Request was made but no response received (could be CORS)
      errorType = 'No Response';
      errorDetails.message = 'Request sent but no response received. This could be a CORS issue.';
    }

    // Only log if there's meaningful error info
    if (errorDetails.status || errorDetails.message) {
      // Filter out undefined values for cleaner logs
      const cleanDetails = Object.fromEntries(
        Object.entries(errorDetails).filter(([_, v]) => v !== undefined)
      );
      if (Object.keys(cleanDetails).length > 0) {
        console.error(`[API ${errorType}]`, cleanDetails);
      }
    }

    return Promise.reject(error);
  }
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

// Scanner API
export const scannerAPI = {
  getLatestScan: async (): Promise<ScanResults | null> => {
    return requestData(
      api.get<ScanResults | null>('/scanner/latest'),
      'Failed to fetch latest scanner results'
    );
  },

  getContext: async (): Promise<ScannerContext> => {
    return requestData(
      api.get<ScannerContext>('/scanner/context'),
      'Failed to fetch scanner context'
    );
  },

  runScan: async (
    scanType: ScanType = 'market_close',
    tradeDate?: string
  ): Promise<RunScanResponse> => {
    const params: Record<string, string> = {
      scan_type: scanType,
      include_ml: 'true'
    };
    if (tradeDate) {
      params.trade_date = tradeDate;
    }
    return requestData(
      api.post<RunScanResponse>('/scanner/run', null, {
        params
      }),
      'Failed to run scanner'
    );
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
