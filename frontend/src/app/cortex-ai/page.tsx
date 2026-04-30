'use client';

import React, { useState, useCallback } from 'react';
import { SignalsPanel } from '@/components/ai/SignalsPanel';
import { RegimePanel } from '@/components/ai/RegimePanel';
import { EventsPanel } from '@/components/ai/EventsPanel';
import { MLModelsPanel } from '@/components/ai/MLModelsPanel';
import { DeprecatedModelsPanel } from '@/components/ai/DeprecatedModelsPanel';
import { ConnectionStatusIndicator } from '@/components/ai/ConnectionStatus';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/contexts/AuthContext';
import type { ConnectionStatus } from '@/hooks/useCAIWebSocket';

export default function CortexAIPage() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const [activeTab, setActiveTab] = useState('signals');
  const [wsStatus, setWsStatus] = useState<ConnectionStatus>('disconnected');

  const handleWsStatusChange = useCallback((status: ConnectionStatus) => {
    setWsStatus(status);
  }, []);

  if (!isAuthReady || !isAuthenticated) return null;

  return (
    <div className="container mx-auto p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold">Cortex AI</h1>
          <p className="text-muted-foreground mt-1">
            Real-time intelligence and market analysis powered by AI
          </p>
        </div>
        <ConnectionStatusIndicator status={wsStatus} />
      </div>

      {/* Main Content */}
      <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
        <TabsList className="grid w-full grid-cols-4">
          <TabsTrigger value="signals">Trading Signals</TabsTrigger>
          <TabsTrigger value="regime">Market Regime</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
          <TabsTrigger value="models">ML Models</TabsTrigger>
        </TabsList>

        <TabsContent value="signals" className="mt-6">
          <SignalsPanel onWsStatusChange={handleWsStatusChange} />
        </TabsContent>

        <TabsContent value="regime" className="mt-6">
          <RegimePanel />
        </TabsContent>

        <TabsContent value="events" className="mt-6">
          <EventsPanel />
        </TabsContent>

        <TabsContent value="models" className="mt-6 space-y-4">
          <MLModelsPanel isAdmin={true} />
          <DeprecatedModelsPanel isAdmin={true} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
