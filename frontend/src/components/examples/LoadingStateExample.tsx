/**
 * Loading State Examples
 * ======================
 * Production-grade examples showing how to use loading states with TanStack Query v5.
 * 
 * This file demonstrates best practices for:
 * - Skeleton screens for initial loads
 * - Subtle indicators for background refetches
 * - Error handling with retry
 * - Empty states
 * - Optimistic updates
 */

'use client';

import { useModels } from '@/hooks/useModels';
import { LoadingState, EmptyState } from '@/components/ui/loading-state';
import { SkeletonCard, SkeletonList, SkeletonTable } from '@/components/ui/skeleton';
import { ErrorBoundary, CompactErrorFallback } from '@/components/ui/error-boundary';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Plus } from 'lucide-react';

/**
 * Example 1: List with Skeleton
 * 
 * Shows skeleton cards during initial load, then displays data.
 * Background refetches show subtle "Updating..." indicator.
 */
export function ModelsListExample() {
  const { data, isPending, isFetching, isError, error, refetch } = useModels();

  return (
    <ErrorBoundary fallback={CompactErrorFallback}>
      <LoadingState
        isPending={isPending}
        isFetching={isFetching}
        isError={isError}
        error={error}
        data={data}
        refetch={refetch}
        skeleton={<SkeletonList items={5} />}
        isEmpty={(data) => data.models.length === 0}
        emptyState={
          <EmptyState
            title="No models found"
            description="Get started by training your first ML model"
            action={
              <Button>
                <Plus className="h-4 w-4 mr-2" />
                Train Model
              </Button>
            }
          />
        }
      >
        {(data) => (
          <div className="space-y-3">
            {data.models.map((model) => (
              <Card key={model.model_id}>
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <CardTitle className="text-base">{model.model_name}</CardTitle>
                    <Badge variant={model.state === 'live' ? 'default' : 'secondary'}>
                      {model.state}
                    </Badge>
                  </div>
                  <CardDescription>
                    Version {model.version} • Accuracy: {(model.accuracy * 100).toFixed(1)}%
                  </CardDescription>
                </CardHeader>
              </Card>
            ))}
          </div>
        )}
      </LoadingState>
    </ErrorBoundary>
  );
}

/**
 * Example 2: Table with Skeleton
 * 
 * Shows skeleton table during initial load.
 */
export function ModelsTableExample() {
  const { data, isPending, isFetching, isError, error, refetch } = useModels();

  return (
    <ErrorBoundary>
      <LoadingState
        isPending={isPending}
        isFetching={isFetching}
        isError={isError}
        error={error}
        data={data}
        refetch={refetch}
        skeleton={<SkeletonTable rows={10} columns={5} />}
        isEmpty={(data) => data.models.length === 0}
      >
        {(data) => (
          <div className="rounded-md border">
            <table className="w-full">
              <thead>
                <tr className="border-b bg-muted/50">
                  <th className="p-3 text-left text-sm font-medium">Model</th>
                  <th className="p-3 text-left text-sm font-medium">Version</th>
                  <th className="p-3 text-left text-sm font-medium">State</th>
                  <th className="p-3 text-left text-sm font-medium">Accuracy</th>
                  <th className="p-3 text-left text-sm font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {data.models.map((model) => (
                  <tr key={model.model_id} className="border-b">
                    <td className="p-3 text-sm">{model.model_name}</td>
                    <td className="p-3 text-sm">{model.version}</td>
                    <td className="p-3 text-sm">
                      <Badge variant={model.state === 'live' ? 'default' : 'secondary'}>
                        {model.state}
                      </Badge>
                    </td>
                    <td className="p-3 text-sm">{(model.accuracy * 100).toFixed(1)}%</td>
                    <td className="p-3 text-sm">
                      <Button size="sm" variant="outline">
                        View
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </LoadingState>
    </ErrorBoundary>
  );
}

/**
 * Example 3: Card Grid with Skeleton
 * 
 * Shows skeleton cards in a grid layout.
 */
export function ModelsGridExample() {
  const { data, isPending, isFetching, isError, error, refetch } = useModels();

  return (
    <ErrorBoundary>
      <LoadingState
        isPending={isPending}
        isFetching={isFetching}
        isError={isError}
        error={error}
        data={data}
        refetch={refetch}
        skeleton={
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <SkeletonCard key={i} />
            ))}
          </div>
        }
        isEmpty={(data) => data.models.length === 0}
      >
        {(data) => (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {data.models.map((model) => (
              <Card key={model.model_id}>
                <CardHeader>
                  <CardTitle className="text-base">{model.model_name}</CardTitle>
                  <CardDescription>Version {model.version}</CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="space-y-2">
                    <div className="flex justify-between text-sm">
                      <span className="text-muted-foreground">State</span>
                      <Badge variant={model.state === 'live' ? 'default' : 'secondary'}>
                        {model.state}
                      </Badge>
                    </div>
                    <div className="flex justify-between text-sm">
                      <span className="text-muted-foreground">Accuracy</span>
                      <span className="font-medium">{(model.accuracy * 100).toFixed(1)}%</span>
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </LoadingState>
    </ErrorBoundary>
  );
}

/**
 * Example 4: Inline Loading (for mutations)
 * 
 * Shows inline loading indicator during mutations.
 */
export function MutationExample() {
  const { mutate, isPending } = useUpdateModelState();
  const { success, error: showError } = useToast();

  const handleUpdate = async (modelId: number, state: string) => {
    mutate(
      { modelId, data: { state } },
      {
        onSuccess: () => {
          success('Model state updated successfully');
        },
        onError: (error) => {
          showError('Failed to update model state', error.message);
        },
      }
    );
  };

  return (
    <Button onClick={() => handleUpdate(1, 'live')} disabled={isPending}>
      {isPending ? (
        <>
          <InlineLoader className="mr-2" />
          Updating...
        </>
      ) : (
        'Update State'
      )}
    </Button>
  );
}
