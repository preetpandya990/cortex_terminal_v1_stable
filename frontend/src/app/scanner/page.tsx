'use client';

import { useCallback, useMemo, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '@/lib/api-client';
import { useAuth } from '@/contexts/AuthContext';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { RefreshCw, AlertTriangle, Clock3, ChartColumnBig, DatabaseZap } from 'lucide-react';
import { RunScanResponse, ScanResultsData, ScanType, ScannerContext, StockAnalysis } from '@/types/market';
import { ScanResults, type ScannerTab } from '@/components/scanner/ScanResults';
import { ScannerDetailPane } from '@/components/scanner/ScannerDetailPane';

const emptyResults: ScanResultsData = {
  top_gainers: [],
  top_losers: [],
  volume_spikes: [],
  total_scanned: 0,
  errors: [],
  metadata: {},
};

export default function ScannerPage() {
  const queryClient = useQueryClient();
  const { isAuthenticated, isAuthReady } = useAuth();

  const [selectedStock, setSelectedStock] = useState<StockAnalysis | null>(null);
  const [selectedListType, setSelectedListType] = useState<ScannerTab>('gainers');

  const handleStockSelect = useCallback((stock: StockAnalysis, listType: ScannerTab) => {
    setSelectedStock(stock);
    setSelectedListType(listType);
  }, []);

  const handlePaneClose = useCallback(() => {
    setSelectedStock(null);
  }, []);

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
    refetchInterval: 60_000,
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
    refetchInterval: 60_000,
  });

  const runScanMutation = useMutation({
    mutationFn: async ({ selectedType }: { selectedType: ScanType }) => {
      const response = await api.post('/scanner/run', { scan_type: selectedType });
      return response.data as RunScanResponse;
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['scanner', 'latest'] });
    },
  });

  const latestScanTimestamp = scanData?.timestamp
    ? new Date(scanData.timestamp).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })
    : null;

  // Prefer fresh mutation results — POST /scanner/run returns the full results
  // in its body. Using them directly avoids a stale-cache race on the GET /latest refetch.
  const resultSet = useMemo(() => {
    if (runScanMutation.isSuccess && runScanMutation.data?.results) {
      return runScanMutation.data.results;
    }
    return scanData?.results ?? emptyResults;
  }, [scanData?.results, runScanMutation.isSuccess, runScanMutation.data?.results]);
  const totalScanned: number = useMemo(() => {
    const raw = resultSet.metadata?.quote_analyses;
    return typeof raw === 'number' && Number.isFinite(raw)
      ? raw
      : (scanData?.total_scanned ?? resultSet.total_scanned);
  }, [resultSet, scanData?.total_scanned]);

  const isRunDisabled = runScanMutation.isPending || isFetching || isContextLoading;

  const runScan = () => {
    runScanMutation.mutate({ selectedType: 'market_close' });
  };

  if (!isAuthReady || !isAuthenticated) return null;

  if (isLoading || isContextLoading) {
    return (
      <div className="flex items-center justify-center min-h-[40vh]">
        <div className="flex items-center gap-2 text-lg text-muted-foreground">
          <RefreshCw className="h-4 w-4 animate-spin" />
          Loading scanner...
        </div>
      </div>
    );
  }

  if (isLatestScanError && !scanData) {
    return (
      <div className="space-y-6">
        <ErrorCard title="Scanner unavailable" message={getErrorLabel(latestScanError)}>
          <Button variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </ErrorCard>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ── Header ── */}
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex flex-wrap items-center gap-6">
          <div>
            <div className="flex items-center gap-3">
              <h1 className="text-2xl font-bold">Market Scanner</h1>
              {typeof scanData?.processing_time_ms === 'number' ? (
                <Badge variant="secondary" className="font-mono">
                  <Clock3 className="mr-1 h-3 w-3" />
                  {scanData.processing_time_ms} ms
                </Badge>
              ) : null}
            </div>
            <p className="mt-0.5 text-sm text-muted-foreground">
              {latestScanTimestamp
                ? `Last scan: ${latestScanTimestamp} IST`
                : 'No scan has been run yet.'}
            </p>
          </div>

          {/* ── Market status indicator ── */}
          {scannerContext ? (
            <MarketStatusIndicator scannerContext={scannerContext} />
          ) : null}

          {/* ── Inline stats ── */}
          {scanData ? (
            <div className="flex items-center gap-5 border-l border-slate-200 pl-6">
              <div className="text-center">
                <p className="text-lg font-semibold tabular-nums">{totalScanned}</p>
                <p className="text-xs text-muted-foreground">Scanned</p>
              </div>
              <div className="text-center">
                <p className="text-lg font-semibold tabular-nums text-emerald-600">
                  {resultSet.top_gainers.length}
                </p>
                <p className="text-xs text-muted-foreground">Gainers</p>
              </div>
              <div className="text-center">
                <p className="text-lg font-semibold tabular-nums text-red-600">
                  {resultSet.top_losers.length}
                </p>
                <p className="text-xs text-muted-foreground">Losers</p>
              </div>
            </div>
          ) : null}
        </div>

        {/* ── Controls ── */}
        <Button onClick={runScan} disabled={isRunDisabled}>
          <RefreshCw
            className={`mr-2 h-4 w-4 ${runScanMutation.isPending ? 'animate-spin' : ''}`}
          />
          {runScanMutation.isPending ? 'Scanning...' : 'Run Scan'}
        </Button>
      </div>

      {/* ── Banners ── */}
      {isLatestScanError ? (
        <ErrorCard title="Failed to load latest scan" message={getErrorLabel(latestScanError)}>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            Retry
          </Button>
        </ErrorCard>
      ) : null}

      {isScannerContextError ? (
        <ErrorCard
          title="Failed to load market context"
          message={getErrorLabel(scannerContextError)}
        />
      ) : null}

      {runScanMutation.isError ? (
        <ErrorCard title="Scan failed" message={getErrorLabel(runScanMutation.error)} />
      ) : null}

      {runScanMutation.isSuccess ? (
        <ScanSuccessBanner payload={runScanMutation.data} />
      ) : null}

      {scanData && scanData.live_prices_available === false ? (
        <Card className="border-amber-200 bg-amber-50/70">
          <CardHeader className="py-3">
            <CardTitle className="flex items-center gap-2 text-amber-700 text-sm font-medium">
              <DatabaseZap className="h-4 w-4" />
              Live prices unavailable — results reflect last stored close prices. Connect your Upstox account to enable live data.
            </CardTitle>
          </CardHeader>
        </Card>
      ) : null}

      {scanData && scanData.stale_instrument_count > 0 ? (
        <Card className="border-amber-200 bg-amber-50/70">
          <CardHeader className="py-3">
            <CardTitle className="flex items-center gap-2 text-amber-700 text-sm font-medium">
              <DatabaseZap className="h-4 w-4" />
              {scanData.stale_instrument_count} instrument{scanData.stale_instrument_count !== 1 ? 's' : ''} showing stale data — live prices unavailable for these symbols. Hover the ⚠ icon on a row for details.
            </CardTitle>
          </CardHeader>
        </Card>
      ) : null}

      {/* ── Results ── */}
      {!scanData ? (
        <Card className="border-dashed">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ChartColumnBig className="h-5 w-5" />
              Scanner ready
            </CardTitle>
            <CardDescription>
              Run a scan to see top gainers, top losers, and volume spikes across all NSE instruments.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button onClick={runScan} disabled={isRunDisabled}>
              <RefreshCw
                className={`mr-2 h-4 w-4 ${runScanMutation.isPending ? 'animate-spin' : ''}`}
              />
              Run First Scan
            </Button>
          </CardContent>
        </Card>
      ) : (
        <ScanResults
          topGainers={resultSet.top_gainers}
          topLosers={resultSet.top_losers}
          volumeSpikes={resultSet.volume_spikes}
          onStockSelect={handleStockSelect}
        />
      )}

      <ScannerDetailPane
        stock={selectedStock}
        listType={selectedListType}
        open={selectedStock !== null}
        onClose={handlePaneClose}
      />
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────────

function MarketStatusIndicator({ scannerContext }: { scannerContext: ScannerContext }) {
  const isOpen = scannerContext.is_market_open;

  return (
    <div className={`flex items-start gap-3 rounded-xl border px-4 py-2.5 ${
      isOpen
        ? 'border-emerald-200 bg-emerald-50'
        : 'border-slate-200 bg-slate-50'
    }`}>
      <div className="mt-0.5 flex-shrink-0">
        <span className={`relative flex h-2.5 w-2.5`}>
          {isOpen && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-emerald-400 opacity-75" />
          )}
          <span className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
            isOpen ? 'bg-emerald-500' : 'bg-slate-400'
          }`} />
        </span>
      </div>
      <div>
        <p className={`text-sm font-semibold leading-none ${
          isOpen ? 'text-emerald-700' : 'text-slate-700'
        }`}>
          {isOpen ? 'Market Open' : 'Market Closed'}
        </p>
        {!isOpen && scannerContext.closed_reason ? (
          <p className="mt-1 text-xs text-slate-500">{scannerContext.closed_reason}</p>
        ) : isOpen && scannerContext.market_close_utc ? (
          <p className="mt-1 text-xs text-emerald-600">
            Closes at 3:30 PM IST
          </p>
        ) : null}
      </div>
    </div>
  );
}

function ScanSuccessBanner({ payload }: { payload: RunScanResponse }) {
  const firstError = payload.results.errors[0] as Record<string, unknown> | undefined;
  const errorCode =
    (firstError?.code as string | undefined) ?? payload.primary_error_code ?? 'unknown_error';
  const errorMessage =
    (firstError?.message as string | undefined) ?? 'No additional details.';

  if (payload.status === 'failed') {
    return (
      <ErrorCard
        title="Scan failed"
        message={payload.message || `Code: ${errorCode}. ${errorMessage}`}
      />
    );
  }

  if (payload.status === 'partial') {
    return (
      <Card className="border-amber-200 bg-amber-50/70">
        <CardHeader className="py-3">
          <CardTitle className="text-amber-700">{payload.message}</CardTitle>
          <CardDescription className="text-amber-600">
            {payload.total_scanned} instruments evaluated, {payload.error_count} error(s). Code: {errorCode}.
          </CardDescription>
        </CardHeader>
      </Card>
    );
  }

  return (
    <Card className="border-emerald-200 bg-emerald-50/70">
      <CardHeader className="py-3">
        <CardTitle className="text-emerald-700">{payload.message}</CardTitle>
        <CardDescription className="text-emerald-600">
          {payload.total_scanned} instruments evaluated.
        </CardDescription>
      </CardHeader>
    </Card>
  );
}

function ErrorCard({
  title,
  message,
  children,
}: {
  title: string;
  message?: string;
  children?: React.ReactNode;
}) {
  return (
    <Card className="border-red-200 bg-red-50/70">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-red-700">
          <AlertTriangle className="h-4 w-4" />
          {title}
        </CardTitle>
        {message ? (
          <CardDescription className="text-red-600">{message}</CardDescription>
        ) : null}
      </CardHeader>
      {children ? <CardContent>{children}</CardContent> : null}
    </Card>
  );
}

function getErrorLabel(error: unknown): string {
  if (error && typeof error === 'object' && 'response' in error) {
    const axiosError = error as { response?: { status?: number; data?: { detail?: string } }; message?: string };
    const status = axiosError.response?.status;
    const message = axiosError.response?.data?.detail ?? (axiosError as { message?: string }).message;
    if (status && message) return `HTTP ${status}: ${message}`;
    return message ?? 'Request failed.';
  }
  if (error instanceof Error) return error.message;
  return 'Please try again.';
}
