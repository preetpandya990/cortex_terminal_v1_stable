/**
 * API Client - Updated to use auth-aware client and BFF proxy
 * 
 * This file maintains backward compatibility with existing components
 * while using the new authentication system.
 */

import { AxiosResponse } from 'axios';
import { api, requestData, APIError } from './api-client';
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

// Export API base URL for components that need it
export const API_BASE_URL = '/api/v1';
export const API_ORIGIN = typeof window !== 'undefined' 
  ? `${window.location.protocol}//${window.location.hostname}:8000`
  : 'http://localhost:8000';
export const WS_BASE_URL = API_ORIGIN.replace(/^http/i, 'ws');

// Re-export the auth-aware axios instance
export { api };

// Re-export error class for backward compatibility
export { APIError as APIRequestError };

// Market Data API
export const marketDataAPI = {
  getStockData: async (symbol: string, params?: {
    timeframe?: string;
    start_date?: string;
    end_date?: string;
    limit?: number;
  }) => {
    return requestData(
      api.get(`/market/stocks/${symbol}/data`, { params }),
      'Failed to fetch stock data'
    );
  },

  getStockIndicators: async (symbol: string, params?: {
    timeframe?: string;
    limit?: number;
  }) => {
    return requestData(
      api.get(`/market/stocks/${symbol}/indicators`, { params }),
      'Failed to fetch stock indicators'
    );
  },

  refreshStockData: async (symbol: string) => {
    return requestData(
      api.post(`/market/stocks/${symbol}/refresh`),
      'Failed to refresh stock data'
    );
  },

  getOhlcv: async (
    instrumentKey: string,
    options?: { timeframe?: string; limit?: number; signal?: AbortSignal }
  ) => {
    const { signal, ...params } = options ?? {};
    return requestData(
      api.get(`/market-data/ohlcv/${instrumentKey}`, {
        params,
        signal,
      }),
      'Failed to fetch OHLCV data'
    );
  },

  getLivePrice: async (instrumentKey: string, signal?: AbortSignal) => {
    return requestData(
      api.get(`/market-data/live/${instrumentKey}`, { signal }),
      'Failed to fetch live price'
    );
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
    return requestData(
      api.get('/upstox/instruments/search', {
        params: { q: query, ...params },
      }),
      'Failed to search instruments'
    );
  },

  getOhlcQuote: async (instrumentKey: string, interval?: string) => {
    const params: Record<string, string> = { instrument_key: instrumentKey };
    if (interval) {
      params.interval = interval;
    }
    return requestData(
      api.get('/upstox/ohlc', { params }),
      'Failed to fetch OHLC quote'
    );
  },

  getLtpQuote: async (instrumentKey: string) => {
    return requestData(
      api.get('/upstox/ltp', {
        params: { instrument_key: instrumentKey },
      }),
      'Failed to fetch LTP quote'
    );
  },

  getHistoricalCandles: async (
    instrumentKey: string,
    params: { unit: string; interval: number; to_date: string; from_date?: string }
  ) => {
    return requestData(
      api.get('/upstox/candles/historical', {
        params: { instrument_key: instrumentKey, ...params },
      }),
      'Failed to fetch historical candles'
    );
  },

  getIntradayCandles: async (
    instrumentKey: string,
    params: { unit: string; interval: number }
  ) => {
    return requestData(
      api.get('/upstox/candles/intraday', {
        params: { instrument_key: instrumentKey, ...params },
      }),
      'Failed to fetch intraday candles'
    );
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
