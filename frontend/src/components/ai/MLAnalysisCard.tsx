/**
 * ML Analysis Card Component
 * Displays machine learning predictions and pattern recognition
 */
import React from 'react';
import { useMLAnalysis } from '@/hooks/useMLAnalysis';

interface MLAnalysisCardProps {
  symbol: string;
}

export function MLAnalysisCard({ symbol }: MLAnalysisCardProps) {
  const { data, isLoading, error } = useMLAnalysis(symbol);

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
        <h3 className="text-lg font-semibold text-gray-900 mb-2">ML Analysis</h3>
        <p className="text-sm text-red-600">Failed to load ML analysis</p>
      </div>
    );
  }

  if (!data) return null;

  const { price_prediction, pattern_recognition } = data;
  const directionColor = 
    price_prediction.direction === 'bullish' ? 'text-green-600' :
    price_prediction.direction === 'bearish' ? 'text-red-600' :
    'text-gray-600';

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">ML Analysis</h3>
      
      {/* Price Prediction */}
      <div className="mb-4">
        <h4 className="text-sm font-medium text-gray-700 mb-2">Price Prediction</h4>
        <div className="flex items-baseline gap-2">
          <span className="text-2xl font-bold text-gray-900">
            ₹{price_prediction.predicted_price.toFixed(2)}
          </span>
          <span className={`text-sm font-medium ${directionColor} capitalize`}>
            {price_prediction.direction}
          </span>
        </div>
        <div className="mt-1 flex items-center gap-4 text-sm text-gray-600">
          <span>Confidence: {(price_prediction.confidence * 100).toFixed(0)}%</span>
          <span>Timeframe: {price_prediction.timeframe}</span>
        </div>
      </div>

      {/* Pattern Recognition */}
      <div>
        <h4 className="text-sm font-medium text-gray-700 mb-2">Detected Patterns</h4>
        <div className="flex flex-wrap gap-2 mb-2">
          {pattern_recognition.detected_patterns.map((pattern: string, idx: number) => (
            <span
              key={idx}
              className="px-2 py-1 bg-blue-100 text-blue-800 text-xs rounded-full"
            >
              {pattern.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-4 text-xs text-gray-600">
          <span>Strength: {(pattern_recognition.strength * 100).toFixed(0)}%</span>
          <span>Reliability: {(pattern_recognition.reliability * 100).toFixed(0)}%</span>
        </div>
      </div>
    </div>
  );
}
