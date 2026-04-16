'use client';

/**
 * CORTEX AI Dashboard Page
 * 
 * Real-time dashboard displaying all CAI microservice panels:
 * - Trading Signals
 * - Market Regime Detection
 * - High-Impact Events
 * - ML Model Monitoring
 */
import { CAIRealtimeDemo } from '@/components/ai/CAIRealtimeDemo';

export default function CAIDashboardPage() {
  return (
    <div className="container mx-auto">
      <CAIRealtimeDemo />
    </div>
  );
}
