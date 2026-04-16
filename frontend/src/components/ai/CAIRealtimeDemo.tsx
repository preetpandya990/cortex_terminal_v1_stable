/**
 * CAI Real-time Demo Component
 * 
 * Demonstrates WebSocket integration with all CAI panels.
 * This is an example component showing how to use real-time hooks.
 */
import React from 'react';
import { useSignalsRealtime } from '@/hooks/useSignalsRealtime';
import { useRegimeRealtime } from '@/hooks/useRegimeRealtime';
import { useEventsRealtime } from '@/hooks/useEventsRealtime';
import { useModelsRealtime } from '@/hooks/useModelsRealtime';
import { ConnectionStatusIndicator } from './ConnectionStatus';

export function CAIRealtimeDemo() {
  // Use real-time hooks with WebSocket integration
  const signals = useSignalsRealtime({ limit: 10 });
  const regime = useRegimeRealtime('RELIANCE');
  const events = useEventsRealtime({ min_impact: 70, limit: 10 });
  const models = useModelsRealtime();

  return (
    <div className="p-6 space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">CAI Real-time Dashboard</h1>
        <ConnectionStatusIndicator status={signals.wsStatus} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Signals Panel */}
        <div className="border rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4">Trading Signals</h2>
          {signals.isLoading ? (
            <p>Loading signals...</p>
          ) : signals.error ? (
            <p className="text-red-500">Error loading signals</p>
          ) : (
            <div className="space-y-2">
              {signals.data?.signals.slice(0, 5).map((signal) => (
                <div key={signal.signal_id} className="p-2 bg-gray-50 rounded">
                  <div className="flex justify-between">
                    <span className="font-medium">{signal.symbol}</span>
                    <span className={signal.signal_type === 'buy' ? 'text-green-600' : 'text-red-600'}>
                      {signal.signal_type.toUpperCase()}
                    </span>
                  </div>
                  <div className="text-sm text-gray-600">
                    Confidence: {signal.confidence}%
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Regime Panel */}
        <div className="border rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4">Market Regime</h2>
          {regime.isLoading ? (
            <p>Loading regime...</p>
          ) : regime.error ? (
            <p className="text-red-500">Error loading regime</p>
          ) : regime.data ? (
            <div className="space-y-2">
              <div className="p-2 bg-gray-50 rounded">
                <div className="font-medium">{regime.data.symbol}</div>
                <div className="text-sm text-gray-600">
                  Regime: {regime.data.regime_type}
                </div>
                <div className="text-sm text-gray-600">
                  Confidence: {regime.data.confidence}%
                </div>
              </div>
            </div>
          ) : null}
        </div>

        {/* Events Panel */}
        <div className="border rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4">High-Impact Events</h2>
          {events.isLoading ? (
            <p>Loading events...</p>
          ) : events.error ? (
            <p className="text-red-500">Error loading events</p>
          ) : (
            <div className="space-y-2">
              {events.data?.events.slice(0, 5).map((event) => (
                <div key={event.event_id} className="p-2 bg-gray-50 rounded">
                  <div className="font-medium">{event.affected_symbols?.join(', ') || 'N/A'}</div>
                  <div className="text-sm text-gray-600">{event.event_type}</div>
                  <div className="text-sm text-gray-600">
                    Impact: {event.impact_score}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Models Panel */}
        <div className="border rounded-lg p-4">
          <h2 className="text-lg font-semibold mb-4">ML Models</h2>
          {models.models.isLoading ? (
            <p>Loading models...</p>
          ) : models.models.error ? (
            <p className="text-red-500">Error loading models</p>
          ) : (
            <div className="space-y-2">
              {models.models.data?.models.slice(0, 5).map((model) => (
                <div key={model.model_id} className="p-2 bg-gray-50 rounded">
                  <div className="font-medium">{model.model_name}</div>
                  <div className="text-sm text-gray-600">
                    State: {model.deployment_state}
                  </div>
                  <div className="text-sm text-gray-600">
                    Accuracy: {Object.values(model.accuracy_metrics)[0] || 0}%
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
