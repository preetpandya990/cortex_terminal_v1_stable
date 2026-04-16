/**
 * Health check response types
 * Used for monitoring application and backend status
 */

export type HealthStatus = 'healthy' | 'degraded' | 'error';

export interface HealthCheckResponse {
  status: HealthStatus;
  frontend: 'healthy' | 'degraded';
  backend: 'healthy' | 'unhealthy' | 'unreachable' | 'unknown';
  backendVersion?: string;
  responseTime: number;
  timestamp: string;
  error?: string;
}

export interface HealthCheckState {
  status: HealthStatus;
  lastCheckedAt: Date | null;
  lastSuccessAt: Date | null;
  consecutiveFailures: number;
  isChecking: boolean;
}
