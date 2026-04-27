/**
 * Loading State Component
 * =======================
 * Production-grade loading state wrapper for TanStack Query v5.
 * 
 * Features:
 * - Proper isPending/isFetching distinction
 * - Skeleton screens for initial loads
 * - Subtle indicators for background refetches
 * - Empty state handling
 * - Error state with retry
 * - Optimistic updates support
 * 
 * Best Practices (2026):
 * - Use isPending for initial loads (show skeleton)
 * - Use isFetching for background updates (show subtle indicator)
 * - Never show blank screens
 * - Progressive disclosure (show partial data)
 * - Graceful degradation
 * 
 * References:
 * - https://tanstack.com/query/latest/docs/react/guides/queries
 * - https://www.carlosdubon.dev/blog/understanding-loading-states-in-tanstack-query-v5
 */

'use client';

import React from 'react';
import { Loader2, RefreshCw, AlertCircle, Inbox } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface LoadingStateProps<TData> {
  /**
   * Query state from TanStack Query
   */
  isPending: boolean;
  isFetching: boolean;
  isError: boolean;
  error: Error | null;
  data: TData | undefined;
  
  /**
   * Refetch function from TanStack Query
   */
  refetch?: () => void;
  
  /**
   * Children to render when data is loaded
   */
  children: (data: TData) => React.ReactNode;
  
  /**
   * Skeleton component for initial load
   */
  skeleton?: React.ReactNode;
  
  /**
   * Empty state component when data is empty
   */
  emptyState?: React.ReactNode;
  
  /**
   * Custom error component
   */
  errorComponent?: React.ReactNode;
  
  /**
   * Check if data is empty (for empty state)
   */
  isEmpty?: (data: TData) => boolean;
  
  /**
   * Show refetch indicator during background updates
   */
  showRefetchIndicator?: boolean;
  
  /**
   * Custom className for container
   */
  className?: string;
}

/**
 * Loading State Wrapper
 * 
 * Handles all loading, error, and empty states for TanStack Query.
 * 
 * @example
 * ```tsx
 * const { data, isPending, isFetching, isError, error, refetch } = useQuery({
 *   queryKey: ['users'],
 *   queryFn: fetchUsers,
 * });
 * 
 * return (
 *   <LoadingState
 *     isPending={isPending}
 *     isFetching={isFetching}
 *     isError={isError}
 *     error={error}
 *     data={data}
 *     refetch={refetch}
 *     skeleton={<SkeletonList items={5} />}
 *     isEmpty={(data) => data.length === 0}
 *   >
 *     {(data) => (
 *       <ul>
 *         {data.map((user) => (
 *           <li key={user.id}>{user.name}</li>
 *         ))}
 *       </ul>
 *     )}
 *   </LoadingState>
 * );
 * ```
 */
export function LoadingState<TData>({
  isPending,
  isFetching,
  isError,
  error,
  data,
  refetch,
  children,
  skeleton,
  emptyState,
  errorComponent,
  isEmpty,
  showRefetchIndicator = true,
  className,
}: LoadingStateProps<TData>) {
  // Error state (highest priority)
  if (isError && error) {
    if (errorComponent) {
      return <>{errorComponent}</>;
    }
    
    return (
      <div className={cn("flex flex-col items-center justify-center p-8 text-center space-y-4", className)}>
        <AlertCircle className="h-12 w-12 text-destructive" />
        <div>
          <h3 className="font-semibold text-lg">Failed to load data</h3>
          <p className="text-sm text-muted-foreground mt-1">
            {error.message || 'An unexpected error occurred'}
          </p>
        </div>
        {refetch && (
          <Button onClick={() => refetch()} variant="outline" size="sm">
            <RefreshCw className="h-4 w-4 mr-2" />
            Try Again
          </Button>
        )}
      </div>
    );
  }
  
  // Initial loading state (no data yet)
  if (isPending && !data) {
    if (skeleton) {
      return <div className={className}>{skeleton}</div>;
    }
    
    return (
      <div className={cn("flex items-center justify-center p-8", className)}>
        <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
      </div>
    );
  }
  
  // Empty state (data loaded but empty)
  if (data && isEmpty?.(data)) {
    if (emptyState) {
      return <div className={className}>{emptyState}</div>;
    }
    
    return (
      <div className={cn("flex flex-col items-center justify-center p-8 text-center space-y-3", className)}>
        <Inbox className="h-12 w-12 text-muted-foreground" />
        <div>
          <h3 className="font-semibold">No data available</h3>
          <p className="text-sm text-muted-foreground mt-1">
            There's nothing to display at the moment
          </p>
        </div>
      </div>
    );
  }
  
  // Data loaded successfully
  if (data) {
    return (
      <div className={cn("relative", className)}>
        {/* Background refetch indicator */}
        {isFetching && showRefetchIndicator && (
          <div className="absolute top-0 right-0 z-10">
            <div className="flex items-center gap-2 bg-muted/80 backdrop-blur-sm px-3 py-1.5 rounded-bl-lg border-l border-b">
              <Loader2 className="h-3 w-3 animate-spin" />
              <span className="text-xs text-muted-foreground">Updating...</span>
            </div>
          </div>
        )}
        
        {children(data)}
      </div>
    );
  }
  
  // Fallback (should never reach here)
  return null;
}

/**
 * Inline Loading Indicator
 * 
 * Small loading indicator for buttons, inline text, etc.
 */
export function InlineLoader({ className }: { className?: string }) {
  return (
    <Loader2 className={cn("h-4 w-4 animate-spin", className)} />
  );
}

/**
 * Full Page Loading
 * 
 * Loading indicator for full page loads.
 */
export function FullPageLoading({ message = "Loading..." }: { message?: string }) {
  return (
    <div className="flex flex-col items-center justify-center min-h-screen">
      <Loader2 className="h-12 w-12 animate-spin text-primary mb-4" />
      <p className="text-muted-foreground">{message}</p>
    </div>
  );
}

/**
 * Empty State Component
 * 
 * Reusable empty state for lists, tables, etc.
 */
export function EmptyState({
  icon: Icon = Inbox,
  title = "No data available",
  description = "There's nothing to display at the moment",
  action,
  className,
}: {
  icon?: React.ComponentType<{ className?: string }>;
  title?: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex flex-col items-center justify-center p-12 text-center space-y-4", className)}>
      <Icon className="h-16 w-16 text-muted-foreground/50" />
      <div>
        <h3 className="font-semibold text-lg">{title}</h3>
        <p className="text-sm text-muted-foreground mt-1 max-w-sm">
          {description}
        </p>
      </div>
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

/**
 * Refetch Indicator
 * 
 * Subtle indicator for background refetches.
 */
export function RefetchIndicator({ isFetching }: { isFetching: boolean }) {
  if (!isFetching) return null;
  
  return (
    <div className="flex items-center gap-2 text-xs text-muted-foreground">
      <Loader2 className="h-3 w-3 animate-spin" />
      <span>Refreshing...</span>
    </div>
  );
}
