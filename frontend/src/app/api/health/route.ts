import { NextResponse } from 'next/server';

const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:8000';
const HEALTH_CHECK_TIMEOUT = 3000; // 3 seconds

/**
 * Health check endpoint for monitoring application status
 * 
 * This endpoint checks:
 * - Next.js server is responding
 * - Backend API is reachable and healthy
 * 
 * Used by:
 * - Load balancers / orchestrators (K8s, Docker)
 * - Monitoring services (Datadog, New Relic, etc.)
 * - Client-side health checks (HealthCheckWrapper)
 * 
 * Returns:
 * - 200: All systems operational
 * - 503: Backend unavailable (Next.js still responding)
 * - 500: Internal error
 */
export async function GET() {
  const startTime = Date.now();

  try {
    // Create AbortController for timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), HEALTH_CHECK_TIMEOUT);

    try {
      // Check backend health
      const response = await fetch(`${BACKEND_URL}/health`, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
        },
        signal: controller.signal,
        cache: 'no-store', // Never cache health checks
      });

      clearTimeout(timeoutId);

      const responseTime = Date.now() - startTime;

      if (!response.ok) {
        return NextResponse.json(
          {
            status: 'degraded',
            frontend: 'healthy',
            backend: 'unhealthy',
            responseTime,
            timestamp: new Date().toISOString(),
          },
          { status: 503 }
        );
      }

      // Parse backend response
      const backendHealth = await response.json();

      return NextResponse.json(
        {
          status: 'healthy',
          frontend: 'healthy',
          backend: 'healthy',
          backendVersion: backendHealth.version,
          responseTime,
          timestamp: new Date().toISOString(),
        },
        { 
          status: 200,
          headers: {
            'Cache-Control': 'no-store, no-cache, must-revalidate',
          },
        }
      );
    } catch (fetchError) {
      clearTimeout(timeoutId);

      // Distinguish between timeout and other errors
      const isTimeout = fetchError instanceof Error && fetchError.name === 'AbortError';
      const responseTime = Date.now() - startTime;

      return NextResponse.json(
        {
          status: 'degraded',
          frontend: 'healthy',
          backend: 'unreachable',
          error: isTimeout ? 'Backend health check timeout' : 'Backend connection failed',
          responseTime,
          timestamp: new Date().toISOString(),
        },
        { status: 503 }
      );
    }
  } catch (error) {
    // Unexpected error in health check logic itself
    console.error('[Health Check] Unexpected error:', error);

    return NextResponse.json(
      {
        status: 'error',
        frontend: 'degraded',
        backend: 'unknown',
        error: 'Health check failed',
        timestamp: new Date().toISOString(),
      },
      { status: 500 }
    );
  }
}
