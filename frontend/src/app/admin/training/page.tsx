"use client";

import { useCallback, useEffect, useState } from "react";
import { useToast } from "@/components/ui/toast";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { useAuth } from "@/contexts/AuthContext";
import { adminTrainingAPI, apiErrorMessage } from "@/lib/api";
import type {
  LaunchRequest,
  PreflightReport,
  RunSummary,
  TrainingTab,
} from "@/types/admin_training";
import { useTrainingRunStream } from "@/hooks/useTrainingRunStream";
import { PreflightBoard }   from "./components/PreflightBoard";
import { LaunchForm }       from "./components/LaunchForm";
import { LiveRunStream }    from "./components/LiveRunStream";
import { RunHistory }       from "./components/RunHistory";
import { FeedbackPreview }  from "./components/FeedbackPreview";

export default function MLTrainingPage() {
  const { accessToken }             = useAuth();
  const { success: toastOk, error: toastErr } = useToast();

  const [activeTab,        setActiveTab]        = useState<TrainingTab>("preflight");
  const [preflight,        setPreflight]        = useState<PreflightReport | null>(null);
  const [preflightLoading, setPreflightLoading] = useState(false);
  const [runs,             setRuns]             = useState<RunSummary[]>([]);
  const [runsLoading,      setRunsLoading]      = useState(false);
  const [isLaunching,      setIsLaunching]      = useState(false);
  const [isCancelling,     setIsCancelling]     = useState(false);

  // Active run tracking
  const [activeRunId,            setActiveRunId]           = useState<string | null>(null);
  // Phase 2 — active feedback bundle
  const [activeFeedbackBundlePath, setActiveFeedbackBundlePath] = useState<string | null>(null);

  // WS stream for the active run
  const {
    connectionState,
    events,
    completedSteps,
    currentStep,
    exitCode,
    disconnect: wsDisconnect,
  } = useTrainingRunStream(activeRunId, accessToken, !!activeRunId);

  // ── Preflight ───────────────────────────────────────────────────────────────
  const fetchPreflight = useCallback(async () => {
    setPreflightLoading(true);
    try {
      const report = await adminTrainingAPI.preflight();
      setPreflight(report);
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setPreflightLoading(false);
    }
  }, []);

  // ── Runs history ─────────────────────────────────────────────────────────────
  const fetchRuns = useCallback(async () => {
    setRunsLoading(true);
    try {
      const res = await adminTrainingAPI.getRuns(30);
      setRuns(res.runs);
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setRunsLoading(false);
    }
  }, []);

  // ── Check for existing active run on mount ────────────────────────────────
  useEffect(() => {
    adminTrainingAPI.getActiveRun().then(res => {
      if (res.active && res.run_id) {
        setActiveRunId(res.run_id);
        setActiveTab("live");
      }
    }).catch(() => {});
    fetchPreflight();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Tab change side effects ────────────────────────────────────────────────
  useEffect(() => {
    if (activeTab === "preflight") fetchPreflight();
    if (activeTab === "history")   fetchRuns();
  }, [activeTab]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Launch ────────────────────────────────────────────────────────────────
  const handleLaunch = useCallback(async (req: LaunchRequest) => {
    setIsLaunching(true);
    try {
      const res = await adminTrainingAPI.launch(req);
      setActiveRunId(res.run_id);
      toastOk(`Training run launched (pid tracking active)`);
      setActiveTab("live");
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setIsLaunching(false);
    }
  }, []);

  // ── Cancel ────────────────────────────────────────────────────────────────
  const handleCancel = useCallback(async () => {
    if (!activeRunId) return;
    setIsCancelling(true);
    try {
      await adminTrainingAPI.cancelRun(activeRunId);
      toastOk("SIGTERM sent — checkpoint preserved.");
    } catch (err) {
      toastErr(apiErrorMessage(err));
    } finally {
      setIsCancelling(false);
    }
  }, [activeRunId]);

  // When run completes, disconnect WS and refresh history
  useEffect(() => {
    if (exitCode !== null) {
      if (exitCode === 0) {
        toastOk("Training run completed. Models registered — promote via ML Governance.");
      } else if (exitCode !== null) {
        toastErr(`Training run failed (exit ${exitCode}). Check History tab.`);
      }
      // Refresh runs after a short delay
      setTimeout(() => {
        fetchRuns();
      }, 2000);
    }
  }, [exitCode, fetchRuns]);

  // ── Tab disabled state ────────────────────────────────────────────────────
  const launchDisabled = !preflight || !preflight.can_launch;

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">ML Training Console</h1>
        <p className="mt-1 text-sm text-slate-500">
          Pre-flight validation, training dispatch, live run monitoring, and run history.
          Operator-only — all actions are audited.
        </p>
      </div>

      <Tabs
        value={activeTab}
        onValueChange={v => setActiveTab(v as TrainingTab)}
        className="space-y-5"
      >
        <TabsList className="inline-flex rounded-xl border border-slate-200 bg-slate-100 p-1 shadow-sm">
          <TabsTrigger value="preflight" className="rounded-lg px-4 py-1.5 text-sm">
            Preflight
          </TabsTrigger>
          <TabsTrigger
            value="launch"
            className="rounded-lg px-4 py-1.5 text-sm"
            disabled={launchDisabled}
            title={launchDisabled ? "Fix all failing gates in Preflight before launching" : undefined}
          >
            Launch
          </TabsTrigger>
          <TabsTrigger value="live" className="rounded-lg px-4 py-1.5 text-sm">
            Live Run
            {activeRunId && connectionState === "connected" && (
              <span className="ml-2 h-1.5 w-1.5 rounded-full bg-emerald-400 inline-block animate-pulse" />
            )}
          </TabsTrigger>
          <TabsTrigger
            value="history"
            className="rounded-lg px-4 py-1.5 text-sm"
            onClick={() => fetchRuns()}
          >
            History
          </TabsTrigger>
          <TabsTrigger value="feedback" className="rounded-lg px-4 py-1.5 text-sm">
            Feedback
            {activeFeedbackBundlePath && (
              <span className="ml-2 h-1.5 w-1.5 rounded-full bg-indigo-500 inline-block" />
            )}
          </TabsTrigger>
        </TabsList>

        <TabsContent value="preflight">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <PreflightBoard
              report={preflight}
              isLoading={preflightLoading}
              onRefresh={fetchPreflight}
            />
          </div>
        </TabsContent>

        <TabsContent value="launch">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <LaunchForm
              preflight={preflight}
              isLaunching={isLaunching}
              onLaunch={handleLaunch}
              activeFeedbackBundlePath={activeFeedbackBundlePath}
            />
          </div>
        </TabsContent>

        <TabsContent value="live">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <LiveRunStream
              runId={activeRunId}
              connectionState={connectionState}
              events={events}
              completedSteps={completedSteps}
              currentStep={currentStep}
              exitCode={exitCode}
              onCancel={handleCancel}
              isCancelling={isCancelling}
            />
          </div>
        </TabsContent>

        <TabsContent value="history">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <RunHistory
              runs={runs}
              isLoading={runsLoading}
              onRefresh={fetchRuns}
            />
          </div>
        </TabsContent>

        <TabsContent value="feedback">
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <FeedbackPreview
              selectedBundlePath={activeFeedbackBundlePath}
              onBundleSelect={setActiveFeedbackBundlePath}
            />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
