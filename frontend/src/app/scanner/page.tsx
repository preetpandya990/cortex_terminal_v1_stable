'use client';

import { useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api-client';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RefreshCw, AlertTriangle, Clock3, ChartColumnBig } from 'lucide-react';
import { RunScanResponse, ScanResultsData, ScanType, ScannerContext } from '@/types/market';
import { StockCard } from '@/components/scanner/StockCard';
import { ScanResults } from '@/components/scanner/ScanResults';

const scanTypeLabels: Record<ScanType, string> = {
  market_open: 'Market Open',
  market_close: 'Market Close',
  intraday: 'Intraday',
};

const emptyResults: ScanResultsData = {
  top_gainers: [],
  top_losers: [],
  volume_spikes: [],
  breakouts: [],
  total_scanned: 0,
  errors: [],
  metadata: {},
};

export default function ScannerPage() {
  const queryClient = useQueryClient();
  const { isAuthenticated } = useAuth();
  const [scanType, setScanType] = useState<ScanType>('market_close');
  const [selectedCloseDate, setSelectedCloseDate] = useState<string>('');

  const {
    data: scanData,
    error: latestScanError,
    isError: isLatestScanError,
    isLoading,
    isFetching,
    refetch,
  } = useQuery({
    queryKey: ['scanner', 'latest'],
    queryFn: async () => {
      const response = await api.get('/scanner/latest');
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 60000,
  });

  const {
    data: scannerContext,
    isLoading: isContextLoading,
    error: scannerContextError,
    isError: isScannerContextError,
  } = useQuery({
    queryKey: ['scanner', 'context'],
    queryFn: async () => {
      const response = await api.get('/scanner/context');
      return response.data;
    },
    enabled: isAuthenticated,
    refetchInterval: 60000,
  });

  const runScanMutation = useMutation({
    mutationFn: async ({ selectedType, tradeDate }: { selectedType: ScanType; tradeDate?: string }) => {
      const params: Record<string, string> = {
        scan_type: selectedType,
        include_ml: 'true'
      };
      if (tradeDate) {
        params.trade_date = tradeDate;
      }
      const response = await api.post('/scanner/run', params);
      return response.data;
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['scanner', 'latest'] });
    },
  });

  const latestScanTimestamp = scanData?.timestamp
    ? new Date(scanData.timestamp).toLocaleString()
    : null;

  const resultSet = useMemo(() => scanData?.results ?? emptyResults, [scanData]);
  const quoteAnalyses = useMemo(() => {
    const raw = resultSet.metadata?.quote_analyses;
    return typeof raw === 'number' && Number.isFinite(raw) ? raw : null;
  }, [resultSet.metadata]);
  const totalScanned = quoteAnalyses ?? scanData?.total_scanned ?? resultSet.total_scanned;

  const isRunDisabled = runScanMutation.isPending || isFetching || isContextLoading;
  const isModeSelectionLocked = Boolean(scannerContext && !scannerContext.is_market_open);
  const closeDateOptions = scannerContext?.selectable_market_close_dates ?? [];
  const effectiveScanType: ScanType = isModeSelectionLocked ? 'market_close' : scanType;
  const effectiveCloseDate = selectedCloseDate || closeDateOptions[0] || '';

  const runScan = () => {
    const tradeDate = effectiveScanType === 'market_close' ? effectiveCloseDate : undefined;
    runScanMutation.mutate({ selectedType: effectiveScanType, tradeDate });
  };

  const latestErrorLabel = getErrorLabel(latestScanError);
  const contextErrorLabel = getErrorLabel(scannerContextError);
  const runErrorLabel = getErrorLabel(runScanMutation.error);

  if (isLoading || isContextLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <div className="flex items-center gap-2 text-lg text-muted-foreground">
          <RefreshCw className="h-4 w-4 animate-spin" />
          Loading scanner data...
        </div>
      </div>
    );
  }

  if (isLatestScanError && !scanData) {
    return (
      <div className="space-y-6">
        <Card className="border-red-200 bg-red-50/70">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-red-700">
              <AlertTriangle className="h-4 w-4" />
              Scanner API unavailable
            </CardTitle>
            <CardDescription className="text-red-600">
              {latestErrorLabel}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" onClick={() => refetch()}>
              Retry
            </Button>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="space-y-2">
          <h1 className="text-3xl font-bold">Market Scanner</h1>
          <p className="text-sm text-muted-foreground">
            {latestScanTimestamp ? `Last scan: ${latestScanTimestamp}` : 'No scan has been executed yet.'}
          </p>
          {scanData ? (
            <div className="flex items-center gap-2">
              <Badge variant="outline">{scanTypeLabels[scanData.scan_type]}</Badge>
              {typeof scanData.processing_time_ms === 'number' ? (
                <Badge variant="secondary" className="font-mono">
                  <Clock3 className="mr-1 h-3 w-3" />
                  {scanData.processing_time_ms} ms
                </Badge>
              ) : null}
            </div>
          ) : null}
        </div>

        <div className="flex flex-wrap items-center gap-2">
          {(Object.keys(scanTypeLabels) as ScanType[]).map((type) => (
            <Button
              key={type}
              variant={effectiveScanType === type ? 'default' : 'outline'}
              onClick={() => setScanType(type)}
              disabled={runScanMutation.isPending || (isModeSelectionLocked && type !== 'market_close')}
            >
              {scanTypeLabels[type]}
            </Button>
          ))}

          {effectiveScanType === 'market_close' ? (
            <select
              aria-label="Select market close trading day"
              className="h-9 rounded-md border bg-background px-3 text-sm"
              value={effectiveCloseDate}
              onChange={(e) => setSelectedCloseDate(e.target.value)}
              disabled={runScanMutation.isPending || closeDateOptions.length === 0}
            >
              {closeDateOptions.map((day) => (
                <option key={day} value={day}>
                  {day}
                </option>
              ))}
            </select>
          ) : null}

          <Button onClick={runScan} disabled={isRunDisabled}>
            <RefreshCw className={`mr-2 h-4 w-4 ${runScanMutation.isPending ? 'animate-spin' : ''}`} />
            {runScanMutation.isPending ? 'Running...' : 'Run Scan'}
          </Button>
        </div>
      </div>

      {scannerContext ? <SessionBanner scannerContext={scannerContext} /> : null}

      {isLatestScanError ? (
        <Card className="border-red-200 bg-red-50/70">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-red-700">
              <AlertTriangle className="h-4 w-4" />
              Failed to load latest scan
            </CardTitle>
            <CardDescription className="text-red-600">
              {latestErrorLabel}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" onClick={() => refetch()}>
              Retry
            </Button>
          </CardContent>
        </Card>
      ) : null}

      {isScannerContextError ? (
        <Card className="border-red-200 bg-red-50/70">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-red-700">
              <AlertTriangle className="h-4 w-4" />
              Failed to load scanner market context
            </CardTitle>
            <CardDescription className="text-red-600">
              {contextErrorLabel}
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      {runScanMutation.isError ? (
        <Card className="border-red-200 bg-red-50/70">
          <CardHeader className="pb-3">
            <CardTitle className="flex items-center gap-2 text-red-700">
              <AlertTriangle className="h-4 w-4" />
              Scan execution failed
            </CardTitle>
            <CardDescription className="text-red-600">
              {runErrorLabel}
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      {runScanMutation.isSuccess ? (
        <ScanSuccessBanner payload={runScanMutation.data} />
      ) : null}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
        <StockCard title="Total Scanned" value={totalScanned} />
        <StockCard title="Top Gainers" value={resultSet.top_gainers.length} tone="positive" />
        <StockCard title="Top Losers" value={resultSet.top_losers.length} tone="negative" />
        <StockCard title="Volume Spikes" value={resultSet.volume_spikes.length} tone="info" />
      </div>

      {!scanData ? (
        <Card className="border-dashed">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ChartColumnBig className="h-5 w-5" />
              Scanner is ready
            </CardTitle>
            <CardDescription>
              Execute your first market scan to populate gainers, losers, volume spikes, and breakouts.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={runScan} disabled={isRunDisabled}>
              <RefreshCw className={`mr-2 h-4 w-4 ${runScanMutation.isPending ? 'animate-spin' : ''}`} />
              Run First Scan
            </Button>
          </CardContent>
        </Card>
      ) : (
        <ScanResults
          topGainers={resultSet.top_gainers}
          topLosers={resultSet.top_losers}
          volumeSpikes={resultSet.volume_spikes}
          breakouts={resultSet.breakouts}
        />
      )}
    </div>
  );
}

function ScanSuccessBanner({ payload }: { payload: RunScanResponse }) {
  const firstError = payload.results.errors[0] as Record<string, unknown> | undefined;
  const firstErrorCode = (firstError?.code as string | undefined) ?? payload.primary_error_code ?? 'unknown_error';
  const firstErrorMessage = (firstError?.message as string | undefined) ?? 'No additional error details provided.';

  if (payload.status === 'failed') {
    return (
      <Card className="border-red-200 bg-red-50/70">
        <CardHeader className="pb-3">
          <CardTitle className="text-red-700">Scan failed</CardTitle>
          <CardDescription className="text-red-600">
            {scanTypeLabels[payload.scan_type]} scan could not fetch market data. Code: {firstErrorCode}. {firstErrorMessage}
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  if (payload.status === 'partial') {
    return (
      <Card className="border-amber-200 bg-amber-50/70">
        <CardHeader className="pb-3">
          <CardTitle className="text-amber-700">{payload.message}</CardTitle>
          <CardDescription className="text-amber-600">
            {scanTypeLabels[payload.scan_type]} scan processed {payload.total_scanned} symbols with {payload.error_count} error(s). First code: {firstErrorCode}.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card className="border-emerald-200 bg-emerald-50/70">
      <CardHeader className="pb-3">
        <CardTitle className="text-emerald-700">{payload.message}</CardTitle>
        <CardDescription className="text-emerald-600">
          {scanTypeLabels[payload.scan_type]} scan completed. {payload.total_scanned} symbols evaluated.
        </CardDescription>
      </CardHeader>
    </Card>
  );
}

function SessionBanner({ scannerContext }: { scannerContext: ScannerContext }) {
  const marketStatusText = scannerContext.is_market_open
    ? 'Market is open. All scan modes are enabled.'
    : 'Market is closed. Only Market Close mode is enabled.';

  return (
    <Card className="border-slate-200 bg-slate-50/70">
      <CardHeader className="pb-3">
        <CardTitle className="text-slate-800">Session Control</CardTitle>
        <CardDescription className="text-slate-600">{marketStatusText}</CardDescription>
      </CardHeader>
    </Card>
  );
}

function getErrorLabel(error: unknown): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const axiosError = error as any;
    const status = axiosError.response?.status;
    const message = axiosError.response?.data?.detail || axiosError.message;
    if (status && message) {
      return `HTTP ${status}: ${message}`;
    }
    return message || 'Request failed.';
  }
  if (error instanceof Error) {
    return error.message;
  }
  return 'Please try again.';
}
