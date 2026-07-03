'use client'

import { useWorkerHealth, useWorkerTasks } from '@/hooks/useWorkerControl'
import { WorkerHealthBanner } from '@/components/admin/WorkerHealthBanner'
import { TasksGrid } from '@/components/admin/TasksGrid'
import { AIProcessingQueueCard } from '@/components/admin/AIProcessingQueueCard'

export default function WorkersPage() {
  const health = useWorkerHealth()
  const tasks  = useWorkerTasks()

  const isDegraded = health.isDegraded

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold tracking-tight text-slate-900">Worker Control</h1>
        <p className="mt-1 text-sm text-slate-500">
          Monitor and control all background workers. Trigger, pause, resume, or restart tasks
          on demand. Operator-only — all actions are logged.
        </p>
      </div>

      {/* Sidecar liveness + uptime */}
      <WorkerHealthBanner
        health={health.data}
        isLoading={health.isLoading}
        isDegraded={isDegraded}
      />

      {/* Live task grid */}
      <TasksGrid
        data={tasks.data}
        isLoading={tasks.isLoading}
        isError={tasks.isError}
        isRefetching={tasks.isRefetching}
        dataUpdatedAt={tasks.dataUpdatedAt}
        isDegraded={isDegraded}
        onRefresh={() => tasks.refetch()}
      />

      {/* Demand-driven AI processing queue control */}
      <AIProcessingQueueCard isDegraded={isDegraded} />
    </div>
  )
}
