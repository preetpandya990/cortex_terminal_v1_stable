export type TaskStatus = 'starting' | 'running' | 'paused' | 'crashed' | 'stopped'

export interface TaskDetail {
  name: string
  status: TaskStatus
  last_run_at: string | null  // ISO-8601 UTC
  crash_count: number
  is_paused: boolean
  has_handle: boolean
}

export interface WorkerTasksResponse {
  count: number
  tasks: Record<string, TaskDetail>
}

export interface WorkerHealthResponse {
  status: string
  uptime_seconds: number
  task_count: number
}

export interface TaskActionResponse {
  task: string
  status: string
}
