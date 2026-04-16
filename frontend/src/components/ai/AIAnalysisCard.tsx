/**
 * AI Analysis Card Component
 * Displays AI-powered sentiment analysis and key insights
 */
import React from 'react';
import { useAIAnalysis } from '@/hooks/useAIAnalysis';

interface AIAnalysisCardProps {
  symbol: string;
}

export function AIAnalysisCard({ symbol }: AIAnalysisCardProps) {
  const { data, isLoading, error } = useAIAnalysis(symbol);

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
        <h3 className="text-lg font-semibold text-gray-900 mb-2">AI Analysis</h3>
        <p className="text-sm text-red-600">Failed to load AI analysis</p>
      </div>
    );
  }

  if (!data) return null;

  const { sentiment_analysis, key_insights } = data;
  const sentimentColor = 
    sentiment_analysis.overall_sentiment === 'positive' ? 'text-green-600 bg-green-100' :
    sentiment_analysis.overall_sentiment === 'negative' ? 'text-red-600 bg-red-100' :
    'text-gray-600 bg-gray-100';

  return (
    <div className="bg-white rounded-lg shadow p-6">
      <h3 className="text-lg font-semibold text-gray-900 mb-4">AI Analysis</h3>
      
      {/* Sentiment Analysis */}
      <div className="mb-4">
        <h4 className="text-sm font-medium text-gray-700 mb-2">Market Sentiment</h4>
        <div className="flex items-center gap-2 mb-2">
          <span className={`px-3 py-1 rounded-full text-sm font-medium capitalize ${sentimentColor}`}>
            {sentiment_analysis.overall_sentiment}
          </span>
          <span className="text-sm text-gray-600">
            Score: {sentiment_analysis.sentiment_score.toFixed(2)}
          </span>
        </div>
        <div className="text-xs text-gray-600">
          Confidence: {(sentiment_analysis.confidence * 100).toFixed(0)}%
        </div>
      </div>

      {/* Key Insights */}
      <div>
        <h4 className="text-sm font-medium text-gray-700 mb-2">Key Insights</h4>
        <div className="space-y-2">
          {key_insights.map((insight: any, idx: number) => (
            <div key={idx} className="border-l-2 border-blue-500 pl-3 py-1">
              <div className="flex items-center gap-2 mb-1">
                <span className="text-xs font-medium text-blue-600 uppercase">
                  {insight.category}
                </span>
                <span className="text-xs text-gray-500">
                  {(insight.importance * 100).toFixed(0)}% importance
                </span>
              </div>
              <p className="text-sm text-gray-700">{insight.insight}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
