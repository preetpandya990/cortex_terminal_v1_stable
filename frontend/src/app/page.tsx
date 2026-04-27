'use client';

import { useState } from 'react';
import { useAuth } from '@/contexts/AuthContext';
import { OpenPositionsPlaceholder } from "@/components/dashboard/OpenPositionsPlaceholder";
import { InstrumentSearchCombobox } from "@/components/market/InstrumentSearchCombobox";
import { DetailPane } from "@/app/hawk-eye-radar/components/DetailPane";
import type { UpstoxInstrument } from "@/types/upstox";

export default function Home() {
  const { isAuthenticated, isAuthReady } = useAuth();
  const [selectedInstrument, setSelectedInstrument] = useState<UpstoxInstrument | null>(null);

  const handleInstrumentSelect = (instrument: UpstoxInstrument) => {
    setSelectedInstrument(instrument);
  };

  const handleCloseDetail = () => {
    setSelectedInstrument(null);
  };

  return (
    <div className="space-y-6">
      {/* Search Bar — only rendered once auth state is known and user is logged in.
          During the initial refresh window (isAuthReady=false) nothing renders here,
          preventing instrument-search and LTP requests from firing unauthenticated. */}
      {isAuthReady && isAuthenticated && (
        <InstrumentSearchCombobox
          onSelect={handleInstrumentSelect}
          placeholder="Search stocks..."
          variant="dashboard"
          showQuickLtp={true}
        />
      )}

      {/* Open Positions */}
      {/* {isAuthenticated && <OpenPositionsPlaceholder />} */}

      {/* Detail Pane Overlay */}
      {selectedInstrument && (
        <DetailPane
          instrument={selectedInstrument}
          onClose={handleCloseDetail}
          showAnalysis={false}
        />
      )}
    </div>
  );
}
