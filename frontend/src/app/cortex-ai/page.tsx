'use client';

/**
 * Cortex AI Page
 * 
 * Integrated dashboard for CORTEX AI microservice featuring:
 * - Real-time Trading Signals
 * - Market Regime Detection
 * - High-Impact Events Monitoring
 * - ML Model Governance
 */
import React, { useState } from 'react';
import { SignalsPanel } from '@/components/ai/SignalsPanel';
import { RegimePanel } from '@/components/ai/RegimePanel';
import { EventsPanel } from '@/components/ai/EventsPanel';
import { MLModelsPanel } from '@/components/ai/MLModelsPanel';
import { ConnectionStatusIndicator } from '@/components/ai/ConnectionStatus';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

export default function CortexAIPage() {
  const [activeTab, setActiveTab] = useState('signals');

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
        <ConnectionStatusIndicator status="connected" />
      </div>

      {/* Main Content - Tabbed Interface */}
      <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
        <TabsList className="grid w-full grid-cols-4">
          <TabsTrigger value="signals">Trading Signals</TabsTrigger>
          <TabsTrigger value="regime">Market Regime</TabsTrigger>
          <TabsTrigger value="events">Events</TabsTrigger>
          <TabsTrigger value="models">ML Models</TabsTrigger>
        </TabsList>

        <TabsContent value="signals" className="mt-6">
          <SignalsPanel />
        </TabsContent>

        <TabsContent value="regime" className="mt-6">
          <RegimePanel />
        </TabsContent>

        <TabsContent value="events" className="mt-6">
          <EventsPanel />
        </TabsContent>

        <TabsContent value="models" className="mt-6">
          <MLModelsPanel isAdmin={true} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
