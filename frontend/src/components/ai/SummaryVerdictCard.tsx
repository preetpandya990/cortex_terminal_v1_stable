/**
 * Summary & Verdict Card Component
 * Displays overall trading verdict and risk assessment
 */
import React from 'react';
import { useVerdictAnalysis } from '@/hooks/useVerdictAnalysis';

interface SummaryVerdictCardProps {
  symbol: string;
}

export function SummaryVerdictCard({ symbol }: SummaryVerdictCardProps) {
  const { data, isLoading, error } = useVerdictAnalysis(symbol);

  if (isLoading) {
    return (
      <div className="bg-white rounded-lg shadow p-6 animate-pulse">
        <div className="h-4 bg-gray-200 rounded w-1/3 mb-4"></div>
        <div className="space-y-3">
          <div className="h-3 bg-gray-200 rounded"></div>
          <div className="h-3 bg-gray-200 rounded w-5/6"></div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-white rounded-lg shadow p-6">
        <h3 className="text-lg font-semibold text-gray-900 mb-2">Summary & Verdict</h3>
        <p className="text-sm text-red-600">Failed to load verdict</p>
      </div>
    );
  }

  if (!data) return null;

  const verdictColor = 
    data.overall_verdict === 'buy' ? 'text-green-600 bg-green-100 border-green-300' :
    data.overall_verdict === 'sell' ? 'text-red-600 bg-red-100 border-red-300' :
    'text-yellow-600 bg-yellow-100 border-yellow-300';

  const riskColor = 
    data.risk_level === 'low' ? 'text-green-600' :
    data.risk_level === 'high' ? 'text-red-600' :
    'text-yellow-600';

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">Summary & Verdict</h3>
      
      {/* Verdict Badge */}
      <div className="mb-4">
        <div className={`inline-flex items-center px-4 py-2 rounded-lg border-2 ${verdictColor}`}>
          <span className="text-xl font-bold uppercase">{data.overall_verdict}</span>
        </div>
      </div>

      {/* Confidence and Risk */}
      <div className="flex items-center gap-6 mb-4">
        <div>
          <span className="text-xs text-gray-600">Confidence</span>
          <div className="text-lg font-semibold text-gray-900">
            {(data.confidence_score * 100).toFixed(0)}%
          </div>
        </div>
        <div>
          <span className="text-xs text-gray-600">Risk Level</span>
          <div className={`text-lg font-semibold capitalize ${riskColor}`}>
            {data.risk_level}
          </div>
        </div>
      </div>

      {/* Summary */}
      <div>
        <h4 className="text-sm font-medium text-gray-700 mb-2">Summary</h4>
        <p className="text-sm text-gray-700 leading-relaxed">{data.summary}</p>
      </div>
    </div>
  );
}
