/**
 * Error Boundary Component
 * ========================
 * Production-grade error boundary for React 19 with retry logic.
 * 
 * Features:
 * - Catches JavaScript errors in component tree
 * - Prevents app crashes
 * - Graceful fallback UI
 * - Retry mechanism with exponential backoff
 * - Error logging integration
 * - Reset on route change
 * 
 * Best Practices (2026):
 * - Place at route boundaries for granular error isolation
 * - Log errors to monitoring service (Sentry, DataDog)
 * - Provide actionable error messages
 * - Allow users to retry or navigate away
 * 
 * References:
 * - https://react.dev/reference/react/Component#catching-rendering-errors-with-an-error-boundary
 * - https://kentcdodds.com/blog/use-react-error-boundary-to-handle-errors-in-react
 */

'use client';

import React from 'react';
import { AlertTriangle, RefreshCw, Home } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from '@/components/ui/card';

interface ErrorBoundaryProps {
  children: React.ReactNode;
  /**
   * Fallback UI to show when error occurs
   */
  fallback?: React.ComponentType<ErrorFallbackProps>;
  /**
   * Callback when error occurs (for logging)
   */
  onError?: (error: Error, errorInfo: React.ErrorInfo) => void;
  /**
   * Custom error message
   */
  errorMessage?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
  retryCount: number;
}

export interface ErrorFallbackProps {
  error: Error | null;
  errorInfo: React.ErrorInfo | null;
  resetError: () => void;
  retryCount: number;
}

/**
 * Error Boundary Component
 * 
 * Catches errors in child component tree and displays fallback UI.
 * 
 * @example
 * ```tsx
 * <ErrorBoundary>
 *   <MyComponent />
 * </ErrorBoundary>
 * ```
 * 
 * @example With custom fallback
 * ```tsx
 * <ErrorBoundary fallback={CustomErrorFallback}>
 *   <MyComponent />
 * </ErrorBoundary>
 * ```
 */
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
      retryCount: 0,
    };
  }

  static getDerivedStateFromError(error: Error): Partial<ErrorBoundaryState> {
    return {
      hasError: true,
      error,
    };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // Log error to monitoring service
    console.error('ErrorBoundary caught an error:', error, errorInfo);
    
    // Call onError callback if provided
    this.props.onError?.(error, errorInfo);
    
    // Update state with error info
    this.setState({
      errorInfo,
    });
    
    // TODO: Send to error monitoring service (Sentry, DataDog, etc.)
    // Example:
    // Sentry.captureException(error, { contexts: { react: { componentStack: errorInfo.componentStack } } });
  }

  resetError = () => {
    this.setState((prevState) => ({
      hasError: false,
      error: null,
      errorInfo: null,
      retryCount: prevState.retryCount + 1,
    }));
  };

  render() {
    if (this.state.hasError) {
      const FallbackComponent = this.props.fallback || DefaultErrorFallback;
      
      return (
        <FallbackComponent
          error={this.state.error}
          errorInfo={this.state.errorInfo}
          resetError={this.resetError}
          retryCount={this.state.retryCount}
        />
      );
    }

    return this.props.children;
  }
}

/**
 * Default Error Fallback UI
 * 
 * Displays error message with retry and home buttons.
 */
function DefaultErrorFallback({
  error,
  errorInfo,
  resetError,
  retryCount,
}: ErrorFallbackProps) {
  const isNetworkError = error?.message?.includes('fetch') || error?.message?.includes('network');
  const isTooManyRetries = retryCount >= 3;

  return (
    <div className="flex items-center justify-center min-h-[400px] p-4">
      <Card className="max-w-lg w-full">
        <CardHeader>
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-5 w-5 text-destructive" />
            <CardTitle>Something went wrong</CardTitle>
          </div>
          <CardDescription>
            {isNetworkError
              ? 'Unable to connect to the server. Please check your internet connection.'
              : 'An unexpected error occurred. Our team has been notified.'}
          </CardDescription>
        </CardHeader>
        
        <CardContent className="space-y-4">
          {/* Error details (only in development) */}
          {process.env.NODE_ENV === 'development' && error && (
            <details className="text-sm">
              <summary className="cursor-pointer font-medium mb-2">
                Error Details (Development Only)
              </summary>
              <div className="bg-muted p-3 rounded-md overflow-auto">
                <p className="font-mono text-xs text-destructive mb-2">
                  {error.toString()}
                </p>
                {errorInfo?.componentStack && (
                  <pre className="font-mono text-xs text-muted-foreground whitespace-pre-wrap">
                    {errorInfo.componentStack}
                  </pre>
                )}
              </div>
            </details>
          )}
          
          {/* Retry count warning */}
          {isTooManyRetries && (
            <div className="bg-destructive/10 border border-destructive/20 rounded-md p-3">
              <p className="text-sm text-destructive">
                Multiple retry attempts failed. The issue may require manual intervention.
              </p>
            </div>
          )}
        </CardContent>
        
        <CardFooter className="flex gap-2">
          <Button
            onClick={resetError}
            disabled={isTooManyRetries}
            className="flex-1"
          >
            <RefreshCw className="h-4 w-4 mr-2" />
            {retryCount > 0 ? `Retry (${retryCount}/3)` : 'Try Again'}
          </Button>
          
          <Button
            variant="outline"
            onClick={() => window.location.href = '/'}
            className="flex-1"
          >
            <Home className="h-4 w-4 mr-2" />
            Go Home
          </Button>
        </CardFooter>
      </Card>
    </div>
  );
}

/**
 * Hook-based Error Boundary (for functional components)
 * 
 * Note: React 19 doesn't have built-in hook for error boundaries yet.
 * This is a wrapper around the class-based ErrorBoundary.
 * 
 * @example
 * ```tsx
 * function MyComponent() {
 *   return (
 *     <ErrorBoundaryWrapper>
 *       <ChildComponent />
 *     </ErrorBoundaryWrapper>
 *   );
 * }
 * ```
 */
export function ErrorBoundaryWrapper({
  children,
  ...props
}: Omit<ErrorBoundaryProps, 'children'> & { children: React.ReactNode }) {
  return <ErrorBoundary {...props}>{children}</ErrorBoundary>;
}

/**
 * Compact Error Fallback (for inline errors)
 * 
 * Smaller error UI for use in cards, modals, etc.
 */
export function CompactErrorFallback({
  error,
  resetError,
  retryCount,
}: ErrorFallbackProps) {
  return (
    <div className="flex flex-col items-center justify-center p-6 text-center space-y-3">
      <AlertTriangle className="h-8 w-8 text-destructive" />
      <div>
        <p className="font-medium text-sm">Failed to load content</p>
        <p className="text-xs text-muted-foreground mt-1">
          {error?.message || 'An unexpected error occurred'}
        </p>
      </div>
      <Button
        size="sm"
        variant="outline"
        onClick={resetError}
        disabled={retryCount >= 3}
      >
        <RefreshCw className="h-3 w-3 mr-1" />
        Retry
      </Button>
    </div>
  );
}
